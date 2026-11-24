#!/usr/bin/env python3
"""Prompt Evaluation Gate — golden test runner for prompt changes.

Reads golden test cases from bot/prompts/_golden/*.json and validates agent
prompts against them. Each golden case is a structured "input → expected
behavior" pair; the evaluator checks that a prompt file still encodes the
expected behavior (by keyword/label match on the prompt content).

Usage:
    python app/eval/prompt_eval.py                     # Run all suites
    python app/eval/prompt_eval.py --ref router/system_prompt.md   # One suite
    python app/eval/prompt_eval.py --list               # List suites
    python app/eval/prompt_eval.py --json               # Machine-readable output

Exit codes:
    0  all suites pass (score >= threshold)
    1  some suite below threshold or failed to load
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/eval/ -> feishu-agent-platform/
GOLDEN_DIR = PROJECT_DIR / "bot" / "prompts" / "_golden"
PROMPTS_DIR = PROJECT_DIR / "bot" / "prompts"

# 直接执行本脚本时把项目根加入 sys.path，保证能导入运行时模块
# （占位符同源渲染 + 别名一致性自检都依赖它；失败则降级为纯文本评分）
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

logger = logging.getLogger("prompt_eval")

MIN_SCORE = 0.80  # 每套用例最低通过率


# ── 数据源 ──────────────────────────────────────────────────────────


def load_prompt_content(ref: str) -> str:
    """按 golden ref 定位对应 prompt 文件并读取正文（去除 frontmatter）。"""
    ref = ref.lstrip("/")
    # 提示词已单文件化（没有版本目录、没有 stable 副本）：ref 指向的文件本身
    # 就是运行时真源，golden ref 直接命中 candidates[0]，无需再做路径重映射。
    candidates = [
        PROMPTS_DIR / ref,
        PROMPTS_DIR / f"{ref}.md",
    ]
    for p in candidates:
        if p.is_file():
            content = p.read_text(encoding="utf-8")
            # 去除 frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) == 3:
                    content = parts[2]
            # {{AGENT_LIST}} 等占位符与运行时同源渲染 —— 门禁测的是真实产物
            return _render_registry_placeholders(content)
    return ""


def _render_registry_placeholders(content: str) -> str:
    """渲染 Registry 单源占位符（复用运行时的 build_router_tools 派生逻辑）。

    router 模块不可导入时返回原文（保持本脚本独立性，此时相关用例可能失分）。
    """
    if "{{" not in content:
        return content
    try:
        from app.agents.registry import AgentRegistry
        from app.orchestrator.router import build_router_tools
    except Exception as e:
        logger.warning("[prompt_eval] placeholder render skipped (import failed): %s", e)
        return content

    registry = AgentRegistry()
    if "{{AGENT_LIST}}" in content:
        tools, _ = build_router_tools(registry)
        lines = [f"- {t['function']['name']} — {t['function']['description']}" for t in tools]
        content = content.replace("{{AGENT_LIST}}", "\n".join(lines))
    if "{{AGENT_LIST_DECOMPOSE}}" in content:
        lines = [
            f"- {a.name}：{a.description or a.display_name}" for a in registry.routable()
        ]
        content = content.replace("{{AGENT_LIST_DECOMPOSE}}", "\n".join(lines))
    return content


def _keyword_hits(prompt: str, keywords: list[str]) -> int:
    """统计 prompt 中命中期望关键字的数量（用于标签匹配）。"""
    hits = 0
    for kw in keywords:
        if kw.lower() in prompt.lower():
            hits += 1
    return hits


# 路由工具 → 中文语义别名（prompt 内多以中文标签描述路由目标）。
# ⚠️ 新增 Agent 时需同步此表（与 app/orchestrator/router.AGENT_NAME_MAP 保持一致，
# 启动时会做一致性自检，缺失打 warning——import 失败时静默跳过自检保持脚本独立）。
_ROUTE_ALIASES: dict[str, list[str]] = {
    "route_to_db_agent": ["数据库查询", "db agent", "表结构"],
    "route_to_config_agent": ["配置管理", "config agent", "nacos"],
    "route_to_daily_agent": ["技术日报", "daily agent", "日报"],
    "route_to_help": ["帮助", "help", "自我介绍", "版本"],
    "route_to_dev_bot": ["综合业务", "dev bot", "代码分析"],
    "route_to_schedule_agent": ["定时任务", "schedule agent", "周期任务"],
    "route_to_digital_oracle_agent": ["数字先知", "概率预测", "digital oracle", "市场信号"],
    "route_to_skill_catalog_agent": ["团队 Skill 目录", "skill catalog", "立项查重",
                                     "技能建设情况", "规范状态", "技能库现状"],
    "direct_answer": ["直接回答", "direct answer", "打招呼", "闲聊"],
}


def _check_alias_consistency() -> None:
    """启动自检：别名表与 router 派生的 AGENT_NAME_MAP 对账（缺失打 warning）。"""
    try:
        from app.orchestrator.router import AGENT_NAME_MAP
    except Exception:
        return  # 脚本独立性：router 不可导入时静默跳过
    for tool in AGENT_NAME_MAP:
        if tool not in _ROUTE_ALIASES:
            logger.warning("[prompt_eval] _ROUTE_ALIASES missing entry for %r "
                           "(new Agent? add aliases to keep golden gate accurate)", tool)


def score_case(prompt: str, case: dict) -> tuple[bool, str]:
    """评估单个 golden 用例。

    打分逻辑：
    - expected_tool：路由目标 → 通过 _ROUTE_ALIASES 中文别名匹配 prompt 语义
      （函数名 + 中文标签任一命中即可视为该路由被编码）。
    - expected_behavior：检查其中出现的函数名与源 id 关键字。
    - expected：作为补充描述（仅在有结构化期望时作为辅助）。
    - tags：仅用于分类与展示，不作为扣分依据。

    Returns:
        (passed, reason)
    """
    exp_tool = case.get("expected_tool", "")
    exp_behavior = case.get("expected_behavior", "")
    exp_text = case.get("expected", "")

    keywords: list[str] = []
    if exp_tool:
        keywords.append(exp_tool)
        # 中文别名（如 router 用「数据库查询」而非英文）
        for alias in _ROUTE_ALIASES.get(exp_tool, []):
            keywords.append(alias)
    # require_extra_call：混合意图第二跳须编码进 prompt（如 help+专业域）
    extra_tool = case.get("require_extra_call", "")
    if extra_tool:
        keywords.append(extra_tool)
    if exp_behavior:
        for m in re.findall(r"[a-z_]+(?=\()", exp_behavior):
            keywords.append(m)
        # 旧数据源 id（tech_daily 等）已随目录读取下线，不再作为关键字

    keywords = list(dict.fromkeys(k for k in keywords if k))

    if keywords:
        hits = _keyword_hits(prompt, keywords)
        # 函数名本身命中即算该路由已被编码（中文别名是辅助佐证）
        if exp_tool and exp_tool in prompt:
            return True, f"路由工具 '{exp_tool}' 已在 prompt 编码"
        score = hits / len(keywords)
        if score >= MIN_SCORE:
            return True, f"命中 {hits}/{len(keywords)} 关键字"
        return False, f"仅命中 {hits}/{len(keywords)} 关键字: {keywords}"

    # 无结构化期望 → 退化到 expected 文本关键字。
    # 连续中文短语拆成 2 字语义片段（如「限制结果行数」→ 限制/结果/行数），
    # 语义相关片段命中即计分（避免整体短语因表述差异漏判）。
    exp_kws = [w for w in re.split(r"[\s，。；、()/|]+", exp_text) if len(w) >= 2]
    frag_kws: list[str] = []
    for w in exp_kws:
        if re.fullmatch(r"[一-鿿]+", w) and len(w) > 2:
            # 按 2 字滑窗取「限制」「结果」「行数」—— 只取语义上自洽的片段
            for i in range(0, len(w) - 1, 2):
                frag_kws.append(w[i:i + 2])
        else:
            frag_kws.append(w)
    all_kws = list(dict.fromkeys(exp_kws + frag_kws))
    hits = _keyword_hits(prompt, all_kws)
    if all_kws:
        score = hits / len(all_kws)
        if score >= MIN_SCORE:
            return True, f"expected 命中 {hits}/{len(all_kws)}"
        return False, f"expected 仅命中 {hits}/{len(all_kws)}: {all_kws}"
    return False, "无任何期望匹配依据"


# ── 套件执行 ─────────────────────────────────────────────────────────


def load_suites() -> list[dict]:
    """加载所有 golden json 套件。"""
    if not GOLDEN_DIR.is_dir():
        logger.warning(f"Golden dir not found: {GOLDEN_DIR}")
        return []
    suites = []
    for f in sorted(GOLDEN_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_path"] = str(f)
            suites.append(data)
        except Exception as e:
            logger.error(f"Failed to parse golden {f.name}: {e}")
    return suites


def _check_index_freshness() -> str | None:
    """INDEX.md 生成区是否与 frontmatter 一致；不一致返回错误说明。

    单源是 frontmatter，INDEX 是派生物——不同步就过期（曾经 INDEX 里
    10 个版本号与 golden 用例数全错且无人发现，故转为门禁项）。
    """
    import contextlib
    import io

    try:
        from app.eval.prompt_index import apply as _index_apply
    except Exception as exc:  # 生成器缺失不该放行，但也不让门禁崩
        return f"prompt_index 不可用: {exc}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = _index_apply(check=True)
    return None if rc == 0 else (buf.getvalue().strip() or "INDEX 生成区已过期")


def run_suite(suite: dict) -> dict[str, Any]:
    """运行单个套件，返回统计。"""
    ref = suite.get("ref", "?")
    prompt = load_prompt_content(ref)
    cases = suite.get("cases", [])
    results = []
    passed = 0
    for case in cases:
        ok, reason = score_case(prompt, case)
        results.append({"input": case.get("input", ""), "passed": ok, "reason": reason})
        if ok:
            passed += 1
    total = len(cases)
    score = (passed / total) if total else 0.0
    return {
        "ref": ref,
        "prompt_loaded": bool(prompt),
        "total": total,
        "passed": passed,
        "score": round(score, 3),
        "passed_gate": score >= MIN_SCORE and bool(prompt),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prompt golden test gate")
    parser.add_argument("--ref", help="只跑指定 ref 的套件")
    parser.add_argument("--list", action="store_true", help="列出可用套件")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--min-score", type=float, default=MIN_SCORE, help="通过阈值")
    args = parser.parse_args()

    suites = load_suites()
    if args.list:
        for s in suites:
            print(f"  {s['ref']}  (v{s['version']}, {len(s['cases'])} cases)")
        return 0

    if args.ref:
        suites = [s for s in suites if s["ref"] == args.ref]
        if not suites:
            print(f"❌ 未找到套件: {args.ref}")
            return 1

    if not suites:
        print("❌ 无 golden 套件，请先补 bot/prompts/_golden/*.json")
        return 1

    _check_alias_consistency()
    index_err = _check_index_freshness()
    results = [run_suite(s) for s in suites]
    all_pass = all(r["passed_gate"] for r in results) and index_err is None
    global_total = sum(r["total"] for r in results)
    global_passed = sum(r["passed"] for r in results)
    global_score = global_passed / global_total if global_total else 0.0

    if args.json:
        print(json.dumps({
            "all_pass": all_pass,
            "total": global_total,
            "passed": global_passed,
            "score": round(global_score, 3),
            "min_score": args.min_score,
            "suites": results,
        }, ensure_ascii=False, indent=2))
        return 0 if all_pass else 1

    print(f"=== Prompt Evaluation Gate (min_score={args.min_score}) ===")
    for r in results:
        status = "✅ PASS" if r["passed_gate"] else "❌ FAIL"
        prompt_state = "prompt 缺失" if not r["prompt_loaded"] else ""
        print(f"  [{status}] {r['ref']}  score={r['score']:.0%} ({r['passed']}/{r['total']}) {prompt_state}")
        if not r["passed_gate"]:
            for c in r["cases"]:
                if not c["passed"]:
                    print(f"         ✗ {c['input'][:50]} — {c['reason']}")
    if index_err:
        print("  [❌ FAIL] INDEX.md 生成区过期 — 运行 `python app/eval/prompt_index.py`")
        for line in index_err.splitlines()[:8]:
            print(f"         {line}")
    print(f"\n  总分: {global_passed}/{global_total} = {global_score:.0%}")
    if all_pass:
        print("Result: ALL SUITES PASSED 🎉")
        return 0
    print("Result: GATE BLOCKED — 部分套件未达标")
    return 1


if __name__ == "__main__":
    sys.exit(main())
