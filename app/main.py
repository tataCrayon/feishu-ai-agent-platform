"""Main bot application — wires Channel SDK + dsh 执行面 + Memory + Agents.

Pipeline:
    1. DshGateway.handle() — 消息交给 dsh 会话（意图识别与分派都在内核里）
       dsh 未启用时回落到 CrewOrchestrator（LangGraph 回切保险）
    2. TraceContext creation (trace_id + span_id for chain tracking)
    3. Memory recall (L0 context)
    4. Agent dispatch (via MCP 工具面 / cc-connect relay)
    5. Audit logging (per-action audit trail)
    6. Memory capture (store conversation round)
    7. EDC metrics snapshot (periodic)

路由不靠正则或关键词表：dsh 侧按技能目录语义匹配，回切链路靠 Function Calling。
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lark_channel import InboundMessage

# ── 代理绕过（必须在任何 httpx/openai 客户端构造前执行）────────────────────
# 根因：Windows 系统代理（Clash 127.0.0.1:7897）开启时，
# httpx/openai SDK 经 urllib getproxies() 读注册表系统代理，aigw 网关请求
# 走代理出口 IP 发出 → 触发 aigw 令牌 IP 白名单 403 access_denied
# （"API Error: Connection refused" 的真实根因）。
# 这里进程内注入 NO_PROXY（含公司域名+内网），不依赖启动脚本/系统设置。
# Python 的 NO_PROXY 不支持通配后缀，公司域名须逐个列全。
_PROXY_BYPASS_DOMAINS = [
    "localhost", "127.0.0.1",
    "10.*", "192.168.*", "172.16.*", "172.17.*", "172.18.*", "172.19.*",
    "172.20.*", "172.21.*", "172.22.*", "172.23.*", "172.24.*", "172.25.*",
    "172.26.*", "172.27.*", "172.28.*", "172.29.*", "172.30.*", "172.31.*",
    # LLM 网关（aigw 令牌绑 IP 白名单，必须直连）
    "aigw.example.com", "example.com",
    # 公司内网域名（lark-cli / MCP / 内部服务）
    "example.com", "example.com", "example.com",
    "example.com", "example.com",
    # 飞书开放平台（国内接入点，走代理反而可能被风控）
    "feishu.cn", "larksuite.com",
]


def _ensure_proxy_bypass() -> None:
    """合并注入 NO_PROXY：已有配置保留在前，追加本项目的直连域名（去重）。"""
    existing = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    merged: list[str] = []
    for item in (existing.split(",") if existing else []) + _PROXY_BYPASS_DOMAINS:
        item = item.strip()
        if item and item not in merged:
            merged.append(item)
    value = ",".join(merged)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


_ensure_proxy_bypass()

from app.agents import AgentRegistry  # noqa: E402
from app.agents.handlers import (  # noqa: E402
    AgentHandlerRegistry,
    auto_discover_handlers,
)
from app.config import load_config  # noqa: E402
from app.dashboard import start_dashboard_server  # noqa: E402
from app.domains.daily import (  # noqa: E402
    DailyScheduler,
    DailyStore,
    SubscriptionManager,
)
from app.domains.webportal import WebPortalClient, WebPortalConfig  # noqa: E402
from app.dsh import (  # noqa: E402
    DshGateway,
    DshSupervisor,
    DshUnavailableError,
    GatewayConfig,
)
from app.dsh.mcp_specs import (  # noqa: E402
    absolute_path,
    build_dsh_supervisor_config,
)
from app.dsh.progress_card import (  # noqa: E402
    answer_fits_card,
    build_done as build_done_card,
    build_running as build_running_card,
)
from app.dsh.task_record import record_dsh_task  # noqa: E402
from app.dsh.timeline import Timeline  # noqa: E402
from app.eval.edc import EDCMetricsCollector  # noqa: E402
from app.gateway.aggregator import MessageAggregator  # noqa: E402
from app.gateway.channel import BotChannel  # noqa: E402
from app.guardrails.runtime import (  # noqa: E402
    RuntimeGuard,
    TaskLifecycleRecord,
    TaskLifecycleStore,
)
from app.mcpreaper import reap_orphans, start_watcher  # noqa: E402
from app.memory import MemoryClient, MemoryConfig  # noqa: E402
from app.memory.lifecycle import (  # noqa: E402
    MemoryCoreLifecycle,
    MemoryLifecycleConfig,
)
from app.observability import AgentTrace, TraceContext, tracer  # noqa: E402
from app.observability.task_record import TaskRecord, task_recorder  # noqa: E402
from app.ops.health import AgentHealthMonitor, quick_health_check  # noqa: E402
from app.ops.scheduler import ScheduledTaskService  # noqa: E402
from app.orchestrator import (  # noqa: E402
    CrewOrchestrator,
    set_attachment_downloader,
    set_late_delivery_sender,
)
from app.orchestrator.feedback import detect_negative_feedback  # noqa: E402
from app.protocols.cc_connect import CCConnectClient, CCConnectConfig  # noqa: E402

# Fix Windows encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)


def _install_file_logger() -> None:
    """进程日志同时落盘（`logs/bot/app.log`，10MB × 5 份轮转）。

    启动方式是 `start cmd /c python -m app.main`，stdout 进的是那个 cmd 窗口的
    缓冲区——窗口一关、机器一重启，日志就没了，出了事只剩「用户说不对」。
    文件 handler 是排查的最低前提；轮转防单文件涨到不可读。
    """
    from logging.handlers import RotatingFileHandler

    log_dir = Path(__file__).resolve().parent.parent / "logs" / "bot"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "app.log", maxBytes=10 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
    except OSError as e:  # 目录不可写不阻断启动：stdout 仍在
        logging.getLogger("bot").warning("文件日志不可用，仅输出到控制台: %s", e)
        return
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATEFMT))
    root = logging.getLogger()
    root.addHandler(handler)


def _install_exception_hooks() -> Callable[[object, dict], None]:
    """未捕获异常也进日志文件；返回事件循环钩子供 `main()` 挂上。

    事件循环里的回调抛错、`asyncio.ensure_future` 的 task 抛错，默认只打到
    stderr（那个 cmd 窗口）。没有这两个钩子，「进程还在但某条链路悄悄断了」
    就完全无迹可循。
    """
    loop_logger = logging.getLogger("bot.unhandled")

    def _on_loop_exception(loop: object, context: dict) -> None:
        exc = context.get("exception")
        loop_logger.error(
            "事件循环未捕获异常: %s", context.get("message", ""),
            exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
        )

    def _on_uncaught(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        loop_logger.critical("未捕获异常导致进程退出", exc_info=(exc_type, exc, tb))

    sys.excepthook = _on_uncaught
    return _on_loop_exception


_LOOP_EXCEPTION_HANDLER = _install_exception_hooks()
_install_file_logger()
logger = logging.getLogger("bot")

# 进度卡计时刷新间隔：卡片上「已处理 Ns」要跟着走，静默期（模型连续调工具、
# 几秒钟不吐字）也每 5s 重画一次。飞书卡片编辑限频约 5 次/秒，5s 远在安全区内。
_PROGRESS_TICK_SEC = 5.0
# 卡片刷新节流：过程帧（思考/工具）到达很密，每条都刷会撞飞书频率限制，
# 也只会在用户眼前闪过几十次半句话。2s 合并一次，终态必刷。
_PROGRESS_THROTTLE_SEC = 2.0


def _build_skill_catalog_store(config):
    """按配置建目录 store。关闭时返回 None，handler 工厂会拒绝空目录上线。"""
    global _skill_catalog_store
    sc = config.skill_catalog
    if not sc.enabled:
        logger.info("skill catalog disabled by config")
        return None
    from pathlib import Path

    from app.domains.skill_catalog.scan import CatalogScanner
    from app.domains.skill_catalog.store import CatalogStore

    root = Path(__file__).resolve().parent.parent
    bare = sc.bare_dir if Path(sc.bare_dir).is_absolute() else str(root / sc.bare_dir)
    snap = sc.snapshot_path if Path(sc.snapshot_path).is_absolute() else str(root / sc.snapshot_path)
    scanner = CatalogScanner(
        repo_url=sc.repo_url,
        bare_dir=bare,
        skip_branches=sc.skip_branches,
        fetch_timeout_sec=sc.fetch_timeout_sec,
    )
    _skill_catalog_store = CatalogStore(
        scanner,
        snapshot_path=snap,
        refresh_interval_hours=sc.refresh_interval_hours,
    )
    return _skill_catalog_store


async def _prd_send_card(chat_id: str, payload: object) -> None:
    """easyprd 发消息（卡片/通知）—— 晚绑定：service 在 channel 之前创建。

    payload 是 dict 走交互卡片（渲染层 build_*_card 的产物，形态对齐飞书项目机器人），
    是 str 走 markdown（决策回执那种一句话通知）。两种都支持，是为了让"改卡片形态"这件事
    不需要同时改动 decisions 的文案链路——那条链路的行为已被单测钉住。
    """
    ch = _channel
    if ch is None:
        logger.warning("[prd_watch] channel 未就绪，消息未发: %s", chat_id[:12])
        return
    if isinstance(payload, dict):
        await ch.send_card(chat_id, payload)
    else:
        await ch.send(chat_id, str(payload))



async def _prd_chat_members(chat_id: str):
    """群成员名单（easyprd 把 Story 上的负责人姓名换成可 @ 的 open_id）。

    SDK 自带 per-chat 缓存，重复调不打飞书；取不到就返回空列表，
    调用方退回 @姓名纯文本 —— 通知必须发得出去。
    """
    ch = _channel
    if ch is None or not chat_id:
        return []
    try:
        return await ch.get_chat_members(chat_id)
    except Exception as e:
        logger.warning("[prd_watch] 群成员名单失败(%s) → 退回 @姓名", str(e)[:100])
        return []


def _prd_record(result, stats) -> None:
    """把一次监听窗口的产出落成任务档案（与编排同格式，控制台可看）。"""
    from app.observability.task_record import TaskRecord, task_recorder

    task_recorder.write(TaskRecord(
        task_id=f"prdwatch-{result.story_id}-{int(stats.get('window_ts', time.time()))}",
        chat_id=result.chat_id,
        sender_id="",
        sender_name=f"easyprd 监听（story {result.story_id}）",
        query=str(stats.get("window_text") or "")[:300],
        final_agent="prd-watch-agent",
        route_chain=["prd_watch_bypass"],
        results_quality="ok" if not result.errors else "unclear",
        status="success" if not result.errors else "error",
        duration_ms=float(stats.get("duration_ms") or 0.0),
        response_preview=("| ".join(c.title for c in result.changes)
                          or f"无达阈变更（候选 {result.candidates}）")[:200],
        error="; ".join(result.errors)[:200],
        tokens_total=int(stats.get("tokens") or 0),
        cost_usd=float(stats.get("cost_usd") or 0.0),
        model=str(stats.get("model") or ""),
        llm_calls=1 if stats.get("candidates") is not None else 0,
        intent_summary=f"PRD 变更监听 · {result.summary()}",
        confidence=max((c.confidence for c in result.changes), default=0.0),
    ))


def _build_prd_watch(config, handler_registry):
    """组装 easyprd 服务（未启用也返回实例，active=False 走关闭态）。"""
    global _prd_watch
    from app.domains.prd_watch.service import PrdWatchService

    svc = PrdWatchService(config, send_card=_prd_send_card, record_hook=_prd_record,
                          member_resolver=_prd_chat_members)
    # 把共享 watcher / service 交给手动入口 Handler（同一窗口缓冲、同一 CR 台账、
    # 同一 metrics，不搞两套状态）。会议整理、需求分析、UI 对账都要 service。
    handler = handler_registry.get("prd-watch-agent") if handler_registry else None
    binder = getattr(handler, "bind_watcher", None)
    if callable(binder):
        # 启用了就给 watcher，不看 active：手动入口（analyze_story / 状态 /
        # 知识台账 / UI 对账）不需要群绑定，「不配群」不该连工具一起关掉。
        # 群旁路仍由 `_process_batch` 里的 `_prd_watch.active` 单独把关。
        binder(svc.watcher if config.prd_watch.enabled else None)
        service_binder = getattr(handler, "bind_service", None)
        if callable(service_binder):
            service_binder(svc)
    else:
        logger.warning("prd-watch-agent Handler 未注册（检查 prd_watch_handler.py 与 agent.yaml）")
    _prd_watch = svc
    return svc


async def _cmdb_send_card(target: str, card: object) -> None:
    """CMDB 提醒发卡（channel 晚绑定）。

    `target` 是 open_id（订阅者私聊直投）或 oc_ 群的 chat_id —— channel 侧按前缀
    自动选 receive_id_type，所以这里不做判断，原样透传。
    """
    ch = _channel
    if ch is None:
        logger.warning("[cmdb_watch] channel 未就绪，卡片未发: %s", target[:12])
        return
    if isinstance(card, dict):
        await ch.send_card(target, card)
    else:
        await ch.send(target, str(card))


def _cmdb_record(event: str, payload: dict) -> None:
    """CMDB 提醒落任务档案（与编排同格式，控制台任务档案里能看到发过哪些卡）。"""
    try:
        task_recorder.write(TaskRecord(
            task_id=f"cmdbwatch-{int(time.time() * 1000)}",
            chat_id=str(payload.get("chat_id") or ""),
            sender_id="",
            sender_name="CMDB 工单提醒（旁路）",
            query=str(payload.get("title") or "")[:300],
            final_agent="cmdb-watch-agent",
            route_chain=[f"cmdb_watch_{event}"],
            results_quality="ok",
            status="success",
            duration_ms=0.0,
            response_preview=str(payload.get("title") or "")[:200],
            intent_summary=f"CMDB 工单提醒 · {payload.get('title') or ''}",
        ))
    except Exception:
        logger.debug("[cmdb_watch] 任务档案写入失败", exc_info=True)


def _build_cmdb_watch(config, handler_registry=None):
    """组装 CMDB 工单提醒服务，并把 watcher 交给订阅入口 Handler。

    `enabled=false` 时返回 None：不建服务、不起循环、零网络开销。
    启用但还没人订阅时服务照建——订阅入口本身要能工作，第一轮扫描会跳过。
    """
    global _cmdb_watch
    cw = config.cmdb_watch
    if not cw.enabled:
        logger.info("CMDB 工单提醒未启用（enabled=false）")
        return None
    from app.domains.cmdb_watch.service import CmdbWatchService

    state_path = Path(cw.state_path)
    if not state_path.is_absolute():
        state_path = Path(__file__).resolve().parent.parent / state_path
    cw.state_path = str(state_path)
    _cmdb_watch = CmdbWatchService(config, send_card=_cmdb_send_card,
                                   record_hook=_cmdb_record)
    # 订阅入口 Handler 拿同一个 watcher（同一份订阅表与状态文件，不搞两套）
    handler = handler_registry.get("cmdb-watch-agent") if handler_registry else None
    binder = getattr(handler, "bind_watcher", None)
    if callable(binder):
        binder(_cmdb_watch.watcher)
    else:
        logger.warning("cmdb-watch-agent Handler 未注册"
                       "（检查 cmdb_watch_handler.py 与 agent.yaml）")
    return _cmdb_watch


# ── Globals ────────────────────────────────────────────────────

_memory_client: MemoryClient | None = None
_webportal_client: WebPortalClient | None = None
_cc_client: CCConnectClient | None = None
_registry_agent: AgentRegistry | None = None
_orchestrator: CrewOrchestrator | None = None
_aggregator: MessageAggregator | None = None
_health_monitor: AgentHealthMonitor | None = None
_edc_collector: EDCMetricsCollector | None = None
_daily_scheduler: DailyScheduler | None = None
_daily_subscription_manager: SubscriptionManager | None = None
_daily_sources = None  # 兼容占位：旧 DailySourceRegistry 已下线（日报单源化为 AI HOT）
_mcp_watcher_task: asyncio.Task | None = None
_memory_lifecycle: MemoryCoreLifecycle | None = None  # MemoryCore 进程托管（随启随停）
_channel: BotChannel | None = None
_messaging_cfg = None  # MessagingConfig，on_message 回执策略用（main 启动时注入）
_capability_ingest = None  # CapabilityIngestService，能力热插拔入口（enabled 时挂载）
_capability_handlers: list = []  # ApprovalCardHandler 列表（card_action 回调用）
_runtime_guard: RuntimeGuard | None = None  # 并发闸+排队+任务持久化（enabled 时挂载）
_scheduler_service: ScheduledTaskService | None = None  # 定时任务调度（enabled 时挂载）
_skill_catalog_store = None  # 团队 Skill 目录快照（handler 与启动预热共用）
_dsh_cfg = None  # DshConfig（执行面开关与模型/凭据来源）
_dsh_supervisor: DshSupervisor | None = None  # dsh 进程托管（enabled 且拉起成功时非空）
_dsh_gateway: DshGateway | None = None  # 飞书 ↔ dsh 会话桥（同上前提）
_prd_watch: Any = None  # easyprd 服务（PrdWatchService；未启用时为关闭态实例）
_cmdb_watch: Any = None  # CMDB 工单提醒服务（CmdbWatchService；未启用时 None）

# 用户显示名缓存：{open_id: display_name}，用于 sender_name 为空时回填
_user_name_cache: dict[str, str] = {}

# 附件下载临时目录（webportal-agent 透传）
_ATTACHMENT_DIR = "logs/attachments"

# fire-and-forget 任务引用池：防止后台任务被 GC 中途取消
_background_tasks: set[asyncio.Task] = set()

# dsh 不可用时的用户可见回执：说清发生了什么 + 下一步做什么（conventions §三）
_DSH_UNAVAILABLE_TEXT = (
    "⚠️ 执行服务暂时不可用，本轮已中断。已完成的步骤不会重复计费；"
    "请稍等一分钟重发同一条消息，无需做其他操作。"
)

# 项目根目录（runtime_guard persist_dir 相对路径解析用）
PROJECT_DIR = Path(__file__).resolve().parent.parent


async def _wait_channel(timeout_sec: float = 10.0) -> BotChannel | None:
    """等待全局 channel 就绪（启动早期 channel 尚未赋值时轮询等待）。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _channel is not None:
            return _channel
        await asyncio.sleep(0.2)
    return _channel


def get_memory_client() -> MemoryClient | None:
    return _memory_client


def get_webportal_client() -> WebPortalClient | None:
    return _webportal_client


def get_cc_client() -> CCConnectClient | None:
    return _cc_client


async def _download_attachments(message_obj) -> list[dict]:
    """从 InboundMessage 下载附件，返回 webportal attachment dict 列表。

    通过 channel 全局引用下载；channel 未就绪或下载失败时返回空（不阻断）。
    """
    if not message_obj:
        return []
    ch = _channel
    if not ch:
        return []
    try:
        return await ch.download_resources(message_obj, _ATTACHMENT_DIR)
    except Exception as e:
        logger.warning("Attachment download failed: %s", e)
        return []


async def _send_late_result(chat_id: str, text: str) -> None:
    """cc-connect 超时转后台后的迟到结果投递：直发原会话（channel.send）。

    由 crew._run_cc_connect_agent 的 on_reply 回调调用（chat_id 闭包捕获，
    不会错配到别的会话）。发送失败只记日志——结果已生成，重试无意义且
    后台守望已退出。
    """
    ch = _channel
    if ch is None or not chat_id:
        logger.warning("[late-delivery] channel/chat 未就绪，结果丢弃")
        return
    try:
        await ch.send(chat_id, text)
        logger.info("[late-delivery] 迟到结果已直投会话 %s (%d chars)", chat_id[:20], len(text))
    except Exception as e:
        logger.warning("[late-delivery] send failed: %s", e)


def _memory_session_key(chat_id: str) -> str:
    """记忆层的会话键：`feishu:{chat_id}`，会话维度；用户维度走 `user_id`。

    统一口径的理由：同一个群里两个人前后聊同一件事属于同一段对话上下文，
    把 sender_id 拼进 key 会把群聊切成一人一条互不相通的记忆线；而跨会话
    的用户画像本来就走 `user_id`，不需要在 key 里再带一次。
    """
    return f"feishu:{chat_id}"


def _dsh_progress_cb(emit) -> Any:
    """dsh 的事件回调在读取协程内同步调用，`emit` 是异步的（要发卡片）。

    这里只把事件投递成任务，不 await——回调里阻塞会把 dsh 的报文读取卡住。
    """
    def _on(stage: str, detail: str) -> None:
        task = asyncio.ensure_future(emit({"stage": stage, "detail": detail}))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return _on


def _progress_line(stage: str, detail: str) -> str | None:
    """进度事件 → 卡片上的一行文案；返回 None 表示这行不该展示。

    只用于旧编排链路（`_orchestrator`）的 stage 词表；dsh 链路的过程卡由
    `app/dsh/progress_card.py` 渲染（思考与动作按时间序交错，见 `app/dsh/timeline.py`）。
    详情**不能再往里塞工具名或原始 payload**——卡片是给用户看的过程感，不是调用日志。
    """
    if stage == "tool_start":
        return f"🔎 {detail}"
    if stage == "tool_done":
        return f"· {detail} ✓"
    if stage == "agent_error":
        # detail 已是「动作（原因）」形态，例如「获取市场信号（超时未响应）」
        return f"❌ {detail}"
    icons = {
        "route_start": "🧭", "route_done": "🧭", "dispatch": "📤",
        "agent_start": "🔄", "agent_done": "🎯",
    }
    icon = icons.get(stage)
    return f"{icon} {detail}" if icon else None


def _upsert_tool_line(events: list[str], action: str, line: str) -> None:
    """旧编排链路的过程行合并：同名动作原地更新状态，而不是每次事件各占一行。

    一次调用有 started / completed 两帧，失败重试还有更多帧；逐帧追加的话
    3 次重试就吃掉一整屏。失败行保留（用户要知道哪一步没成），重试的新
    started 不覆盖错误，而是另起一行。
    """
    for index in range(len(events) - 1, -1, -1):
        text = events[index]
        if not isinstance(text, str) or not action or action not in text:
            continue
        if text.startswith("❌") and not line.startswith("❌"):
            break  # 重试开始：错误行留着，新的一行另起
        events[index] = line
        return
    events.append(line)


def _legacy_timeline(events: list[str]) -> Timeline:
    """旧编排链路的逐行文案 → 时间线（回切链路没有 thought 帧）。

    那些 line 自带图标前缀（🔎 / · / ❌），直接当思考正文会串味；这里按前缀
    还原成「动作」块：`· x ✓` 视为完成、`❌ x` 视为失败、其余视为进行中。
    解析不出动作的行（route/dispatch 之类的路由话术）当普通文本块保留。
    """
    timeline = Timeline()
    for raw in events:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("🔎 "):
            timeline.observe("tool_start", text[2:])
        elif text.startswith("· ") and text.endswith(" ✓"):
            timeline.observe("tool_done", text[2:-2])
        elif text.startswith("❌ "):
            timeline.observe("agent_error", text[2:])
        else:
            timeline.add_thought(text)
    return timeline


async def resolve_sender_name(open_id: str, channel: BotChannel, chat_id: str) -> str:
    """通过飞书聊天成员名单解析 sender 显示名。
    优先查本地缓存；未命中时调用 channel.get_chat_members() 拉取群成员名单。
    结果缓存到本地 dict，避免每次请求都打飞书 API。
    """
    if not open_id:
        return ""
    if open_id in _user_name_cache:
        return _user_name_cache[open_id]

    name = ""
    try:
        members = await channel.get_chat_members(chat_id)
        for m in members:
            if m.id == open_id and m.name:
                name = m.name
                break
    except Exception:
        pass

    if name:
        _user_name_cache[open_id] = name
    return name


# ── Main Message Handler ───────────────────────────────────────


async def _text_with_quote(msg: InboundMessage, channel: BotChannel) -> str:
    """本条正文 + 被引用消息正文。引用拉取失败不阻断，只保留本条。"""
    from app.gateway.aggregator import extract_text

    own = extract_text(msg)
    parent_id = _parent_message_id(msg)
    if not parent_id or "（引用的上一条：" in own:
        return own
    quoted = await _fetch_quoted_text(channel, parent_id)
    if not quoted:
        # 引用没拉到 = 路由只看到短指令，工单正文全丢——串单事故的直接前兆，
        # 落 info 级便于事后在日志里检索
        logger.info("quoted text empty: parent=%s msg=%s", parent_id, msg.id)
        return own
    if quoted in own:
        return own
    if own:
        return f"{own}\n（引用的上一条：{quoted}）"
    return f"（引用的上一条：{quoted}）"


def _parent_message_id(msg: InboundMessage) -> str:
    """被引用消息 id：ReplyRef 优先，raw.parent_id 兜底。

    lark_channel 只在 `parent_id != root_id` 时建 ReplyRef——用户「回复」
    话题根消息（截图形态：引用工单正文那条，parent==root）reply 为 None，
    只认 reply 会整条丢掉引用（曾导致串单）。
    """
    reply = getattr(msg, "reply", None)
    pid = str(getattr(reply, "message_id", "") or "") if reply else ""
    if pid:
        return pid
    raw = getattr(msg, "raw", None)
    if isinstance(raw, dict):
        pid = str(raw.get("parent_id") or "")
    return pid


async def _fetch_quoted_text(channel: BotChannel, message_id: str) -> str:
    """拉被引用消息的正文。飞书 get 失败返回空串。"""
    inner = getattr(channel, "_channel", None)
    fetch = getattr(inner, "fetch_quoted_context", None)
    if fetch is None:
        return ""
    try:
        quoted = await fetch(message_id)
    except Exception:
        logger.debug("quoted message fetch failed: %s", message_id, exc_info=True)
        return ""
    text = getattr(quoted, "text", None) if quoted else None
    return str(text or "").strip()[:1500]


async def on_message(msg: InboundMessage, channel: BotChannel):
    """消息入口 — 连发消息先经聚合器 debounce 合并，再统一进编排管线。

    Pipeline（_process_batch 内）:
    1. Create TraceContext (trace_id + span_id for chain tracking)
    2. Execute via DshGateway（dsh 未启用则回落 CrewOrchestrator）
    3. Routed-contextualized receipt (direct/help 秒回，专业 agent 带去向回执)
    4. Capture conversation round to L0 memory
    5. Trace observability
    """
    text = await _text_with_quote(msg, channel)
    # 引用正文是异步拉的，而 aggregator worker 合并时只跑同步 extract_text——
    # 不挂回去，算好的引用正文会被丢掉（串单事故的根因之一）。
    # 无 aggregator 的直发路径本来就用 text，挂上也无害。
    try:
        # 动态挂字段：InboundMessage 是外部包 dataclass（无 slots），本仓不能声明该字段
        object.__setattr__(msg, "_prefetched_text", text)
    except Exception:
        logger.debug("prefetch stash failed", exc_info=True)
    chat_id = msg.conversation.chat_id
    sender_id = msg.sender.open_id
    sender_name = msg.sender.display_name or ""

    # ── 能力热插拔分流：带文件附件的消息先交给 CapabilityIngestService ──
    # （仅 [projects.capability].enabled=true 时挂载；非能力包附件不影响
    #  正常编排——is_capability_package 只认 zip/yaml/md 文件名）
    if _capability_ingest is not None and _capability_ingest.is_capability_package(msg):
        asyncio.get_running_loop().create_task(
            _handle_capability_package(msg, channel)
        )
        return
    # sender_name 为空时回填（群聊消息可能不带 display_name）
    if not sender_name and sender_id:
        sender_name = await resolve_sender_name(sender_id, channel, chat_id)

    # ── easyprd 修正回填：点过「修正」的人在等一句新表述 ──
    # 必须排在监听旁路之前：旁路会把授权群的非@消息整条吃掉（只进讨论窗口），
    # 排在后面就等于「点了修正、照提示回复，结果提示被当成新讨论吞掉」。
    # 判据同卡片回调：自测台在 active=false 时也能点，这里就得同样放开。
    if _prd_watch is not None:
        fix_text = await _apply_prd_fix(chat_id, sender_id, text)
        if fix_text:
            await _reply_text_to_chat(chat_id, fix_text)
            return

    # ── easyprd 文字指令：`采纳 B1` / `忽略 B1` / `有用 B1` / `判错 B1` ──
    # 转发来的卡片按钮点不动（飞书当只读），编号是唯一还能用的入口。
    # 与「修正回填」同层：同样要抢在监听旁路之前，否则授权群里的指令会被当讨论吞掉。
    if _prd_watch is not None:
        cmd_text = await _apply_prd_command(chat_id, sender_id, text)
        if cmd_text:
            await _reply_text_to_chat(chat_id, cmd_text)
            return

    # ── easyprd 自测台：`/easyprd demo <文档链接>` 发一张带真实按钮的卡片 ──
    # 按钮只有飞书客户端点得到，而真实监听要等群↔Story 绑定配好才有合法入口发卡；
    # 自测台把「素材从哪来」换成固定样例，锚点/卡片/按钮/评论写入全走生产路径。
    demo_text = await _prd_demo_command(chat_id, sender_id, text)
    if demo_text:
        await _reply_text_to_chat(chat_id, demo_text)
        return

    # ── easyprd 旁路：授权群里的「非@消息」只进监听，绝不进编排 ──
    # 这些群放开了 require_mention（见 BotChannel no_mention_chats），消息第一次
    # 会到达本进程；不在此截断就会把同事闲聊送去路由模型作答（群变广播喇叭）。
    # 两个例外必须放行：@ 了机器人的消息（照常走编排，不双处理），
    # 以及 `/` 开头的命令（/dev /help 等由 _process_batch 的 Step 0.2 拦截，
    # 旁路跑在它前面，截了就把管理员命令吞掉）。
    if (_prd_watch is not None and _prd_watch.active
            and not getattr(msg, "mentioned_bot", False)
            and not (text or "").lstrip().startswith("/")
            and chat_id in _prd_watch.watcher.watch_chats()):
        try:
            await _prd_watch.watcher.handle_message(
                chat_id=chat_id, sender_id=sender_id,
                sender_name=sender_name, text=text)
        except Exception:
            logger.debug("[prd_watch] 旁路投递失败", exc_info=True)
        return

    if _aggregator:
        async def _process(merged_text: str, last_msg, merged_count: int):
            await _process_batch(
                merged_text, last_msg, merged_count, channel, sender_name
            )

        _aggregator.submit(msg, _process)
    else:
        # 聚合器关闭时保持旧行为：每条消息独立后台处理
        asyncio.get_running_loop().create_task(
            _process_batch(text, msg, 1, channel, sender_name)
        )


async def _handle_capability_package(msg: InboundMessage, channel: BotChannel) -> None:
    """能力包接入处理（管理员发 zip/agent.yaml/SKILL.md 触发）。"""
    if _capability_ingest is None:
        return
    try:
        await _capability_ingest.handle_package_message(msg)
    except Exception as e:
        logger.exception("Capability package handling failed: %s", e)
        try:
            await channel.reply(msg, f"⚠️ 能力包处理出错：{str(e)[:120]}")
        except Exception:
            pass


# ── Capability callbacks（main 启动时注入 CapabilityIngestService/ApprovalCardHandler）──


async def _send_capability_card(chat_id: str, card: dict):
    """发送审批卡片，返回 message_id（card_action 回调更新卡片用）。"""
    ch = _channel
    if ch is None:
        return None
    result = await ch.send_card(chat_id, card)
    return getattr(result, "message_id", None) or ""


async def _reply_text_via_msg(msg: InboundMessage, text: str) -> None:
    """经消息对象回复（调度服务创建入口回执用）。"""
    ch = _channel
    if ch is None:
        return
    try:
        await ch.reply(msg, text)
    except Exception:
        logger.debug("Scheduler reply failed", exc_info=True)


async def _update_capability_card(message_id: str, card: dict) -> None:
    ch = _channel
    if ch is None or not message_id:
        return
    try:
        await ch.update_card(message_id, card)
    except Exception:
        logger.warning("Capability card update failed: %s", message_id, exc_info=True)


async def _reply_capability(msg: InboundMessage, text: str) -> None:
    ch = _channel
    if ch is None:
        return
    try:
        await ch.reply(msg, text)
    except Exception:
        logger.debug("Capability reply failed", exc_info=True)


async def _resolve_approver_name(open_id: str, chat_id: str) -> str:
    """审批人显示名解析（复用 resolve_sender_name 的成员名单 + 缓存）。"""
    ch = _channel
    if ch is None:
        return ""
    return await resolve_sender_name(open_id, ch, chat_id)


async def _download_capability_resource(msg: InboundMessage, resource, dest_dir):
    """下载单个能力包附件（经 channel 封装，不做扩展名白名单限制）。"""
    ch = _channel
    if ch is None:
        return None
    return await ch.download_single_resource(msg, resource, dest_dir)


async def _on_capability_card_action(event) -> None:
    """card_action 事件分发：转发给已注册的 ApprovalCardHandler。"""
    for handler in _capability_handlers:
        try:
            await handler.handle(event, reply_callback=_reply_text_to_chat)
        except Exception:
            logger.exception("Capability card action handling failed")


async def _on_pm_card_action(event) -> None:
    """PM 催办卡片回调：转给 `pm_run` 的 9113 端点处理。

    为什么主链代收：草稿状态在 `pm_run` 的进程内存里（`pm_nudge._PENDING`），
    主链拿不到也不该拿。而飞书卡片回调只走应用长连接，主链又是唯一持有那条
    连接的一方——所以主链只做搬运，权威状态留在它出生的进程。

    非催办卡片的点击静默让过（value 里没有 `pn` 键），不打扰其它 handler。
    """
    from app.domains.pm import pm_card_server

    action = getattr(event, "action", None)
    value = getattr(action, "value", None) if action is not None else None
    if not isinstance(value, dict) or "pn" not in value:
        return

    import httpx

    payload = _card_event_payload(event)
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                pm_card_server.url(),
                json=payload,
                timeout=30.0,
            )
    except Exception:
        # 转发失败要让点按钮的人知道：卡片回调静默失败看起来就是「点了没反应」
        logger.debug("[pm] 卡片回调转发失败", exc_info=True)
        chat_id = str(getattr(event, "chat_id", "") or "")
        if chat_id:
            await _reply_text_to_chat(
                chat_id, "催办按钮没连上 PM 守护进程——用文字回「确认」或「取消」也行。")


def _card_event_payload(event: Any) -> dict:
    """把 lark_channel 的卡片事件摊成 dict，供 pm_run 的 HTTP 端点解析。

    字段名与飞书原始事件体一致（`action.value` / `operator.open_id` /
    `context.open_chat_id`），这样 pm_run 那边既能接主链转发，也不必为
    将来直连飞书另写一套解析。
    """
    def _get(obj: Any, name: str, default: Any = "") -> Any:
        return getattr(obj, name, default) if obj is not None else default

    action = _get(event, "action", None)
    operator = _get(event, "operator", None)
    return {
        "action": {"value": _get(action, "value", {}) or {}},
        "operator": {"open_id": _get(operator, "open_id", "")},
        "context": {
            "open_chat_id": _get(event, "chat_id", ""),
            "open_message_id": _get(event, "message_id", ""),
        },
    }


async def _on_prd_card_action(event) -> None:
    """easyprd 卡片按钮回调：采纳 / 修正 / 忽略（不是本卡片的按钮就静默返回）。

    结果回给点按者所在的会话；回执失败不冒泡（卡片回调抛异常会弹"操作失败"，
    但状态可能已经落库了，用户会以为没生效而重复点）。
    """
    svc = _prd_watch
    if svc is None:
        return
    try:
        # 三套按钮走同一个回调：变更卡片的 cr/act、分析卡片的 sid/rel、
        # 分析意见的 cr/rate（🙂/🙃 评分）。各自自证身份（非本类按钮返回空串），
        # 不用猜是哪张卡。
        text = await svc.card_actions.handle(event)
        if not text:
            text = await svc.card_actions.handle_rating(event)
        if not text:
            text = await svc.card_actions.handle_related(event)
    except Exception:
        logger.exception("[prd_watch] 卡片按钮处理失败")
        return
    if not text:
        return
    chat_id = str(getattr(event, "chat_id", "") or "")
    if chat_id:
        await _reply_text_to_chat(chat_id, text)


async def _prd_demo_command(chat_id: str, sender_id: str, text: str) -> str:
    """`/easyprd demo <PRD 链接或 token>` → 发一张自测卡片；不是该命令返回空串。

    只对 L2 管理员开放：卡片按钮会真往文档里写评论，等级不够的人不该有这条入口。
    """
    body = (text or "").strip()
    if not body.lower().startswith("/easyprd"):
        return ""
    svc = _prd_watch
    actions = getattr(svc, "card_actions", None)
    if svc is None or actions is None:
        return "⚠️ easyprd 未装配（bot 未启用该能力），自测台不可用。"
    from app.orchestrator import get_user_permission

    if get_user_permission(sender_id, chat_id).level < 2:
        return "🔒 自测台会真往 PRD 写评论，只对管理员开放。"
    parts = body.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].lower().startswith("demo"):
        return ("用法：`/easyprd demo <PRD 文档链接或 docx token>`\n"
                "会在这个会话里发一张带「采纳/修正/忽略」按钮的自测卡片，"
                "文档用你给的那篇。")
    target = parts[2].strip()
    token = target
    prd_url = target if "://" in target else ""
    if prd_url:
        try:
            from app.protocols.lark_cli import LarkCli

            info = await LarkCli().inspect_url(target, identity="user")
            token = str(info.get("obj_token") or info.get("token") or target)
        except Exception as e:
            return f"⚠️ 解析文档链接失败：{str(e)[:100]}"
    from app.domains.prd_watch.demo import run_demo

    try:
        card, points, _binding = await run_demo(
            token, chat_id=chat_id, ledger=svc.watcher._ledger,
            config=svc.watcher._cfg, prd_url=prd_url)
    except Exception as e:
        logger.exception("[prd_watch] 自测卡片生成失败")
        return f"⚠️ 自测卡片生成失败：{str(e)[:120]}"
    ch = _channel
    if ch is None:
        return "⚠️ channel 未就绪，卡片没发出去。"
    await ch.send_card(chat_id, card)
    anchored = sum(1 for p in points if p.located)
    return (f"🧪 自测卡片已发：{len(points)} 条，锚定到原文 {anchored} 条。\n"
            "点卡片上的按钮即走真实链路（采纳/忽略回写 PRD 评论；修正会先让你回一条新表述）。")


async def _apply_prd_fix(chat_id: str, sender_id: str, text: str) -> str:
    """点过「修正」的人补上文案 → 落新评论并结掉这条 CR；非待修正状态返回空串。

    待修正状态只认「点按钮的那个 open_id」，群里别人接着聊不会被当成修正文案。
    判定**不带会话**：卡片可能被转发到别的群/私聊，转发后这条回复的 chat_id
    与点按钮时不同，带上就永远对不上（见 `CardActionHandler._pending_fix`）。
    """
    svc = _prd_watch
    actions = getattr(svc, "card_actions", None)
    if actions is None or not chat_id or not sender_id or not (text or "").strip():
        return ""
    if not actions.pending_fix(sender_id):
        return ""
    try:
        return await actions.apply_fix(chat_id, sender_id, text)
    except Exception:
        logger.exception("[prd_watch] 修正回填失败")
        return "⚠️ 修正处理出错，请再点一次「修正」重试。"


async def _apply_prd_command(chat_id: str, sender_id: str, text: str) -> str:
    """`采纳 B1` / `忽略 B1` / `有用 B1` / `判错 B1` → 与点按钮同一套副作用。

    为什么需要：飞书把**转发来的消息**当只读，卡片按钮点了只弹「这是转发消息，
    无法直接操作」。卡片没法改这个客户端语义，但编号是可见的——人把这句指令
    抄回自己的会话，就等于按了一次按钮。

    只认极短一句（`parse_plain_command`），判宽了会把群里正常讨论当成指令。
    """
    svc = _prd_watch
    actions = getattr(svc, "card_actions", None)
    if actions is None or not sender_id or not (text or "").strip():
        return ""
    try:
        return await actions.handle_command(text, open_id=sender_id)
    except Exception:
        logger.exception("[prd_watch] 文字指令处理失败")
        return "⚠️ 指令处理出错，请稍后重试。"


async def _reply_text_to_chat(chat_id: str, text: str) -> None:
    ch = _channel
    if ch is None:
        return
    try:
        await ch.send(chat_id, text)
    except Exception:
        logger.debug("Capability chat reply failed", exc_info=True)


def _dev_project_of(sender_id: str) -> str:
    """`/dev` 该投给哪个 cc-connect 项目。

    dev-bot = 平台维护者，全仓可改；pm-dev = PM 受限通道，只能改 PM 相关代码。

    判据是**身份**不是等级：两个都是 L2，等级分不开。`[projects.pm.identity].open_id`
    就是 PM 本人的 open_id，直接复用——再配一份「谁是 PM」会出现两处不一致。
    读不到配置就退 dev-bot（宽松侧），因为受限侧会静默地把人挡在门外。
    """
    try:
        cfg = load_config()
        pm = getattr(cfg, "pm", None)
        pm_open_id = (getattr(getattr(pm, "identity", None), "open_id", "") or "").strip()
        if pm_open_id and sender_id == pm_open_id:
            return "pm-dev"
    except Exception:
        logger.debug("dev project resolve failed", exc_info=True)
    return "dev-bot"


def _pm_owns_message(msg: InboundMessage, text: str) -> bool:
    """这条消息该不该由 PM 守护进程独占（主链让路）。

    PM 本人私聊：整个让。「PM 本人」用 `[projects.pm.identity].open_id` 判定，
    不用 `names`——名字会重、会改，open_id 不会。

    这里刻意**不判断** daemon 有没有真的收到：它 3 秒轮询一次，同一条消息
    必然被它捞到；主链让路是消除重复，不是保证送达。
    """
    try:
        cfg = load_config()
        pm = getattr(cfg, "pm", None)
        if pm is None or not getattr(pm, "enabled", False):
            return False
        pm_open_id = (getattr(getattr(pm, "identity", None), "open_id", "") or "").strip()
        convo = getattr(msg, "conversation", None)
        chat_type = (getattr(convo, "chat_type", "") or "").lower()
        # `/dev` 例外：那是 Claude Code 开发通道，只有主链有 cc-connect 客户端，
        # daemon 接走只会回一句「不认识这个命令」。所以私聊里也不让。
        if (text or "").lstrip().startswith("/dev"):
            return False
        if chat_type == "p2p" and pm_open_id and msg.sender.open_id == pm_open_id:
            return True
        group_id = (getattr(getattr(pm, "push", None), "group_id", "") or "").strip()
        if group_id and getattr(convo, "chat_id", "") == group_id:
            return (text or "").lstrip().startswith("/pm")
        return False
    except Exception:
        logger.debug("pm ownership check failed", exc_info=True)
        return False


def _pm_daemon_alive() -> bool:
    """PM 守护进程（`python -m app.domains.pm.pm_run`）是否在这台机器上跑着。

    为什么要在主链判断：`/pm` 指令两个进程都接得到 —— daemon 轮询私聊自己捞，
    主链也收得到同一条。谁答得看 daemon 在不在，不能让主链无条件抢或无条件让。

    判据是命令行里的 `app.domains.pm.pm_run`。只在自己机器上有效，但两个进程
    本来就只能跑在同一台机器（同一个飞书应用的 WS 与同一个私聊会话）。
    探测失败一律当「不在」：宁可主链多答一句，也不要指令石沉大海。
    """
    try:
        if os.name != "nt":
            out = subprocess.run(["pgrep", "-f", "app.domains.pm.pm_run"],
                                 capture_output=True, text=True, timeout=3)
            return out.returncode == 0
        # wmic 的输出按系统代码页编码（中文机器是 GBK），按 UTF-8 解码会抛
        # UnicodeDecodeError 被 except 吞掉，结果恒为「没在跑」——静默失效。
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=6, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "app.domains.pm.pm_run" in (out.stdout or "")
    except Exception:
        logger.debug("pm daemon probe failed", exc_info=True)
        return False


async def _process_batch(
    text: str,
    msg: InboundMessage,
    merged_count: int,
    channel: BotChannel,
    sender_name: str,
):
    """处理一批（可能由多条连发合并的）消息。

    回执策略（[projects.messaging].ack_receipt 开关控制表情回执）：
    - 收到即回执：给原消息打 ⌨️ 表情（零噪音）
    - 全程处理链路（意图判定→分发→执行→完成）由进度卡实时展示，
      旧「已接收 N 条/正在交给 XX」文案回执已并入进度卡，不再发文本
    - direct_answer / help：进度卡可能一闪而过，秒回结果不受影响
    """
    chat_id = msg.conversation.chat_id
    sender_id = msg.sender.open_id
    start_time = time.time()

    # ── Step -1: 收到即回执（fail-safe：回执失败不阻断主流程）──
    # 豆包式轻回执：只给原消息打 ⌨️ 表情——「已接收/正在处理/正在交给 XX」等
    # 文案回执已由进度卡承接（全链路思考过程第一跳），不再重复发文本噪音。
    if _messaging_cfg is None or _messaging_cfg.ack_receipt:
        try:
            await channel.react(msg, "Typing")
        except Exception:
            logger.debug("Ack reaction failed", exc_info=True)

    # ── Step 0: TraceContext（task_id 复用 trace_id，串起全部观测日志）──
    trace_ctx = TraceContext.new(chat_id, sender_id, sender_name)

    # ── Step 0.5: 运行时护栏 — 申请并发额度（超限排队，队满/超时快速失败）──
    # enabled=false 时 _runtime_guard 为 None，行为与旧版一致（无并发闸）。
    lifecycle: TaskLifecycleRecord | None = None
    if _runtime_guard is not None:
        lifecycle = TaskLifecycleRecord(
            task_id=trace_ctx.trace_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            query_preview=text[:50],
        )
        acquired, reject_msg = await _runtime_guard.start_task(lifecycle)
        if not acquired:
            logger.warning(
                f"[{trace_ctx.trace_id}] RuntimeGuard rejected: {lifecycle.outcome}"
            )
            try:
                await channel.reply(msg, reject_msg or "😵 系统繁忙，请稍后再试")
            except Exception:
                logger.debug("Busy reply failed", exc_info=True)
            return

    # ── Step 0.15: 让路给 PM 守护进程 ──
    # 两个进程连的是**同一个飞书应用**，`app.domains.pm.pm_run` 自己轮询私聊把
    # 指令捞走并回复，主链再答一遍就是「同一句话回两次」，用户看到两条不同答案。
    #
    # 让路范围按「谁的场子」划，不是按命令前缀划：
    #   · PM 本人私聊 —— 整个让（`/dev` 除外，见 `_pm_owns_message`）。那是 daemon
    #     的产品面（推送和接收全走私聊），问排期、确认催办、`/pm 授权` 都得由它答，
    #     主链答了也拿不到它的待确认状态。
    #   · 群里 —— 不让。群聊是 app.main 的地盘；daemon 的群通道只在
    #     `[projects.pm.push].group_id` 配了才开，没配时让出去就没人应答了。
    #     （例外：配了群且是 `/pm*`，两边都接得到，让给 daemon。）
    #
    # daemon 没在跑时一律不让：宁可多答一句，也不要指令石沉大海。
    # 顺序不能反：「这条归不归 PM」是一次内存比较，而探测 daemon 要起子进程
    # （实测 0.3s）。先判归属，绝大多数消息根本不会去探测。
    if _pm_owns_message(msg, text) and _pm_daemon_alive():
        logger.info("[pm] 这条归 PM 守护进程，主链不重复应答: %s", (text or "")[:40])
        return

    # ── Step 0.2: /dev 直达通道 — 管理员专属远程开发（dev-bot/Claude Code）──
    # dev-bot 的 routable=false（不参与 LLM 路由，见 agent.yaml）：
    # 团队消息不再消耗 Claude Code 会话；维护者用 /dev 前缀显式直达，
    # 不经路由 LLM，直接投递 cc-connect dev-bot 项目并等待回复。
    if text.startswith("/dev"):
        await _handle_dev_channel(
            text, msg, channel, chat_id, sender_id, sender_name,
            trace_ctx, lifecycle,
        )
        return

    # ── Step 0: 负反馈检测 — 短文本不满表达回填到最近一条任务档案 ──
    # 不拦截消息：判定后照常进编排流程（用户可能边吐槽边补充需求）
    fb = detect_negative_feedback(text)
    if fb:
        try:
            attached = task_recorder.attach_feedback(chat_id, sender_id, fb)
            if attached:
                logger.info(
                    "[feedback] negative feedback captured from %s: %s",
                    sender_id, fb.get("matched"),
                )
        except Exception:
            logger.debug("Feedback attach failed, continuing")

    # ── Step 1: Processing batch log（trace_ctx 已在 Step 0 创建）──
    logger.info(
        f"[{trace_ctx.trace_id}] Processing batch ({merged_count} msg) from {sender_id}"
    )

    memory_client = get_memory_client()

    # ── Step 2: Memory recall ──
    # 召回结果要**真的进 prompt**：后台 call 一次就把返回值丢掉，等于每轮白花
    # 一次网关往返，跨会话记忆永远不生效。所以先取文本、执行时拼在本轮正文前
    # （dsh 的 `gateway.handle(memory=...)`，不进会话历史）。
    # 3s 上限与旧编排链路同口径：MemoryCore 不可达时实测阻塞 2s+，宁可没有记忆
    # 也不能让每条消息先等三秒。
    memory_context = ""
    if memory_client and memory_client.config.enabled:
        try:
            memory_context = await asyncio.wait_for(
                memory_client.recall(
                    query=text,
                    session_key=_memory_session_key(chat_id),
                    user_id=sender_id,
                ),
                timeout=3.0,
            )
            if memory_context:
                logger.info(
                    f"[{trace_ctx.trace_id}] Memory context injected ({len(memory_context)} chars)"
                )
        except Exception as e:
            logger.debug("Memory recall skipped: %s", e)

    try:
        # ── 过程卡：思考 + 调用动作按时间序缝成一条，收尾时答案写进同一张卡 ──
        # 同一张卡片原地刷新：运行态是展开的过程面板，完成态面板收起、答案接在下面。
        # 只发一条消息是用户的硬要求——答案再单独发文本，会话里就是两条。
        # 节流 2s 合并过程帧（飞书编辑频率限制）；direct/help/clarify 秒回链路不发卡
        # （表情回执+直接回复足够，卡反而显得郑重其事）。
        _progress_state: dict[str, Any] = {"card_id": "", "last_ts": 0.0,
                                           "cancelled": False, "done": False,
                                           "started_at": time.monotonic(),
                                           "timeline": Timeline()}
        _tool_names: list[str] = []  # 本轮调用过的工具（任务档案 route_chain 数据源）
        # 旧编排链路的逐行文案：那条链路没有 thought 帧，事件就是它的全部过程。
        _legacy_events: list[str] = []

        # 串行整卡推送：`_on_progress` 每帧都投成独立任务，两帧几乎同时到达时
        # 会双双看到「还没有 card_id」而各发一张卡——那一轮就变成一条永远停在
        # 「正在处理」的孤儿卡 + 一条真正被刷新的卡。检查与发送必须在同一把锁里。
        _card_lock = asyncio.Lock()

        async def _push_card(force: bool):
            # 锁的获取也在 try 里：刷新失败只能让「过程卡」降级，绝不能把
            # 已经拿到的答案一起丢掉——答案由调用方在卡片发不出去时改发文本。
            try:
                async with _card_lock:
                    if not force and time.monotonic() - _progress_state["last_ts"] < _PROGRESS_THROTTLE_SEC:
                        return  # 节流：终态事件（force=True）必刷
                    _progress_state["last_ts"] = time.monotonic()

                    if _progress_state["done"]:
                        card = _done_card(_progress_state, _legacy_events)
                    else:
                        card = _running_card(_progress_state, _legacy_events)
                    if _progress_state["card_id"]:
                        await channel.update_card(_progress_state["card_id"], card)
                    else:
                        result = await channel.send_card(chat_id, card)
                        _progress_state["card_id"] = getattr(result, "message_id", "") or ""
            except Exception as e:
                # 进度卡是「让用户知道还在动」的唯一手段，静默失败就等于
                # 长任务表现为「发了消息没反应」——升 warning 并放弃后续推送，
                # 把呈现让给最终回复（文本仍会发）。
                logger.warning("Progress card failed (%s: %s) — 停止后续推送，只发最终回复",
                               "update" if _progress_state["card_id"] else "send",
                               str(e)[:80])
                _progress_state["cancelled"] = True

        async def _heartbeat():
            """进度卡节拍器：每 5s 把「已处理 Ns · 预估」重画一遍。

            用户要的是**实时**读数，不是「有事件才动」：一次工具调用跑两分钟，
            被动刷新下那行耗时就是冻的，看起来像卡死。这里只补计时，不追加内容，
            `_push_card(force=False)` 仍按节流间隔放行，事件密集时不会加倍刷新。
            """
            while not _progress_state["done"] and not _progress_state.get("cancelled"):
                await asyncio.sleep(_PROGRESS_TICK_SEC)
                if _progress_state["done"] or not _progress_state["card_id"]:
                    continue
                await _push_card(False)

        async def _on_progress(event: dict):
            stage = str(event.get("stage", ""))
            detail = str(event.get("detail", ""))
            if stage == "tool_start" and detail:
                # 档案里的 route_chain 靠它（dsh 没有「路由到某 Agent」这一步，
                # 能力体现在实际调了哪些工具上）
                _tool_names.append(detail)
            if _progress_state["done"]:
                return  # 已刷完成态，不再追加事件
            # 秒回链路（direct/help/clarify）取消首卡：若唯一已入列的事件只有
            # route_start（「正在阅读你的消息」），说明链路对用户无过程价值，
            # 清空并标记取消——闲聊只有表情回执+直接回复，两跳卡是噪音。
            # 注意 route_start 可能因节流尚未真正发卡，取消窗口覆盖整个首跳。
            if event.get("simple") and not _progress_state["card_id"]:
                _progress_state["cancelled"] = True
                return
            if _progress_state.get("cancelled"):
                return  # 已取消的卡不再复活

            timeline: Timeline = _progress_state["timeline"]
            if timeline.observe(stage, detail):
                force = stage in ("agent_done", "tool_done", "route_done")
                await _push_card(force)
                return
            line = _progress_line(stage, detail)
            if line is None:
                return
            if stage in ("tool_start", "tool_done", "agent_error"):
                _upsert_tool_line(_legacy_events, detail, line)
            elif event.get("replace_opening") and _legacy_events:
                _legacy_events[0] = line
            else:
                _legacy_events.append(line)
            await _push_card(stage in ("agent_done", "tool_done", "route_done"))

        def _running_card(state: dict, legacy: list[str]) -> dict:
            elapsed = time.monotonic() - state["started_at"]
            timeline: Timeline = state["timeline"]
            if timeline.has_content():
                return build_running_card(text, timeline, elapsed)
            # 旧编排链路（或 dsh 还没吐任何过程）：退回逐行文案
            return build_running_card(text, _legacy_timeline(legacy), elapsed)

        def _done_card(state: dict, legacy: list[str]) -> dict:
            timeline: Timeline = state["timeline"]
            if not timeline.has_content():
                timeline = _legacy_timeline(legacy)
            return build_done_card(text, timeline, state.get("answer", ""),
                                   task_id=trace_ctx.trace_id)

        # ── Step 3: 后台执行（dsh 会话桥优先，旧编排链路保留可回切）──
        async def _run_orchestrator():
            """后台编排：执行 → 补发正式回复 → 历史/审计/记忆/可观测。"""
            resp = ""
            notice = ""
            try:
                if _dsh_gateway is not None:
                    # 多轮上下文由 dsh 会话承担（同 chat 常驻），本侧不再拼历史。
                    # 多媒体：图片走 ACP image 块，文档解析成文本随本轮提交；
                    # 下载失败只降级成 note，不阻断主链（与旧编排链路同口径）。
                    media_bundle = None
                    if getattr(msg, "resources", None):
                        try:
                            from app.multimodal import download_media_bundle

                            media_bundle = await download_media_bundle(channel, msg)
                        except Exception:
                            logger.debug("Media bundle build failed", exc_info=True)
                    logger.info(f"[{trace_ctx.trace_id}] 交 dsh 执行（chat={chat_id[:12]}）")
                    try:
                        resp = await _dsh_gateway.handle(
                            text, chat_id,
                            on_event=_dsh_progress_cb(_on_progress),
                            media=media_bundle,
                            memory=memory_context,
                            # 身份在建会话时固化进 MCP 子进程 env：订阅/关注类
                            # 工具靠它绑到真实发送者，模型口述的身份不采信。
                            caller={"sender_id": sender_id,
                                    "sender_name": sender_name},
                        )
                    except DshUnavailableError as e:
                        logger.warning(f"[{trace_ctx.trace_id}] dsh 不可用，本轮中断: {e}")
                        notice = _DSH_UNAVAILABLE_TEXT
                    finally:
                        # 档案写在这里而不是 Step 6：回切链路走编排器时那边自己落，
                        # 两条链路各写一条才不会重复计数。
                        turn = getattr(_dsh_gateway, "last_turn", None)
                        record_dsh_task(
                            task_id=trace_ctx.trace_id,
                            chat_id=chat_id,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            query=text,
                            reply=resp,
                            tool_names=_tool_names,
                            duration_ms=(time.time() - start_time) * 1000,
                            merged_count=merged_count,
                            memory=memory_context,
                            # 工具步骤只在内存里活一轮，这里不取就永久丢失
                            steps=turn.step_list() if turn is not None else None,
                        )
                elif _orchestrator:
                    # 多模态：下载消息里的图片/视频/文档，构造媒体包注入编排
                    # （失败降级为 note，不阻断主链）
                    media_bundle = None
                    if getattr(msg, "resources", None):
                        try:
                            from app.multimodal import download_media_bundle

                            media_bundle = await download_media_bundle(channel, msg)
                        except Exception:
                            logger.debug("Media bundle build failed", exc_info=True)
                    resp = await _orchestrator.execute(
                        message=text,
                        chat_id=chat_id,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        trace_id=trace_ctx.trace_id,
                        span_id=trace_ctx.span_id,
                        message_obj=msg,
                        media_bundle=media_bundle,
                        on_progress=_on_progress,
                    )
                else:
                    notice = "⚠️ 系统还没就绪，稍等一分钟再试"

                if resp:
                    # 完成态：答案写进同一张卡（只发一条）。卡片放不下时退回文本，
                    # 过程卡仍收尾——用户至少还能拿到答复。
                    card_ok = False
                    if _progress_state["card_id"]:
                        _progress_state["done"] = True
                        _progress_state["answer"] = resp
                        await _push_card(True)
                        card_ok = not _progress_state.get("cancelled")
                        if not answer_fits_card(resp):
                            logger.info("答复 %d 字超出卡片容量，正文改发文本", len(resp))
                            card_ok = False
                    if not card_ok:
                        # 反馈卡挂在回复上（thread 内）；有完成卡时按钮已内嵌，不重复发。
                        sent = await channel.reply(msg, resp)
                        await _maybe_send_feedback_card(
                            channel, chat_id, trace_ctx.trace_id, reply_to=sent)
                else:
                    await channel.reply(
                        msg, notice or "⚠️ 这次没拿到回复，稍等再问一次"
                    )
            except Exception as e:
                logger.exception(f"[{trace_ctx.trace_id}] Orchestrator background task failed: {e}")
                try:
                    await channel.reply(msg, f"⚠️ 处理出错：{str(e)[:100]}，换个说法再试试，或稍后重试")
                except Exception:
                    pass
            finally:
                # 运行时护栏：释放并发额度 + 落 finished 档案（异常路径也必须释放）
                if _runtime_guard is not None and lifecycle is not None:
                    _runtime_guard.finish_task(
                        lifecycle, outcome="replied" if resp else "no_response"
                    )

            # ── Step 4: 回填多轮历史（供后续消息承接上下文） ──
            if _orchestrator and resp:
                try:
                    await _orchestrator.record_history(chat_id, sender_id, text, resp)
                except Exception:
                    logger.debug("History record failed, continuing")

            # ── Step 4: Audit logging ──
            tracer.audit(
                action="agent:langgraph_router",
                user=sender_id,
                target=chat_id,
                result="ok" if resp else "no_response",
                detail=text[:100],
            )

            # ── Step 5: Capture conversation round ──
            if memory_client and memory_client.config.enabled:
                try:
                    # fire-and-forget：持引用防 GC 中途取消任务
                    _mem_task = asyncio.ensure_future(
                        memory_client.capture(
                            session_key=_memory_session_key(chat_id),
                            user_content=text,
                            assistant_content=resp or "",
                            user_id=sender_id,
                        )
                    )
                    _background_tasks.add(_mem_task)
                    _mem_task.add_done_callback(_background_tasks.discard)
                except Exception:
                    logger.debug("Memory capture failed, continuing")

            # ── Step 6: Trace observability ──
            duration_ms = (time.time() - start_time) * 1000
            tracer.trace(AgentTrace(
                agent="langgraph_router",
                intent="langgraph_router",
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                trace_id=trace_ctx.trace_id,
                span_id=trace_ctx.span_id,
                input_text=text[:200],
                output_text=(resp or "")[:200],
                status="success",
                duration_ms=duration_ms,
            ))
            logger.info(f"[{trace_ctx.trace_id}] Done ({duration_ms:.0f}ms)")

        # 不 await —— 后台执行，on_message 在回执后立即返回（秒回）
        asyncio.get_running_loop().create_task(_run_orchestrator())
        asyncio.get_running_loop().create_task(_heartbeat())

    except Exception:
        # 并发额度已在申请时持有，异常路径必须释放（正常路径在 _run_orchestrator 释放）
        if _runtime_guard is not None and lifecycle is not None:
            _runtime_guard.finish_task(lifecycle, outcome="error")
        logger.exception(f"[{trace_ctx.trace_id}] Error handling message {msg.id}")
        duration_ms = (time.time() - start_time) * 1000
        tracer.trace(AgentTrace(
            agent="langgraph_router",
            intent="langgraph_router",
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            trace_id=trace_ctx.trace_id,
            span_id=trace_ctx.span_id,
            input_text=text[:200],
            status="error",
            duration_ms=duration_ms,
        ))


async def _maybe_send_feedback_card(
    channel: BotChannel, chat_id: str, task_id: str, reply_to=None
) -> None:
    """答复后追加 🙂/🙃 反馈卡（`messaging.feedback_buttons` 关闭时不做）。

    正文仍走文本回复：飞书 markdown 消息带不了按钮，反馈卡只能单独发一张。
    有 `reply_to`（上一条答复的 SendResult）就把它挂成对该消息的回复——卡片
    紧贴答案，会话再热闹也不会张冠李戴。

    这是**额外的低成本信号**：点 🙃 直接落到该轮档案的 feedback 字段，
    比「等用户补一句不对」覆盖面大得多。发不出去就算了，不影响答复。
    """
    if _messaging_cfg is None or not getattr(_messaging_cfg, "feedback_buttons", True):
        return
    if not task_id:
        return
    try:
        from app.protocols.feedback_card import build_feedback_card

        card = build_feedback_card(task_id)
        message_id = str(getattr(reply_to, "message_id", "") or "")
        if message_id:
            await channel.reply_card(chat_id, message_id, card)
        else:
            await channel.send_card(chat_id, card)
    except Exception as e:
        logger.debug("[feedback] 反馈卡发送失败（不影响答复）: %s", e)


async def _on_feedback_card_action(event) -> None:
    """反馈卡按钮回调：回填档案 → 原地更新卡片为「已记录」。

    非本卡的点击静默返回（卡片回调会广播给所有 handler，`fb` 键才是判据）。
    """
    from app.protocols.feedback_card import (
        build_done_card,
        is_feedback_action,
        parse_feedback_value,
    )

    if not is_feedback_action(event):
        return
    task_id, positive = parse_feedback_value(event)
    if not task_id:
        return
    operator = getattr(getattr(event, "operator", None), "open_id", "") or ""
    task_recorder.attach_feedback_by_task(task_id, {
        "negative": not positive,
        "text": "按钮：🙃 不太行" if not positive else "按钮：🙂 有帮助",
        "source": "button",
        "operator": operator,
    })
    message_id = str(getattr(event, "message_id", "") or "")
    channel = _channel
    if message_id and channel is not None:
        try:
            await channel.update_card(
                message_id, build_done_card(task_id, positive, operator[-6:]))
        except Exception as e:
            logger.debug("[feedback] 卡片更新失败: %s", e)
    logger.info("[feedback] task=%s %s by %s", task_id,
                "🙂" if positive else "🙃", operator[-6:] or "?")


# ── /dev 直达通道（管理员远程开发）──────────────────────────────────


async def _handle_dev_channel(
    text: str,
    msg: InboundMessage,
    channel: BotChannel,
    chat_id: str,
    sender_id: str,
    sender_name: str,
    trace_ctx: TraceContext,
    lifecycle: TaskLifecycleRecord | None,
):
    """`/dev <需求>` → 直投 cc-connect dev-bot（Claude Code），不经路由 LLM。

    dev-bot 是维护者的远程开发通道（routable=false，见 agent.yaml）：
    团队消息永远路由不到它；只有 L2 管理员显式 /dev 才进入。
    复用编排层的运行时护栏额度（lifecycle 已在 Step 0.5 申请），
    会话复用 cc-connect 侧 feishu:{chat}:{user} session（上下文延续）。
    """
    from app.orchestrator import get_user_permission

    perm = get_user_permission(sender_id, chat_id)
    if perm.level < 2:
        try:
            await channel.reply(
                msg, "🔒 /dev 是维护者的远程开发通道，暂不对你开放。有问题直接说，我来处理～"
            )
        except Exception:
            logger.debug("dev deny reply failed", exc_info=True)
        if _runtime_guard is not None and lifecycle is not None:
            _runtime_guard.finish_task(lifecycle, outcome="replied")
        tracer.audit(
            action="dev:denied",
            user=sender_id,
            target=chat_id,
            result="denied",
            detail=f"level={perm.level}",
        )
        return

    prompt = text[4:].strip()  # 去掉 "/dev" 前缀
    if not prompt:
        try:
            await channel.reply(
                msg, "🛠 用法：/dev <需求>\n例如：/dev 看一下 dev-bot 最近一次会话的报错日志"
            )
        except Exception:
            logger.debug("dev usage reply failed", exc_info=True)
        if _runtime_guard is not None and lifecycle is not None:
            _runtime_guard.finish_task(lifecycle, outcome="replied")
        return

    cc = get_cc_client()
    if cc is None:
        try:
            await channel.reply(msg, "⚠️ Claude Code 执行通道未启用")
        except Exception:
            logger.debug("dev offline reply failed", exc_info=True)
        if _runtime_guard is not None and lifecycle is not None:
            _runtime_guard.finish_task(lifecycle, outcome="error")
        return

    # 项目选择：L2 全权走 dev-bot；PM 负责人走 pm-dev（受限）。
    # 分界不按等级而是按「是不是 PM 身份」，因为两人都是 L2 —— 等级分不开，
    # 身份能分。pm-dev 侧的约束在 Claude 配置里（permissions.deny + PreToolUse
    # hook），这里是选择路由，不是安全性唯一来源。
    project = _dev_project_of(sender_id)

    try:
        await channel.reply(
            msg,
            "⏳ 交给 **开发助手（开发模式）** 处理中…" if project == "dev-bot"
            else "⏳ 交给 **PM 受限开发通道** 处理中（只能改 PM 相关代码）…",
        )
    except Exception:
        logger.debug("dev routed receipt failed", exc_info=True)

    start = time.time()
    try:
        resp = await cc.send_and_wait(
            prompt=prompt,
            project=project,
            session_key=_memory_session_key(chat_id),
        )
        reply = resp or "⚠️ Claude Code 未响应（超时或不可达）"
        status = "ok" if resp else "no_response"
    except Exception as e:
        logger.warning("[dev] send_and_wait failed: %s", e)
        reply = f"⚠️ 处理出错：{str(e)[:100]}"
        status = "error"

    try:
        await channel.reply(msg, reply)
    except Exception:
        logger.debug("dev reply failed", exc_info=True)

    duration_ms = (time.time() - start) * 1000
    if _runtime_guard is not None and lifecycle is not None:
        _runtime_guard.finish_task(
            lifecycle, outcome="replied" if status == "ok" else "no_response"
        )
    tracer.audit(
        action=f"dev:{project}",
        user=sender_id,
        target=chat_id,
        result=status,
        detail=prompt[:100],
    )
    tracer.trace(AgentTrace(
        agent="dev-bot",
        intent="dev_channel",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        trace_id=trace_ctx.trace_id,
        span_id=trace_ctx.span_id,
        input_text=text[:200],
        output_text=reply[:200],
        status="success" if status == "ok" else "error",
        duration_ms=duration_ms,
    ))
    # 记录任务档案（与编排层同格式，final_agent=dev-bot 便于控制台区分）
    task_recorder.write(TaskRecord(
        task_id=trace_ctx.trace_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        query=text[:300],
        final_agent="dev-bot",
        route_chain=["dev_direct"],
        status="success" if status == "ok" else "error",
        duration_ms=round(duration_ms, 1),
        response_preview=reply[:200],
        error="" if status == "ok" else reply[:200],
    ))


# ── Startup ─────────────────────────────────────────────────────


async def main():
    """Entry point."""
    logger.info("===== Feishu AI Bot (Channel SDK + LLM Routing + Trace) 启动 =====")
    # 事件循环钩子必须在循环存在后挂：提前挂会拿到另一个即将被替换的 loop。
    asyncio.get_running_loop().set_exception_handler(_LOOP_EXCEPTION_HANDLER)

    # Load config
    config = load_config()
    logger.info(f"app_id={config.feishu.app_id[:8]}...")

    # ── Initialize MessageAggregator（连发消息 debounce 合并）──
    global _aggregator, _messaging_cfg
    msg_cfg = config.messaging
    _messaging_cfg = msg_cfg
    if msg_cfg.enabled:
        _aggregator = MessageAggregator(
            debounce_sec=msg_cfg.debounce_sec,
            max_wait_sec=msg_cfg.max_wait_sec,
        )
        logger.info(
            f"MessageAggregator enabled: debounce={msg_cfg.debounce_sec}s, "
            f"max_wait={msg_cfg.max_wait_sec}s, "
            f"ack_receipt={msg_cfg.ack_receipt}, routed_receipt={msg_cfg.routed_receipt}"
        )
    else:
        logger.info("MessageAggregator disabled — messages processed individually")

    # ── Initialize MemoryClient ──
    global _memory_client, _memory_lifecycle
    mem_config = config.memory

    # ── MemoryCore 进程托管：随 bot 启动拉起（模型经 TDAI_LLM_* 与 bot 同源）──
    lc_cfg = mem_config.lifecycle
    if mem_config.enabled and lc_cfg.enabled:
        _memory_lifecycle = MemoryCoreLifecycle(MemoryLifecycleConfig(
            enabled=True,
            base_url=mem_config.base_url,
            core_dir=lc_cfg.core_dir,
            manage_process=lc_cfg.manage_process,
        ))
        try:
            memory_up = await _memory_lifecycle.startup()
            logger.info(
                "MemoryCore lifecycle: %s",
                "service ready" if memory_up else "service NOT ready (degraded)",
            )
        except Exception as e:
            logger.warning("MemoryCore lifecycle startup failed: %s", e)

    if mem_config.enabled:
        _memory_client = MemoryClient(MemoryConfig(
            enabled=mem_config.enabled,
            base_url=mem_config.base_url,
            api_key=mem_config.api_key,
            service_id=mem_config.service_id,
            timeout_sec=mem_config.timeout_sec,
            recall_context_max_chars=mem_config.recall_context_max_chars,
        ))
        try:
            health = await _memory_client.health()
            if health.get("status") == "ok":
                logger.info(f"MemoryCore Gateway connected: v{health.get('version', '?')}")
            else:
                logger.warning(f"MemoryCore Gateway health degraded: {health}")
        except Exception as e:
            logger.warning(f"MemoryCore Gateway unreachable: {e}. Memory disabled.")
            _memory_client = None
    else:
        logger.info("Memory disabled by config")

    # ── Initialize WebPortalClient（工单状态查询等遗留 API，主能力已由
    #    webportal-order-agent A2A 服务承载；Pipeline API 保留可选）──
    global _webportal_client
    wp_config = config.webportal
    if wp_config.enabled:
        _webportal_client = WebPortalClient(WebPortalConfig(
            enabled=wp_config.enabled,
            base_url=wp_config.base_url,
            timeout_sec=wp_config.timeout_sec,
        ))
        try:
            wp_ok = await _webportal_client.health()
            if wp_ok:
                logger.info("WebPortal-pilot connected")
            else:
                logger.warning("WebPortal-pilot health degraded")
        except Exception as e:
            logger.warning(f"WebPortal-pilot unreachable: {e}. Ticket API features disabled.")
            _webportal_client = None
    else:
        logger.info("WebPortal-pilot disabled by config (ticket capability served by A2A)")

    # ── 注入附件下载回调（webportal-agent 透传消息附件） ──
    # channel 要等飞书连接建好才有，注入挪到 channel 赋值之后。
    set_attachment_downloader(_download_attachments)

    # ── 注入迟到结果投递回调（cc-connect 超时转后台 → 完成后直发原会话） ──
    set_late_delivery_sender(_send_late_result)

    # ── Initialize cc-connect Client ──
    global _cc_client
    cc_config = config.cc_connect
    if cc_config.enabled:
        _cc_client = CCConnectClient(CCConnectConfig(
            enabled=cc_config.enabled,
            webhook_url=cc_config.webhook_url,
            webhook_token=cc_config.webhook_token,
            default_project=cc_config.default_project,
            timeout_sec=cc_config.timeout_sec,
            max_sessions=cc_config.max_sessions,
        ))
        logger.info(
            f"cc-connect client configured: default={cc_config.default_project}, "
            f"max_sessions={cc_config.max_sessions}"
        )

    # ── Initialize AgentRegistry + CrewOrchestrator ──
    global _registry_agent, _orchestrator
    _registry_agent = AgentRegistry()
    agents = _registry_agent.load()
    enabled_count = len([a for a in agents if a.enabled])

    # SKILL.md 快速接入：扫描 bot/skills/ → 合成 Agent 注入 registry →
    # 复制同步到 .claude/skills/（Claude Code 原生消费）。须在 orchestrator
    # 创建前完成——路由工具表由完整 registry 派生（含 Skill 合成 Agent）
    from app.agents.skills import setup_skills
    skill_count = setup_skills(_registry_agent)

    # 运行时能力开关（bot/todos/agent_switches.json）：启动即应用——
    # 开关禁用的 Agent 不进路由工具表（控制台可动态上下线，热生效）
    from app.agents.switches import load_switches
    switch_off = load_switches()
    if switch_off and _registry_agent is not None:
        _registry_agent.apply_switches(switch_off)

    # 初始化独立 Agent Handler（约定式自动发现，见 app/agents/handlers/discovery.py）
    handler_registry = AgentHandlerRegistry()
    if config.router.enabled:
        handler_registry = auto_discover_handlers(
            config.router,
            {
                # 案例库飞书镜像写目标：webportal-handler 案例沉淀后同步
                "case_mirror": config.case_mirror,
                # 团队 Skill 目录（按分支读 description）：skill-catalog-handler 读取
                "skill_catalog": _build_skill_catalog_store(config),
                # easyprd 配置：prd-watch-handler 先建实例，watcher 在 channel
                # 就绪后由 bind_watcher 注入（工具循环需要发消息能力）
                "prd_watch": config.prd_watch,
                # CMDB 工单提醒：cmdb-watch-handler 先建实例，watcher 在 channel
                # 就绪后由 bind_watcher 注入（订阅回复要能直投私聊）
                "cmdb_watch": config.cmdb_watch,
            },
        )

    # 编排器创建只依赖 router.enabled，不依赖 cc-connect —— cc-connect 是可选执行通道之一
    if config.router.enabled:
        _orchestrator = CrewOrchestrator(
            cc_client=_cc_client,
            config=config.router,
            webportal_client=_webportal_client,
            registry=_registry_agent,
            handler_registry=handler_registry,
            memory_client=_memory_client,
        )
        logger.info(
            f"CrewOrchestrator initialized: model={config.router.model}, "
            f"cc_connect={'on' if _cc_client else 'OFF (degraded)'}, "
            f"webportal={_webportal_client is not None}, "
            f"skills={skill_count}, handlers={handler_registry.list()}"
        )
        if _skill_catalog_store is not None and _skill_catalog_store.is_stale(_skill_catalog_store.load()):
            async def _warm_catalog(store=_skill_catalog_store):
                try:
                    _, note = await store.refresh(force=False)
                    logger.info("[skill_catalog] startup refresh: %s", note)
                except Exception as e:
                    logger.warning("[skill_catalog] startup refresh failed: %s", e)
            # 持引用防 GC：预热是一次性任务，进程生命周期内只此一处持有
            _warm_task = asyncio.create_task(_warm_catalog())
            _background_tasks.add(_warm_task)
            _warm_task.add_done_callback(_background_tasks.discard)
        # 控制台能力开关接线（dashboard 与 bot 同进程）：开关变更 ->
        # registry 热更新 + 路由图重建，即刻生效
        from app.gateway.api.dashboard import configure_agent_control
        configure_agent_control(_registry_agent, _orchestrator.rebuild)
        if switch_off:
            logger.info(f"Agent switches applied at startup: {sorted(switch_off)}")

    # ── Initialize dsh 执行面（[projects.dsh].enabled 控制；旧编排链路保留回切）──
    # 拉起失败不阻断启动：消息链路回落到 CrewOrchestrator，日志明确标注降级原因。
    global _dsh_cfg, _dsh_supervisor, _dsh_gateway
    _dsh_cfg = config.dsh
    if _dsh_cfg.enabled:
        # 映射落盘：sessionId 随 dsh 进程消亡，但 bot 自己重启时 dsh 可能还活着
        # （独立 DSH_HOME 的产品 profile 常驻），落盘能让用户重启后仍接着上一轮聊。
        gateway = DshGateway(GatewayConfig(
            ttl_sec=_dsh_cfg.session_ttl_sec,
            state_path=absolute_path("logs/dsh/sessions.json"),
        ))
        supervisor = DshSupervisor(
            build_dsh_supervisor_config(
                _dsh_cfg,
                config.mcp,
                # 用 routable() 而不是 enabled()：运行时开关关掉的能力（以及
                # 白名单外/不参与路由的 agent）不该把它的数据源带进会话——
                # 工具还挂着，模型就能绕过路由直接调。
                _registry_agent.routable() if _registry_agent is not None else None,
            ),
            on_event=gateway.on_event,
        )
        started = await supervisor.start()
        if started:
            gateway.attach(supervisor)
            _dsh_supervisor, _dsh_gateway = supervisor, gateway
            # 登记到 runtime：`__main__` 与 `app.main` 是**两个模块对象**，
            # 其它模块 `from app.main import ...` 会重新执行本文件拿到空值。
            from app.runtime import set_dsh_gateway

            set_dsh_gateway(gateway)
            logger.info(
                "dsh 执行面就绪: profile=%s model=%s/%s（消息链路走 dsh）",
                _dsh_cfg.profile, _dsh_cfg.provider, _dsh_cfg.model,
            )
        else:
            logger.error(
                "dsh 执行面启动失败，消息链路回落到旧编排（检查 profile/凭据后重启）"
            )
    else:
        logger.info("dsh 执行面已关闭（[projects.dsh].enabled=false），走旧编排链路")

    # ── Initialize HealthMonitor ──
    global _health_monitor
    if _registry_agent:
        # 获取 webportal base_url（可能未启用）
        _wp_base = "http://127.0.0.1:8000"
        if _webportal_client:
            _wp_base = _webportal_client.config.base_url
        _health_monitor = AgentHealthMonitor(
            registry=_registry_agent,
            cc_client=_cc_client,
            check_interval_sec=300,
            webportal_base_url=_wp_base,
            handler_registry=handler_registry,
        )
        try:
            health_summary = await quick_health_check(
                _registry_agent, _cc_client,
                webportal_base_url=_wp_base,
                handler_registry=handler_registry,
            )
            healthy = health_summary.get("healthy", 0)
            critical = health_summary.get("critical", 0)
            total = health_summary.get("total", 0)
            logger.info(f"Initial health check: {healthy}/{total} healthy, {critical} critical")
            await _health_monitor.start_periodic_check()
        except Exception as e:
            logger.warning(f"Health check failed at startup: {e}")

    # ── Initialize Capability Hot-plug Service ──
    # 管理员发能力包 → 扫描 → 审批卡片 → 热重建（设计稿 20260912-capability-hotplug）
    global _capability_ingest
    if config.capability.enabled and _registry_agent and _orchestrator:
        from app.ops.capability import (
            CapabilityIngestService,
            CapabilityScanner,
            CapabilityStore,
            HotReloader,
        )
        from app.ops.capability.approval_card import ApprovalCardHandler

        cap_store = CapabilityStore()
        reloader = HotReloader(
            registry=_registry_agent,
            store=cap_store,
            rebuild_callback=_orchestrator.rebuild,
        )
        _capability_ingest = CapabilityIngestService(
            store=cap_store,
            scanner=CapabilityScanner(),
            send_card_callback=_send_capability_card,
            reply_callback=_reply_capability,
            min_level=config.capability.approver_min_level,
            max_pkg_mb=config.capability.max_pkg_mb,
        )
        _capability_ingest.wire_channel_downloader(_download_capability_resource)
        approval_handler = ApprovalCardHandler(
            store=cap_store,
            deploy_callback=reloader.deploy,
            update_card_callback=_update_capability_card,
            min_level=config.capability.approver_min_level,
            resolve_name_callback=_resolve_approver_name,
        )
        _capability_handlers.append(approval_handler)
        logger.info(
            "Capability hot-plug mounted: ingest + approval card + hot reloader "
            f"(min_level=L{config.capability.approver_min_level}, max_pkg={config.capability.max_pkg_mb}MB)"
        )

    # ── Initialize ScheduledTask Service（定时任务：创建→评估→调度执行，无审批门）──
    global _scheduler_service
    if config.scheduled_tasks.enabled and _orchestrator is not None:
        _scheduler_service = ScheduledTaskService(
            scan_interval_sec=config.scheduled_tasks.scan_interval_sec,
        )
        # schedule-agent Handler 注入服务总装（discovery 工厂先建壳）
        schedule_handler = handler_registry.get("schedule-agent")
        if schedule_handler is not None and hasattr(schedule_handler, "wire_service"):
            schedule_handler.wire_service(_scheduler_service)
        else:
            logger.warning("ScheduledTasks: schedule-agent handler 未发现，创建入口不可用")
        logger.info(
            "ScheduledTask service initialized: scan_interval=%ss（创建即生效）",
            config.scheduled_tasks.scan_interval_sec,
        )

    # ── Initialize EDC Metrics Collector ──
    global _edc_collector
    _edc_collector = EDCMetricsCollector()
    try:
        snapshot = _edc_collector.collect(days=1)
        _edc_collector.write_snapshot(snapshot)
        logger.info(f"EDC metrics snapshot: {snapshot['summary']['total_calls']} calls today")
    except Exception as e:
        logger.warning(f"EDC metrics snapshot failed: {e}")

    # ── Initialize Daily Report Module ──
    global _daily_scheduler, _daily_subscription_manager
    dr_config = config.daily_report
    if dr_config.enabled:
        _daily_subscription_manager = SubscriptionManager()
        _daily_store = DailyStore(dr_config.cache_dir)
        # 定时推送单源 = AI HOT（get_daily_pair 内部含本地缓存优先 + API 兜底）。
        # 旧的 LocalKBase 目录 reader 已随目录读取设计下线。
        from app.domains.daily.scheduler import AIHOTDailySource
        _daily_scheduler = DailyScheduler(
            source=AIHOTDailySource(),
            store=_daily_store,
            subscription=_daily_subscription_manager,
            converter=None,  # AI HOT 源直出推送文本，无需二次转换
            check_interval_sec=dr_config.check_interval_sec,
        )
        logger.info("Daily report module initialized: source=aihot (local-cached)")
    else:
        logger.info("Daily report module disabled by config")

    # ── MCP 孤儿进程清理 ──
    try:
        killed = reap_orphans(expected=0, dry_run=False)
        if killed > 0:
            logger.info(f"MCP reaper: cleaned {killed} orphan processes")
        else:
            logger.info("MCP reaper: no orphan processes found")
        _mcp_watcher_task = start_watcher(interval_sec=120, expected=0)
        if _mcp_watcher_task is not None:
            _background_tasks.add(_mcp_watcher_task)  # 持引用防 GC
        logger.info("MCP reaper watcher started (300s interval)")
    except Exception as e:
        logger.warning(f"MCP reaper init failed: {e}")

    # ── Initialize RuntimeGuard（并发闸 + 有界排队 + 任务持久化）──
    # 防高并发压垮本地服务器；重启时扫描 running 档案 → interrupted 补通知
    global _runtime_guard
    rg_cfg = config.runtime_guard
    if rg_cfg.enabled:
        guard_store = TaskLifecycleStore(
            log_dir=Path(rg_cfg.persist_dir)
            if Path(rg_cfg.persist_dir).is_absolute()
            else PROJECT_DIR / rg_cfg.persist_dir
        )
        _runtime_guard = RuntimeGuard(
            max_concurrent=rg_cfg.max_concurrent,
            max_queue=rg_cfg.max_queue,
            queue_timeout_sec=rg_cfg.queue_timeout_sec,
            persist=guard_store if rg_cfg.persist_enabled else None,
        )
        logger.info(
            f"RuntimeGuard enabled: max_concurrent={rg_cfg.max_concurrent}, "
            f"max_queue={rg_cfg.max_queue}, queue_timeout={rg_cfg.queue_timeout_sec}s, "
            f"persist={'on' if rg_cfg.persist_enabled else 'off'}"
        )

        # ── 启动扫描：上次进程中断的任务（遗留 running 档案）主动告知用户 ──
        if rg_cfg.persist_enabled and rg_cfg.notify_interrupted:
            async def _notify_interrupted() -> None:
                ch = await _wait_channel()
                if ch is None:
                    logger.warning("Interrupted-task notify skipped: channel not ready")
                    return
                interrupted = guard_store.scan_interrupted()
                for rec in interrupted:
                    guard_store.mark_interrupted(rec)
                text = RuntimeGuard.notify_text(interrupted)
                if not text:
                    logger.info("Interrupted-task scan: none found")
                    return
                logger.warning("Interrupted-task scan: %d task(s) found", len(interrupted))
                # 按 chat_id 去重逐会话补通知
                by_chat: dict[str, list] = {}
                for rec in interrupted:
                    if rec.chat_id:
                        by_chat.setdefault(rec.chat_id, []).append(rec)
                for cid, recs in by_chat.items():
                    try:
                        await ch.send(cid, RuntimeGuard.notify_text(recs))
                    except Exception:
                        logger.debug("Interrupted-task notify failed for %s", cid)

            # 先把上次中断任务落 interrupted 标记（无论通知成败，避免重复打扰）
            _bg = asyncio.get_running_loop().create_task(_notify_interrupted())
            _background_tasks.add(_bg)
            _bg.add_done_callback(_background_tasks.discard)
    else:
        logger.info("RuntimeGuard disabled by config — 无全局并发上限（不推荐生产）")

    # ── 会话轨迹保留期清理（启动跑一次）──
    # 每轮一个 .md，不清会一年堆几十万个小文件，把 `--days 30` 的检索拖慢。
    try:
        from app.observability.turn_trace import turn_tracer

        dropped = turn_tracer.prune(keep_days=90)
        if dropped:
            logger.info("turn trace: 清理 %d 条超过 90 天的轨迹", dropped)
    except Exception as e:
        logger.debug("turn trace prune skipped: %s", e)

    # ── Create channel and start ──
    global _channel, _prd_watch
    # easyprd 授权群放开「不@我也收消息」——仅这些 chat_id，其余群准入一字不变
    _prd_watch = _build_prd_watch(config, handler_registry)
    # CMDB 工单提醒：独立旁路（不参与消息路由），未启用时 None
    _build_cmdb_watch(config, handler_registry)
    # 把旁路服务登记给 MCP 工具面：MCP 侧不 import 主入口（分层表里没有
    # mcp → main 这条边），要用的实例由这里推进去。
    from app.mcp.capabilities_server import register_live_watchers
    register_live_watchers(cmdb=_cmdb_watch, prd=_prd_watch)
    channel = BotChannel(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        token_store_dir=Path(config.work_dir) / "bot" / "tokens" if config.work_dir else None,
        no_mention_chats=_prd_watch.watcher.watch_chats() if _prd_watch.active else None,
    )
    _channel = channel
    from app.orchestrator.crew import set_recent_file_channel
    set_recent_file_channel(channel)

    # Register message handler — 所有消息走 CrewOrchestrator 纯 LLM 路由
    channel.on_message(lambda msg: asyncio.ensure_future(on_message(msg, channel)))
    channel.on_error(lambda e: logger.error(f"Channel error: {e}"))
    # 能力热插拔 + 定时任务：审批卡片按钮回调（_capability_handlers 聚合分发）
    if _capability_handlers:
        channel.on_card_action(
            lambda event: asyncio.ensure_future(_on_capability_card_action(event))
        )

    # easyprd 卡片按钮（采纳/修正/忽略）：独立注册，不挤进 _capability_handlers ——
    # 那个列表的每个 handler 都会在**任何**卡片回调上被调一次，塞进来会让
    # 能力审批的每次点击都多跑一次 easyprd 分支。
    # 判据是「服务存在」而不是 `active`：`active` 还要求 enabled + 授权群，
    # 而管理员自测台（/easyprd demo）正是给「群↔Story 还没配」的阶段用的——
    # 按 active 注册会让它发得出卡片、点下去却没人接（飞书报「回调服务未在线」）。
    # 非本卡片的点击由 handler 自己按 cr/act 判据静默返回，不会误认领。
    if _prd_watch is not None:
        channel.on_card_action(
            lambda event: asyncio.ensure_future(_on_prd_card_action(event))
        )

    # PM 催办卡片（改这条/发送/跳过）：独立注册，理由同上——
    # 它自己按 value 里的 `pn` 键认领，混进 _capability_handlers 会让
    # 每次能力审批点击都多跑一遍 PM 分支。
    channel.on_card_action(
        lambda event: asyncio.ensure_future(_on_pm_card_action(event))
    )

    # 回复反馈卡（🙂/🙃）：同样独立注册——它要在**每一轮**答复后都能被点，
    # 塞进 _capability_handlers 会让每次点击多跑一遍能力审批分支。
    channel.on_card_action(
        lambda event: asyncio.ensure_future(_on_feedback_card_action(event))
    )

    # ── Wire easyprd（channel 就绪后启动监听与决策轮询；未启用则空转返回）──
    if _prd_watch is not None:
        await _prd_watch.start()

    # ── Wire CMDB 工单提醒（channel 就绪后启动轮询；未启用则 None）──
    if _cmdb_watch is not None:
        await _cmdb_watch.start()

    # ── Wire ScheduledTask service（channel 就绪后注入回调 + 启动调度循环）──
    if _scheduler_service is not None:
        _scheduler_service.wire(
            send_card_callback=_send_capability_card,
            reply_callback=_reply_text_via_msg,
            update_card_callback=_update_capability_card,
            orchestrator=_orchestrator,
            channel=channel,
            # 执行面跟消息链路同一条：dsh 启用后定时任务也走 dsh，
            # 否则同样的指令在群里与到点执行会得到两套结果。
            dsh_gateway=_dsh_gateway,
        )
        # 评估器的可承接名单要能在能力热插拔后刷新：默认只放开 aihot，
        # 新能力上线后不调这一句，用户建任务会被「执行方不在能力清单内」驳回。
        _scheduler_service.on_agents_reloaded(list(_registry_agent.routable()))
        await _scheduler_service.start()
        logger.info("  ScheduledTask: loop started (创建走 schedule-agent，评估通过即生效)")

    # Start daily report scheduler
    if _daily_scheduler:
        await _daily_scheduler.start(channel)
        logger.info(f"  Daily: scheduler started ({dr_config.check_interval_sec}s interval)")

    logger.info("===== 启动完成 =====")
    logger.info(f"  Agents: {len(agents)} registered, {enabled_count} enabled")
    logger.info(f"  Health: {_health_monitor is not None}")
    logger.info(f"  EDC: {_edc_collector is not None}")
    logger.info(f"  Memory: {_memory_client is not None}")
    logger.info(f"  WebPortal: {_webportal_client is not None}")
    logger.info("  Dashboard: port=%s, enabled=%s", config.dashboard.port, config.dashboard.enabled)
    if _prd_watch is not None:
        _snap = _prd_watch.snapshot()
        logger.info("  easyprd: enabled=%s, 群监听=%s, dry_run=%s, 授权群=%d%s",
                    _snap["enabled"],
                    "开" if _snap["enabled"] and _snap["watch_chats"] else "关（手动入口可用）",
                    _snap["dry_run"], _snap["watch_chats"],
                    "（台账未配全，变更记录只在内存）" if _snap["degraded_ledger"] else "")


    # ── Start Dashboard HTTP Server (background task) ──
    if config.dashboard.enabled:
        # noinspection PyTypeChecker
        _dashboard_task = asyncio.create_task(
            start_dashboard_server(port=config.dashboard.port, host=config.dashboard.host)
        )
        _background_tasks.add(_dashboard_task)  # 持引用防 GC
        logger.info(f"Dashboard HTTP server started on {config.dashboard.host}:{config.dashboard.port}")

    # Connect and run
    logger.info("Connecting to Feishu via WebSocket...")
    try:
        await channel.run_forever()
    finally:
        # ── 随 bot 关停：监听/决策轮询 + 调度循环 + 整树回收本次拉起的 MemoryCore ──
        if _prd_watch is not None:
            await _prd_watch.stop()
        if _cmdb_watch is not None:
            await _cmdb_watch.stop()
        if _scheduler_service is not None:
            await _scheduler_service.stop()
        if _dsh_gateway is not None:
            await _dsh_gateway.shutdown()
        if _dsh_supervisor is not None:
            await _dsh_supervisor.shutdown()
        if _memory_lifecycle is not None:
            _memory_lifecycle.shutdown()


if __name__ == "__main__":
    asyncio.run(main())