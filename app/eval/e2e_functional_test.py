"""端到端功能测试（SPEC 之外的整链路验收）— 直接打应用核心，不经飞书。

覆盖面：
  T1 装配：registry + skills + handlers + graph 编译
  T2 clarify 全链路：模糊消息 → clarify_node 反问（真实 LLM 路由）
  T3 三态路由真实执行（清晰消息 → agent 节点可达性）
  T4 TaskRecord + decisions 日志落盘回放
  T5 Dashboard :9112 HTTP 接口
  T6 内存在线监控（psutil，超阈值自动中止并报告）

用法（从项目根）：
    NO_PROXY="aigw.example.com" python app/eval/e2e_functional_test.py
退出码：全 PASS → 0；任一 FAIL → 1。
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import psutil  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
PROC = psutil.Process()
MEM_SAMPLES: list[float] = []
MEM_LIMIT_MB = 1500  # 单进程内存上限（超限中止，防撑爆机器）


def mem_mb() -> float:
    PROC.memory_info()  # 触发采样
    return PROC.memory_info().rss / 1024 / 1024


def check_mem(gate: str) -> None:
    mb = mem_mb()
    MEM_SAMPLES.append(mb)
    print(f"    [mem] {gate}: {mb:.0f} MB (peak {max(MEM_SAMPLES):.0f}/{MEM_LIMIT_MB} MB)")
    if mb > MEM_LIMIT_MB:
        print(f"  ❌ 内存超限 {mb:.0f}MB > {MEM_LIMIT_MB}MB — 中止后续用例")
        sys.exit(1)


def check(name: str, fn):
    """同步驱动：async 测试函数用独立 event loop（每用例隔离，防相互污染）。"""
    ok, detail = False, ""
    try:
        out = fn()
        if asyncio.iscoroutine(out):
            out = asyncio.run(out)
        ok, detail = out
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:150]}"
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail[:130]}")


async def t1_assembly() -> tuple[bool, str]:
    """T1 装配：全组件初始化 + graph 编译。"""
    from app.agents.handlers import auto_discover_handlers
    from app.agents.registry import AgentRegistry
    from app.agents.skills import setup_skills
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator

    cfg = load_config()
    reg = AgentRegistry()
    agents = reg.load()
    setup_skills(reg)
    handlers = auto_discover_handlers(cfg.router, {"daily_sources": None})
    orch = CrewOrchestrator(cc_client=None, config=cfg.router, registry=reg,
                            handler_registry=handlers)
    info = orch.get_graph_info()
    ok = len(agents) >= 8 and info["node_count"] >= 20 and bool(handlers.list())
    return ok, f"{len(agents)} agents, {info['node_count']} nodes, handlers={handlers.list()}"


async def t2_clarify_e2e() -> tuple[bool, str]:
    """T2 clarify 全链路：模糊消息走完 router→clarify_node 返回反问。"""
    from app.agents.handlers import auto_discover_handlers
    from app.agents.registry import AgentRegistry
    from app.agents.skills import setup_skills
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator

    cfg = load_config()
    reg = AgentRegistry()
    reg.load()
    setup_skills(reg)
    orch = CrewOrchestrator(cc_client=None, config=cfg.router, registry=reg,
                            handler_registry=auto_discover_handlers(cfg.router, {}))
    resp = await orch.execute(
        message="更新一下", chat_id="oc_e2e", sender_id="ou_test", sender_name="测试",
        trace_id="e2e-clarify-001",
    )
    ok = ("确认" in resp or "🤔" in resp) and "⚠️" not in resp[:5]
    return ok, f"response={resp[:70]!r}"


async def t3_route_agent_reachable() -> tuple[bool, str]:
    """T3 真实路由 → agent 节点可达（mock agent 执行，验证 route→agent→gather 全链）。"""
    from app.agents.handlers import auto_discover_handlers
    from app.agents.registry import AgentRegistry
    from app.agents.skills import setup_skills
    from app.config import load_config
    from app.orchestrator import CrewOrchestrator

    cfg = load_config()
    reg = AgentRegistry()
    reg.load()
    setup_skills(reg)
    orch = CrewOrchestrator(cc_client=None, config=cfg.router, registry=reg,
                            handler_registry=auto_discover_handlers(cfg.router, {}))

    called = {}

    async def fake_dispatcher(role, query, state):
        called["role"] = role
        called["query"] = query
        return "已查询 t_rwa_product 表结构：包含 id、product_name、nav、status 等字段", None

    orch._dispatchers["cc_connect"] = fake_dispatcher
    resp = await orch.execute(
        message="查一下 t_rwa_product 表结构", chat_id="oc_e2e", sender_id="ou_test",
        sender_name="测试", trace_id="e2e-route-001",
    )
    ok = called.get("role") == "db-agent" and "t_rwa_product" in resp
    return ok, f"dispatched={called.get('role')}, resp={resp[:40]!r}"


async def t4_task_record_and_decisions() -> tuple[bool, str]:
    """T4 执行后 TaskRecord + decisions 均落盘且可回放（同 task_id 串联）。"""
    from app.agents.handlers import auto_discover_handlers
    from app.agents.registry import AgentRegistry
    from app.agents.skills import setup_skills
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator

    today = datetime.now().strftime("%Y-%m-%d")
    tasks_path = PROJECT_DIR / "logs" / "tasks" / f"{today}.jsonl"
    dec_path = PROJECT_DIR / "logs" / "decisions" / f"{today}.jsonl"
    tasks_before = len(tasks_path.read_text(encoding="utf-8").splitlines()) if tasks_path.exists() else 0
    dec_before = len(dec_path.read_text(encoding="utf-8").splitlines()) if dec_path.exists() else 0

    cfg = load_config()
    reg = AgentRegistry()
    reg.load()
    setup_skills(reg)
    orch = CrewOrchestrator(cc_client=None, config=cfg.router, registry=reg,
                            handler_registry=auto_discover_handlers(cfg.router, {}))
    await orch.execute(message="你好", chat_id="oc_e2e", sender_id="ou_test",
                       sender_name="测试", trace_id="e2e-record-001")

    task_line = ""
    if tasks_path.exists():
        lines = tasks_path.read_text(encoding="utf-8").splitlines()
        if len(lines) > tasks_before:
            task_line = lines[-1]
    dec_line = ""
    if dec_path.exists():
        lines = dec_path.read_text(encoding="utf-8").splitlines()
        if len(lines) > dec_before:
            dec_line = lines[-1]
    if not task_line or not dec_line:
        return False, f"tasks+{len(task_line)>0} decisions+{len(dec_line)>0} 未新增"
    tr = json.loads(task_line)
    dec = json.loads(dec_line)
    ok = (tr.get("task_id") == dec.get("task_id")
          and "confidence" in dec.get("args", {})
          and "demoted" in dec)
    return ok, f"task_id 串联={tr.get('task_id')[:16]}…, final_agent={tr.get('final_agent')}, conf={dec['args']['confidence']}"


def t5_dashboard_http() -> tuple[bool, str]:
    """T5 Dashboard HTTP 接口（独立起 server，用完即关）。"""
    import threading
    import time

    from app.dashboard import start_dashboard_server

    async def _serve():
        await start_dashboard_server(port=9113, host="127.0.0.1")

    def _run():
        asyncio.run(_serve())

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    time.sleep(2.0)
    try:
        with urllib.request.urlopen("http://127.0.0.1:9113/api/stats/dashboard", timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        return "total" in str(body) or bool(body), f"HTTP 200, keys={list(body)[:4]}"
    except Exception as e:
        return False, f"HTTP 失败: {e}"


def main() -> int:
    print(f"=== E2E 功能测试（内存上限 {MEM_LIMIT_MB}MB，进程基线 {mem_mb():.0f}MB） ===\n")
    check("T1 装配（registry+skills+handlers+graph）", t1_assembly)
    check_mem("T1 后")
    gc.collect()

    check("T2 clarify 全链路（模糊→反问）", t2_clarify_e2e)
    check_mem("T2 后")
    gc.collect()

    check("T3 真实路由→agent 分派→gather", t3_route_agent_reachable)
    check_mem("T3 后")
    gc.collect()

    check("T4 TaskRecord+decisions 落盘回放", t4_task_record_and_decisions)
    check_mem("T4 后")
    gc.collect()

    check("T5 Dashboard HTTP :9113", t5_dashboard_http)
    check_mem("T5 后")

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"\n=== E2E 结果: {passed}/{len(RESULTS)} PASS ===")
    print(f"=== 内存: peak {max(MEM_SAMPLES):.0f}MB / limit {MEM_LIMIT_MB}MB ===")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
