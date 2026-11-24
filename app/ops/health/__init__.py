"""Agent Health Monitor — periodic health checks and self-healing.

Monitors agent health via a pluggable probe registry:
- 按 agent 名匹配专属探测（如 daily-agent 数据源巡检、webportal HTTP 探测）
- 未注册专属探测的 Agent 按其 transport 匹配通用探测
  （如 cc_connect 通道 → webhook ping）
- 都没有 → unknown

Health levels:
- green:  response < 5s
- yellow: response 5-30s
- red:    timeout or error
- dead:   process not found
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.agents.registry import AgentDef, AgentRegistry
from app.observability import tracer
from app.protocols.cc_connect.client import CCConnectClient

logger = logging.getLogger("health")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parents[3]
HEALTH_LOG_DIR = PROJECT_DIR / "logs" / "health"

# ── 健康探测注册表（可插拔，为后续熔断预留口子）────────────────────────
#
# key 两种形态：`name:{agent_name}`（专属探测，最优先）、
# `transport:{transport}`（同类通道共用探测）。
# 探测函数签名：async fn(monitor, agent) -> {"status","duration_ms","detail"}。

ProbeFn = Any  # async Callable[[AgentHealthMonitor, AgentDef], dict]

_PROBES: dict[str, ProbeFn] = {}


def register_probe(key: str, fn: ProbeFn) -> None:
    """注册健康探测函数。key 形如 name:daily-agent 或 transport:cc_connect。"""
    _PROBES[key] = fn


def _default_probes() -> dict[str, ProbeFn]:
    """默认探测表 — 覆盖现有全部探测策略。"""
    return {
        "name:webportal-agent": _probe_webportal_a2a,
        # cc_connect 通道通用探测：dev-bot / db / deploy / config 共用
        "transport:cc_connect": _probe_cc_connect,
        # handler 通道通用探测：schedule-agent 等进程内 Handler
        "transport:handler": _probe_handler,
    }

# 数据源清单注册表（从 sources.json 加载，缺省降级为单主源）— 导入已上移至文件头


async def _probe_daily_agent(monitor, agent: AgentDef) -> dict:
    """daily-agent 探测：AI HOT 单源可用性 + 本地缓存存量。

    旧目录巡检（LocalKBase 多数据源）随目录读取设计一并下线——
    数据单源化后探测「API 可达 + 缓存有货」即可。
    """
    start = time.time()
    # 本地缓存存量（网络零成本，先看家底）
    from app.domains.daily.store import DailyStore

    cached_days = 0
    try:
        entries = DailyStore().all_entries()
        cached_days = sum(1 for e in entries.values() if e.get("markdown"))
    except Exception:
        pass

    # AI HOT 可达性：查一次有界归档索引（20s 超时，不探 dailies/latest 免双跳）
    from app.domains.daily.aihot import AihotError, list_daily_archive

    try:
        archive = await list_daily_archive(limit=7)
        elapsed = (time.time() - start) * 1000
        if archive:
            detail = f"AI HOT 可达，归档 {len(archive)}+ 期，本地缓存 {cached_days} 期"
            return {"status": "green", "duration_ms": round(elapsed, 1), "detail": detail}
        return {"status": "yellow", "duration_ms": round(elapsed, 1),
                "detail": f"AI HOT 归档为空，本地缓存 {cached_days} 期"}
    except AihotError as e:
        elapsed = (time.time() - start) * 1000
        detail = f"AI HOT 不可达：{str(e)[:60]}；本地缓存 {cached_days} 期"
        status = "green" if cached_days else "red"
        return {"status": status, "duration_ms": round(elapsed, 1), "detail": detail}


async def _probe_webportal_a2a(monitor, agent: AgentDef) -> dict:
    """webportal-agent 探测：A2A Agent Card 可发现性（webportal-order-agent 服务）。

    卡片在 = 服务进程活着；卡片 version 即上游引擎版本（独立发版，探活顺带
    把版本带进健康面板）。旧实现读本地 skill 包加载案例库，已随 v3.0.0
    去 vendor（bot/skills/order-sql 删除）退役。
    """
    import httpx

    from app.agents.handlers.webportal_handler import _A2A_BASE

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{_A2A_BASE}/.well-known/agent-card.json")
        elapsed = (time.time() - start) * 1000
        if resp.status_code != 200:
            return {"status": "red", "duration_ms": round(elapsed, 1),
                    "detail": f"A2A card HTTP {resp.status_code}（服务异常）"}
        card = resp.json()
        skills = len(card.get("skills") or [])
        return {"status": "green", "duration_ms": round(elapsed, 1),
                "detail": f"A2A 服务就绪：{card.get('name', '?')} "
                          f"v{card.get('version', '?')}（{skills} 技能）"}
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return {"status": "red", "duration_ms": round(elapsed, 1),
                "detail": f"A2A 服务不可达（python -m order_agent.a2a_server）: "
                          f"{str(e)[:50]}"}


async def _probe_cc_connect(monitor, agent: AgentDef) -> dict:
    """cc_connect 通道探测：TCP connect 探活 cc-connect 监听端口（通用 Agent 共用）。

    历史实现向 webhook 发 `prompt="ping"`，判定只取 HTTP 200 往返、从不读模型回复，
    等于为一个端口可达性结论支付一整个 LLM 往返；且 ping 会经 cc-connect 投进 Claude
    会话（实测 ≈8 次/5 分钟 ≈ 96 次/小时），把接收侧上下文刷爆。改为纯 TCP 连接：
    语义等价（端口在监听 == 通道可用），零模型成本。
    """
    start = time.time()
    host, port = _cc_connect_endpoint(monitor)
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0,
        )
    except Exception as e:  # OSError / TimeoutError：端口不通或未监听
        elapsed = (time.time() - start) * 1000
        return {"status": "red", "duration_ms": round(elapsed, 1),
                "detail": f"tcp connect {host}:{port} failed: {type(e).__name__}"}
    elapsed = (time.time() - start) * 1000
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass  # 关闭失败不影响"端口可达"这一结论
    health = "green" if elapsed < 5000 else "yellow" if elapsed < 30000 else "red"
    return {"status": health, "duration_ms": round(elapsed, 1),
            "detail": f"tcp {host}:{port} reachable in {elapsed:.0f}ms"}


def _cc_connect_endpoint(monitor) -> tuple[str, int]:
    """从 cc-connect webhook_url 解析 (host, port)，取不到则回落默认值。"""
    cfg = getattr(getattr(monitor, "_cc_client", None), "config", None)
    url = getattr(cfg, "webhook_url", "") or "" if cfg is not None else ""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 9111)
    return host, int(port)


async def _probe_handler(monitor, agent: AgentDef) -> dict:
    """handler 通道通用探测：进程内 Handler 已注册且 LLM 客户端就绪即 green。

    Handler 与 bot 同进程，无外部通道可 ping；注册表命中 + tools 非空
    即代表路由到它后能实际执行（未注册 = 启动期 discovery 失败，red）。
    """
    start = time.time()
    registry = monitor._handler_registry
    handler = registry.get(agent.name) if registry is not None else None
    elapsed = (time.time() - start) * 1000
    if handler is None:
        return {"status": "red", "duration_ms": round(elapsed, 1),
                "detail": "Handler 未注册（discovery 失败？）"}
    n_tools = len(getattr(handler, "tools", []) or [])
    return {"status": "green", "duration_ms": round(elapsed, 1),
            "detail": f"进程内 Handler 就绪，{n_tools} 个工具"}


# 注册默认探测表（须在 probe 函数定义之后执行）
_PROBES.update(_default_probes())


@dataclass
class HealthStatus:
    """Health status for a single agent."""

    agent_name: str
    status: str  # green / yellow / red / dead
    duration_ms: float = 0
    detail: str = ""
    consecutive_failures: int = 0
    last_ok: str = ""
    last_fail: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.status in ("green", "yellow")

    @property
    def is_critical(self) -> bool:
        return self.status in ("red", "dead")


class AgentHealthMonitor:
    """Periodic health monitor for all agents.

    健康检查策略（查探测注册表）：
    - agent 名有专属探测 → 用之（daily/webportal）
    - 否则按 transport 匹配通用探测（cc_connect 通道 webhook ping）
    - 都没有 → unknown
    """

    def __init__(
        self,
        registry: AgentRegistry,
        cc_client: CCConnectClient | None,
        check_interval_sec: int = 300,
        max_consecutive_failures: int = 3,
        webportal_base_url: str = "http://127.0.0.1:8000",
        handler_registry: Any = None,
    ):
        self._registry = registry
        self._cc_client = cc_client
        self._check_interval = check_interval_sec
        self._max_failures = max_consecutive_failures
        self._webportal_base_url = webportal_base_url
        self._handler_registry = handler_registry
        self._statuses: dict[str, HealthStatus] = {}
        self._running = False
        self._task: asyncio.Task | None = None

        HEALTH_LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _get_status(self, agent_name: str) -> HealthStatus:
        if agent_name not in self._statuses:
            self._statuses[agent_name] = HealthStatus(agent_name=agent_name, status="unknown")
        return self._statuses[agent_name]

    def _find_probe(self, agent: AgentDef) -> ProbeFn | None:
        """按 name → transport 顺序查找可用探测。"""
        probe = _PROBES.get(f"name:{agent.name}")
        if probe is None and agent.transport:
            probe = _PROBES.get(f"transport:{agent.transport}")
        return probe

    async def check_agent(self, agent: AgentDef) -> HealthStatus:
        """Check a single agent's health using the registered probe."""
        status = self._get_status(agent.name)

        try:
            probe = self._find_probe(agent)
            if probe is None:
                result: dict[str, Any] = {"status": "unknown", "duration_ms": 0,
                                          "detail": f"未接入健康检查 (transport={agent.transport})"}
            elif probe is _probe_cc_connect and not self._cc_client:
                result = {"status": "red", "duration_ms": 0, "detail": "cc-connect 未启用"}
            else:
                result = await probe(self, agent)
        except Exception as e:
            result = {"status": "red", "duration_ms": 0, "detail": str(e)[:60]}

        # 更新状态
        status.status = result["status"]
        status.duration_ms = result.get("duration_ms", 0)
        status.detail = result.get("detail", "")

        if result["status"] in ("green", "yellow"):
            status.last_ok = datetime.now(TZ).isoformat()
            status.consecutive_failures = 0
        else:
            status.last_fail = datetime.now(TZ).isoformat()
            status.consecutive_failures += 1

        # 日志
        tracer.health_log(
            agent_name=agent.name,
            status=status.status,
            duration_ms=status.duration_ms,
            detail=status.detail,
        )
        if status.is_critical:
            logger.warning(
                f"Health: {agent.name} = {status.status} "
                f"({status.detail}) "
                f"failures={status.consecutive_failures}/{self._max_failures}"
            )

        return status

    async def check_all(self) -> dict[str, HealthStatus]:
        """Check all enabled agents."""
        # Any 中转：registry.enabled() 的 list[AgentDef] 在增量 mypy 下被推断成
        # list? 部分类型（check_untyped_defs=False，同 crew.py routable 先例）
        agents: Any = self._registry.enabled()
        results: dict[str, HealthStatus] = {}

        for agent in agents:
            results[agent.name] = await self.check_agent(agent)

        healthy = sum(1 for s in results.values() if s.is_healthy)
        critical = sum(1 for s in results.values() if s.is_critical)
        total = len(results)
        logger.info(f"Health check complete: {healthy}/{total} healthy, {critical} critical")

        self._write_status_snapshot(results)
        return results

    def _write_status_snapshot(self, results: dict[str, HealthStatus]):
        """Write current health status snapshot."""
        snapshot = {
            "timestamp": datetime.now(TZ).isoformat(),
            "total": len(results),
            "healthy": sum(1 for s in results.values() if s.is_healthy),
            "critical": sum(1 for s in results.values() if s.is_critical),
            "agents": {
                name: {
                    "status": s.status,
                    "duration_ms": s.duration_ms,
                    "consecutive_failures": s.consecutive_failures,
                    "detail": s.detail,
                }
                for name, s in results.items()
            },
        }
        try:
            snapshot_file = HEALTH_LOG_DIR / "current.json"
            with open(snapshot_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write health snapshot: {e}")

    def get_status(self, agent_name: str) -> HealthStatus | None:
        """Get the latest health status for an agent."""
        return self._statuses.get(agent_name)

    def get_all_statuses(self) -> dict[str, HealthStatus]:
        """Get all agent health statuses."""
        return dict(self._statuses)

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of current health statuses."""
        statuses = self.get_all_statuses()
        return {
            "total": len(statuses),
            "healthy": sum(1 for s in statuses.values() if s.is_healthy),
            "critical": sum(1 for s in statuses.values() if s.is_critical),
            "agents": {
                name: {"status": s.status, "duration_ms": s.duration_ms, "detail": s.detail}
                for name, s in statuses.items()
            },
        }

    async def start_periodic_check(self):
        """Start the periodic health check loop."""
        self._running = True
        self._task = asyncio.create_task(self._periodic_loop())
        logger.info(f"Health monitor started (interval={self._check_interval}s)")

    async def stop_periodic_check(self):
        """Stop the periodic health check loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Health monitor stopped")

    async def _periodic_loop(self):
        """Background loop that runs health checks periodically."""
        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                logger.exception(f"Health check cycle failed: {e}")
            await asyncio.sleep(self._check_interval)


# ── Convenience ─────────────────────────────────────────────────


async def quick_health_check(
    registry: AgentRegistry,
    cc_client: CCConnectClient | None,
    webportal_base_url: str = "http://127.0.0.1:8000",
    handler_registry: Any = None,
) -> dict[str, Any]:
    """Run a one-shot health check and return summary."""
    monitor = AgentHealthMonitor(
        registry, cc_client,
        check_interval_sec=999999,
        webportal_base_url=webportal_base_url,
        handler_registry=handler_registry,
    )
    await monitor.check_all()
    return monitor.get_summary()