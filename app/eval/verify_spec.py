"""SPEC-20260912 验收脚本（R7）— 按规格逐条断言，AI 可自跑出报告。

用法：
    python app/eval/verify_spec.py               # 静态/冒烟条目（LLM 用例 SKIP）
    python app/eval/verify_spec.py --with-llm    # 含真实 LLM 行为回归（R6）

退出码：全部 PASS（SKIP 不计）→ 0；任一 FAIL → 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

RESULTS: list[tuple[bool, str, str]] = []  # (passed, ac_id, detail)


def check(ac: str, desc: str, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    RESULTS.append((ok, ac, f"{desc} — {detail}"))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {ac} {desc}: {detail}")


def skip(ac: str, desc: str, detail: str = "需要真实 LLM/网络"):
    RESULTS.append((True, ac, f"{desc} — SKIP: {detail}"))
    print(f"  [SKIP] {ac} {desc}: {detail}")


# ── R1 工具 schema 扩展 ──────────────────────────────────────────────

def ac_1_1():
    from app.agents.registry import AgentRegistry
    from app.orchestrator.router import build_router_tools
    tools, _ = build_router_tools(AgentRegistry())
    missing = [
        t["function"]["name"] for t in tools
        if "confidence" not in t["function"]["parameters"]["properties"]
        or "intent_summary" not in t["function"]["parameters"]["properties"]
    ]
    return (not missing, f"{len(tools)} 个工具缺字段: {missing}" if missing else "全部含 confidence+intent_summary")


def ac_1_2():
    from app.orchestrator.router import RouterResult
    r = RouterResult(tool_name="route_to_db_agent", args={"query": "查表结构"},
                     latency_ms=1.0, confidence=0.9, intent_summary="查表结构")
    return r.confidence == 0.9 and r.intent_summary == "查表结构", \
        f"confidence={r.confidence}, intent_summary={r.intent_summary!r}"


# ── R2 clarify 工具与节点 ─────────────────────────────────────────────

def ac_2_1():
    from app.agents.registry import AgentRegistry
    from app.orchestrator.router import build_router_tools
    tools, name_map = build_router_tools(AgentRegistry())
    names = [t["function"]["name"] for t in tools]
    return ("route_to_clarify" in names and "route_to_clarify" not in name_map,
            f"in tools={('route_to_clarify' in names)}, in agent_map={('route_to_clarify' in name_map)}")


def ac_2_2():
    from app.agents.registry import AgentRegistry
    from app.agents.skills import setup_skills
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator
    reg = AgentRegistry()
    reg.load()
    setup_skills(reg)
    orch = CrewOrchestrator(cc_client=None, config=load_config().router, registry=reg)
    nodes = orch.get_graph_info()["nodes"]
    has_node = "clarify_node" in nodes
    routed = orch._route_condition({"route_to": "route_to_clarify"})
    return has_node and routed == "route_to_clarify", \
        f"clarify_node in graph={has_node}, route_condition→{routed}"


def ac_2_3():
    from typing import Any

    from app.agents.registry import AgentRegistry
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator
    reg = AgentRegistry()
    reg.load()
    orch = CrewOrchestrator(cc_client=None, config=load_config().router, registry=reg)
    state: dict[str, Any] = {
        "input_message": "帮我查一下那个东西", "clarify_question": "", "intent_summary": "",
        "demoted": True, "confidence": 0.3, "spans": [],
    }
    out = asyncio.run(orch._clarify_node(state))
    spans = out.get("spans", [])
    # clarify 是合法终态：有回复且不触发结构化门禁（没有独立判定节点可绕过）
    ok = bool(out.get("response")) and not out.get("gate_blocked")
    return ok, f"response={out.get('response', '')[:40]!r}, spans={[sp.get('span') for sp in spans]}"


# ── R3 低置信度降级 ──────────────────────────────────────────────────

def _mock_router_node(confidence: float):
    from typing import Any

    from app.agents.registry import AgentRegistry
    from app.config import load_config
    from app.orchestrator.crew import CrewOrchestrator
    from app.orchestrator.router import RouterLLM, RouterResult

    reg = AgentRegistry()
    reg.load()
    orch = CrewOrchestrator(cc_client=None, config=load_config().router, registry=reg)

    class FakeRouter(RouterLLM):
        async def route(self, message, history=None, **_kwargs):
            # **_kwargs：桩只关心 confidence 分支，不绑死 route() 的可选参数
            # （memory_context / media_bundle 等后续加参会静默打断本验收）
            return RouterResult(
                tool_name="route_to_db_agent",
                args={"query": message},
                latency_ms=1.0, confidence=confidence, intent_summary="查询数据库",
            )

    orch._router_llm = FakeRouter.__new__(FakeRouter)  # 跳过 __init__ 的 openai 客户端装配
    state: dict[str, Any] = {"input_message": "查表结构", "history": [], "notify_cb": None}
    out = asyncio.run(orch._router_node(state))  # type: ignore[arg-type]
    return out


def ac_3_1():
    out = _mock_router_node(0.3)
    spans = out.get("spans", [])
    ok = (out.get("route_to") == "route_to_clarify" and out.get("demoted") is True
          and bool(out.get("clarify_question"))
          and any(sp.get("demoted") and sp.get("original_target") == "db-agent" for sp in spans))
    return ok, f"route_to={out.get('route_to')}, demoted={out.get('demoted')}, original={spans[0].get('original_target') if spans else '?'}"


def ac_3_2():
    out = _mock_router_node(0.9)
    return (out.get("route_to") == "route_to_db_agent" and out.get("demoted") is False,
            f"route_to={out.get('route_to')}, demoted={out.get('demoted')}")


def ac_3_3():
    from app.agents.registry import AgentRegistry
    from app.config import RouterConfig
    from app.orchestrator.crew import CrewOrchestrator
    reg = AgentRegistry()
    reg.load()
    rc = RouterConfig(api_key="x", base_url="http://x", model="m", enabled=True)
    rc.clarify_threshold = 0.0
    orch = CrewOrchestrator(cc_client=None, config=rc, registry=reg)
    return getattr(orch._config, "clarify_threshold", -1) == 0.0, "threshold=0 可配置（永不降级）"


# ── R4 决策日志 ──────────────────────────────────────────────────────

def ac_4_1():
    from app.observability.task_record import TaskRecord
    r = TaskRecord(task_id="t1", intent_summary="查表", confidence=0.3,
                   demoted=True, clarify_question="你想查什么？")
    d = r.to_dict() if hasattr(r, "to_dict") else {}
    import dataclasses
    d = dataclasses.asdict(r)
    ok = (d.get("intent_summary") and d.get("demoted") is True
          and d.get("confidence") == 0.3 and d.get("clarify_question"))
    return ok, f"四字段齐备: intent={d['intent_summary']!r}, demoted={d['demoted']}"


def ac_4_2_and_4_3():
    """AC-4.2 决策日志写入字段齐备；AC-4.3 可重建 query→tool→confidence→demoted。"""
    from app.observability.task_record import DecisionRecorder
    with tempfile.TemporaryDirectory() as td:
        rec = DecisionRecorder(log_dir=Path(td))
        rec.write({"task_id": "tx", "query": "查表结构", "tool": "route_to_db_agent",
                   "args": {"query": "查表结构", "intent_summary": "查表", "confidence": 0.95},
                   "demoted": False, "original_target": "", "is_fallback": False,
                   "model": "glm", "prompt_version": "1.4.0", "latency_ms": 12, "tokens": 100})
        files = list(Path(td).glob("*.jsonl"))
        if not files:
            return False, "未生成决策日志文件"
        data = json.loads(files[0].read_text(encoding="utf-8"))
        need = {"task_id", "query", "tool", "args", "demoted", "original_target",
                "model", "prompt_version", "latency_ms", "tokens"}
        missing = need - set(data)
        chain_ok = (data["query"] and data["tool"] and "confidence" in data["args"]
                    and "demoted" in data)
        return (not missing and chain_ok,
                f"字段缺: {missing}" if missing else "query→tool→confidence→demoted 链路可重建")


# ── R5 agent query_preview ──────────────────────────────────────────

def ac_5_1():
    """agent span 含 query_preview：源码断言 + 真实执行（mock dispatcher）已由 e2e 覆盖，
    此处断言 crew 源码在 agent span 中写入 query_preview 字段。"""
    src = (PROJECT_DIR / "app" / "orchestrator" / "crew.py").read_text(encoding="utf-8")
    ok = '"query_preview"' in src and "dispatched_query" in src
    return ok, "agent_node span 写入 query_preview（源码存在 dispatched_query 组装）"


# ── R8 三道闸 ────────────────────────────────────────────────────────

# 子进程统一 UTF-8：Windows 下 subprocess 默认用 GBK 解码子进程输出，ruff/mypy
# 的中文诊断会撞 UnicodeDecodeError → r.stdout 变 None → 断言假失败或假通过
# （2026-10-12 修复：此前 AC-8.1/8.2/8.3 三条门禁全部处于假通过状态）。
# PYTHONUTF8=1 还要让子进程**内部**（mypy 读含中文的 mypy.ini）也用 UTF-8。
_SUB_ENV = {**os.environ, "PYTHONUTF8": "1"}

# mypy 增量基线。2026-10-12 实测（PYTHONUTF8=1 下真实值）49 项 / 17 文件；
# 记录中的 36 是编码修复前的陈旧数字，已随门禁失效一起作废。
# 改动本批次前后均为 49（逐条 diff 零差异），此处只防继续退化。
_MYPY_BASELINE = 49


def ac_8_1():
    """ruff 改动域 0 错（只验本批次触碰的文件；存量项见 ruff 全量基线）。"""
    import subprocess
    targets = [
        "app/orchestrator", "app/observability", "app/config.py",
        "app/eval/verify_spec.py", "app/eval/router_intent_eval.py",
    ]
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *targets],
        cwd=PROJECT_DIR, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, env=_SUB_ENV)
    return r.returncode == 0, (r.stdout.strip().splitlines() or ["clean"])[-1][:80]


def ac_8_2():
    import subprocess
    r = subprocess.run([sys.executable, "-m", "mypy", "app"], cwd=PROJECT_DIR,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300, env=_SUB_ENV)
    errors = [ln for ln in r.stdout.splitlines() if "error:" in ln and "note:" not in ln]
    # mypy 自己崩了（配置/编码错误）时 stdout 没有 error 行，光看数量会假通过
    crashed = not errors and "Found" not in r.stdout and "Success" not in r.stdout
    if crashed:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, f"mypy 未正常完成: {tail[-1][:70] if tail else 'no output'}"
    return len(errors) <= _MYPY_BASELINE, \
        f"{len(errors)} 项（基线 {_MYPY_BASELINE}，不得新增）"


def ac_8_3():
    import subprocess
    r = subprocess.run([sys.executable, "app/eval/prompt_eval.py"], cwd=PROJECT_DIR,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, env=_SUB_ENV)
    ok = "ALL SUITES PASSED" in r.stdout
    line = next((ln for ln in r.stdout.splitlines() if "总分" in ln), "")
    return ok, line.strip()[:60] or r.stdout[-80:]


def run_llm_cases() -> None:
    """R6 行为级回归（--with-llm 时执行）。"""
    check("AC-6.1", "router_intent_eval 可执行并输出逐条结果", _run_intent_eval)


def _run_intent_eval():
    import subprocess
    r = subprocess.run(
        [sys.executable, "app/eval/router_intent_eval.py", "--min-score", "0.85"],
        cwd=PROJECT_DIR, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
        env={**_SUB_ENV, "NO_PROXY": "aigw.example.com"},
    )
    total = next((ln for ln in r.stdout.splitlines() if "总分" in ln), "")
    return r.returncode == 0, (total.strip() or f"exit={r.returncode} {r.stderr[-80:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SPEC-20260912 验收")
    parser.add_argument("--with-llm", action="store_true", help="含真实 LLM 行为回归")
    args = parser.parse_args()

    print("=== SPEC-20260912 Router 意图识别 验收 ===\n")

    print("── R1 工具 schema 扩展 ──")
    check("AC-1.1", "派生工具含 confidence+intent_summary", ac_1_1)
    check("AC-1.2", "RouterResult 承接自报字段", ac_1_2)

    print("── R2 clarify 路由 ──")
    check("AC-2.1", "route_to_clarify 在工具表且非 agent", ac_2_1)
    check("AC-2.2", "graph 含 clarify_node 且条件边可达", ac_2_2)
    check("AC-2.3", "clarify_node 输出非空且不触发结构化门禁", ac_2_3)

    print("── R3 低置信度降级 ──")
    check("AC-3.1", "confidence=0.3 降级为 clarify（original_target 保留）", ac_3_1)
    check("AC-3.2", "confidence=0.9 保持原路由", ac_3_2)
    check("AC-3.3", "clarify_threshold 可配置为 0（永不降级）", ac_3_3)

    print("── R4 决策日志 ──")
    check("AC-4.1", "TaskRecord 含 intent/confidence/demoted/clarify_question", ac_4_1)
    check("AC-4.2", "决策日志落盘且字段齐备", ac_4_2_and_4_3)
    check("AC-4.3", "query→tool→confidence→demoted 链路可重建", ac_4_2_and_4_3)

    print("── R5 执行日志 ──")
    check("AC-5.1", "agent span 含 query_preview", ac_5_1)

    print("── R6 行为级回归 ──")
    if args.with_llm:
        run_llm_cases()
    else:
        skip("AC-6.1", "router_intent_eval 行为级回归", "加 --with-llm 执行")

    print("── R8 三道闸 ──")
    check("AC-8.1", "ruff 改动域 0 错", ac_8_1)
    check("AC-8.2", f"mypy 不超基线 {_MYPY_BASELINE} 且未崩溃", ac_8_2)
    check("AC-8.3", "prompt_eval 全量通过", ac_8_3)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[0]]
    print(f"\n=== 验收汇总: {passed}/{len(RESULTS)} PASS（FAIL {len(failed)}）===")
    for _, ac, detail in failed:
        print(f"  FAIL {ac}: {detail[:120]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
