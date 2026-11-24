"""路由规则注入与真实技能目录的端到端验证。

这不是单测：它真的拉起 dsh 进程、真的加载技能目录、真的跑 golden 用例。
放在 `deploy/dsh/` 而不是 `tests/`，因为它需要 dsh 安装与网关凭据。

用法：
    python deploy/dsh/verify_routing.py             # 全量 golden
    python deploy/dsh/verify_routing.py --limit 10  # 抽样
    python deploy/dsh/verify_routing.py --min-score 0.9

退出码：通过率 ≥ min-score → 0；否则 1。网络/进程不可用 → 2（不允许静默跳过）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR / ".env")

from app.config import load_config  # noqa: E402
from app.dsh.adapter import DshAdapter, DshError  # noqa: E402

logger = logging.getLogger("verify_routing")

GOLDEN = PROJECT_DIR / "bot" / "prompts" / "_golden" / "router-intent.json"
DEFAULT_PATCH = PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "cordis.patch.yml"

# 技能目录里能承载路由的业务能力（与 skills.allowlist 一致）。
# golden 的 expected_tool 用的是工具名（`route_to_digital_oracle_agent`），
# 而技能名是 `digital-oracle-agent`——比对前统一成 kebab-case 并剥掉 route_to_ 前缀。
def normalize(name: str) -> str:
    """`route_to_digital_oracle_agent` / `digital-oracle-agent` 统一成 `digital-oracle-agent`。"""
    n = name.strip().lower()
    for prefix in ("route_to_", "route-to-"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.replace("_", "-")


_KNOWN_LABELS = frozenset({
    "direct-answer", "clarify", "help",
    "schedule-agent", "digital-oracle-agent", "aihot", "skill-catalog-agent",
})


def _pick_label(raw: str) -> str:
    """从回复里取出第一行的能力名。

    模型有时会在标签前加一句过渡（「好的，我来看看」），所以从前面几行里取
    第一个能对上已知标签的行，而不是死取第 0 行——判分要衡量「它选了谁」，
    不是「它的措辞像不像机器」。
    """
    known = _KNOWN_LABELS
    for line in raw.splitlines()[:4]:
    # 逐词扫而不是整行比对：模型常把标签与正文写在同一行
    # （`db-agent` 与「这条交给…」粘在一起），整行比对会漏掉。
        for token in re.split(r"[\s`*#,，:：。]+", line):
            candidate = normalize(token)
            if candidate in known:
                return candidate
            # 标签与正文之间没空格时会被扫成一个词（`aihotaihot`、
            # `schedule-agent` 紧贴正文）：按前缀命中，取最长的那个复用名。
            prefix_hits = [k for k in known if candidate.startswith(k)]
            if prefix_hits:
                return max(prefix_hits, key=len)
    return normalize(raw.splitlines()[0]) if raw else "<empty>"



# 工具 → 能力名：dsh 链路没有「首行标签」这回事，分派结果体现为实际调了谁。
# 只收**业务能力**的工具；feishu 读写（read_conversation / read_doc）、文件与
# shell 工具是通用手段，不代表分派到某个业务能力。
_TOOL_LABEL = {
    "gather_signals": "digital-oracle-agent",
    "find_similar_skills": "skill-catalog-agent",
    "get_skill_status": "skill-catalog-agent",
    "get_skill_status_board": "skill-catalog-agent",
    "review_skill": "skill-catalog-agent",
    "list_team_skills": "skill-catalog-agent",
    "refresh_skill_catalog": "skill-catalog-agent",
    "skill_catalog_query": "skill-catalog-agent",
    "create_scheduled_task": "schedule-agent",
    "list_tasks": "schedule-agent",
    "get_task": "schedule-agent",
    "pause_task": "schedule-agent",
    "resume_task": "schedule-agent",
    "cancel_task": "schedule-agent",
    "list_schedules": "schedule-agent",
    "daily_report": "aihot",
    "daily_report_card": "aihot",
}

# `skill` 工具载入某项技能 = 分派到该技能（入参里带 name）。
_SKILL_TOOL = "skill"


def _infer_label(called: list[tuple[str, str]], raw: str) -> str:
    """从「本轮调了什么 + 答了什么」推断它实际选了谁。

    判定顺序：业务工具 → 载入的技能 → 无工具时看答复形态（反问 / 能力清单 /
    自答）。这不是宽松比对：每一项都对应可观察行为，只是把「它选了谁」从
    首行文本换成了它真正做的事。
    """
    for title, raw_input in called:
        short = title.split("__")[-1] if "__" in title else title
        if short in _TOOL_LABEL:
            return _TOOL_LABEL[short]
        if short == _SKILL_TOOL:
            try:
                name = str(json.loads(raw_input or "{}").get("name") or "")
            except ValueError:
                name = ""
            if name:
                return normalize(name)
    text = raw.strip()
    if "？" in text[-400:] or "?" in text[-400:]:
        # 没有落到任何业务能力、又在向用户提问 = 澄清
        return "clarify"
    listed = sum(1 for k in _KNOWN_LABELS if k in text)
    if listed >= 3:
        return "help"
    return "direct-answer"


def _mcp_specs(cfg) -> list[dict]:
    """按 main.py 的同一份汇总逻辑生成 mcpServers（含各 agent 的数据源）。"""
    from app.agents.registry import AgentRegistry
    from app.dsh.mcp_specs import build_dsh_supervisor_config
    from app.dsh.supervisor import DshSupervisor

    registry = AgentRegistry()
    registry.load()
    sup_cfg = build_dsh_supervisor_config(cfg.dsh, cfg.mcp, registry.enabled())
    return DshSupervisor(sup_cfg).mcp_server_specs()


def load_cases(limit: int | None = None) -> list[dict]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = list(data.get("cases", []))
    return cases[:limit] if limit else cases


async def _replay_history(adapter: DshAdapter, session_id: str, turn: dict) -> None:
    """把 golden 里的历史轮次灌进会话。

    用户轮直接提交；助手轮是「我已经答过的内容」，用一段带标注的文本还原——
    dsh 的会话不支持外部注入 assistant 消息，但用户在上下文里看到它同样成立。
    """
    role = str(turn.get("role") or "user")
    content = str(turn.get("content") or "")
    if not content:
        return
    if role == "assistant":
        content = "【上一轮机器人答复】\n" + content
    await adapter.session_prompt(session_id, content)


async def run(
    adapter: DshAdapter, cases: list[dict], mcp_servers: list[dict] | None = None
) -> list[dict]:
    # 挂上数据源工具：路由判对不等于答对。回归环境要和线上一致，否则
    # 「已转交 db-agent，请稍候」这种空转答复也会被判成通过。
    # cwd 用仓库根：部署目录里没有 .dsh/ 也没有 .git，MCP 子进程在那种 cwd 下
    # 初始化会失败，表现为 session/new 直接 Internal error——看着像 dsh 的问题，
    # 其实是工作目录的问题。
    session_id = await adapter.session_new(str(PROJECT_DIR), mcp_servers)
    results = []
    # golden 里带 history 的用例（多轮追问、承接反问）只有在上下文里跑才有意义：
    # 「查库那个」单发一句等于让模型凭空猜，判分失真。给它们各开一个新会话，
    # 先把 history 提交进去再发待判消息。
    needs_history = {id(c) for c in cases if c.get("history")}
    for case in cases:
        target_session = session_id
        if id(case) in needs_history:
            target_session = await adapter.session_new(str(PROJECT_DIR), mcp_servers)
            for turn in case["history"]:
                await _replay_history(adapter, target_session, turn)
        texts: list[str] = []
        called: list[tuple[str, str]] = []

        def collect(ev: object, _texts: list[str] = texts,
                    _called: list[tuple[str, str]] = called) -> None:
            # 技能/工具调用是 dsh 链路真正的「分派结果」：系统提示词里没有
            # 首行能力名契约（v2.0.0 移除），判分必须看它实际动了什么。
            if type(ev).__name__ == "ToolCallEvent":
                _called.append((getattr(ev, "title", "") or "",
                                getattr(ev, "raw_input", "") or ""))
                return
            # 只要可见回复块：thought 是思维链，让它参与判分会把「模型想了什么」
            # 当成「模型选了什么」。
            if type(ev).__name__ != "TextChunkEvent":
                return
            if getattr(getattr(ev, "kind", None), "name", "") != "MESSAGE":
                return
            text = getattr(ev, "text", "")
            if isinstance(text, str) and text:
                _texts.append(text)

        adapter._on_event = collect  # 临时替换回调，避免影响其它消费方
        try:
            await adapter.session_prompt(target_session, case["input"])
        except DshError as e:
            if "already in flight" in str(e):
                # 上一条 prompt 超时后服务端还没结算，此时继续提交只会连环失败。
                # 取消后把这条记为失败——判分要如实反映「这条没跑成」，
                # 而不是把后面所有用例一起判成 error。
                logger.warning("上一条 prompt 未结算，取消后继续: %s", e)
                await adapter.session_cancel(session_id)
                await asyncio.sleep(2.0)
                results.append({"input": case["input"], "want": case["expected_tool"],
                                "got": "<error: 上一条超时未结算，本轮跳过>", "passed": False})
                continue
            results.append({"input": case["input"], "want": case["expected_tool"],
                            "got": f"<error: {e}>", "passed": False})
            continue
        if id(case) in needs_history:
            await adapter.session_close(target_session)
        raw = "".join(texts).strip()
        got = _infer_label(called, raw)
        want = normalize(case["expected_tool"])
        acceptable = {normalize(x) for x in case.get("acceptable_tools") or []}
        passed = got == want or got in acceptable
        results.append({"input": case["input"], "want": want, "got": got,
                        "raw": raw[:80], "passed": passed})
    return results


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=0.92)
    parser.add_argument("--offset", type=int, default=0, help="从第几条开始（分段跑）")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not cfg.dsh.resolved_api_key:
        print("缺少网关凭据：config.toml 的 [projects.dsh].api_key 与 AIGW_API_KEY 都为空",
              file=sys.stderr)
        return 2

    cases = load_cases()[args.offset:]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("没有匹配的用例（offset 超出范围）", file=sys.stderr)
        return 2
    adapter = DshAdapter(
        dsh_home=cfg.dsh.dsh_home or str(PROJECT_DIR.parent / ".dsh-bot"),
        patch_path=str(DEFAULT_PATCH),
        env={"AIGW_API_KEY": cfg.dsh.resolved_api_key},
        # 单例超时压到 120s：挂了工具以后个别用例会走多轮工具调用，
        # 默认 1800s 会让一条卡住的用例拖垮整轮回归（外层 600s 就断了）。
        prompt_timeout=120.0,
        # 十个 MCP 子进程（3 个 Python + 7 个 node）冷启动要几十秒：
        # 默认 30s 的请求超时会在 session/new 上先炸，看起来像 dsh 挂了。
        request_timeout=180.0,
    )
    try:
        await adapter.start()
    except DshError as e:
        print(f"dsh 不可用：{e}", file=sys.stderr)
        return 2

    try:
        results = await run(adapter, cases, _mcp_specs(cfg))
    finally:
        await adapter.shutdown()

    passed = sum(1 for r in results if r["passed"])
    score = passed / len(results) if results else 0.0
    for r in results:
        if not r["passed"]:
            print(f"  ✗ [{r['input'][:36]}] want={r['want']} got={r['got']} raw={r.get('raw', '')[:50]}")
    print(f"\n路由通过 {passed}/{len(results)} = {score * 100:.1f}%（门槛 {args.min_score * 100:.0f}%）")
    return 0 if score >= args.min_score else 1


if __name__ == "__main__":
    # 报告里有中文与 ✗ 标记：Windows 控制台默认 GBK，不改编码会在打印结果时崩，
    # 而不是在判定时崩——看起来像「跑完了没输出」。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
