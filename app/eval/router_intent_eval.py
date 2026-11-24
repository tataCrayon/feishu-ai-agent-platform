"""意图识别行为级回归（dsh 链路）— 真实拉起 dsh、真实加载技能目录、真跑 golden。

与 `prompt_eval.py`（静态：提示词里有没有编码路由语义）互补：本脚本把 golden
用例当作真实用户消息提交给 dsh 会话，断言**模型实际选中的能力名**。
改路由规则 / 技能白名单 / 默认模型后必须过本门禁。

为什么合并到 `verify_routing.py` 的实现上：路由的判定已经整段搬进 dsh 会话，
两套脚本各维护一份「取标签」逻辑必然漂移——判分口径只留一处（本仓规矩：
同一事实写一遍）。

用法：
    python app/eval/router_intent_eval.py                 # 全量，min-score 0.92
    python app/eval/router_intent_eval.py --min-score 0.9
    python app/eval/router_intent_eval.py --case-tags clarify   # 只跑某类用例
    python app/eval/router_intent_eval.py --isolated      # 每例新会话（慢，查上下文污染）

退出码：总分 ≥ min-score → 0；否则 1。dsh 不可用 → 2（不允许静默跳过）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR / ".env")

from app.config import load_config  # noqa: E402
from app.dsh.adapter import DshAdapter, DshError  # noqa: E402
from app.dsh.supervisor import SupervisorConfig  # noqa: E402
from deploy.dsh.verify_routing import (  # noqa: E402
    GOLDEN,
    _infer_label,
    normalize,
)

DEFAULT_PATCH = PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "cordis.patch.yml"


def load_cases(tag: str | None = None) -> list[dict]:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = list(data.get("cases", []))
    if tag:
        cases = [c for c in cases if tag in (c.get("tags") or [])]
    return cases


def _judge(case: dict, raw: str, called: list[tuple[str, str]] | None = None) -> dict:
    """按 golden 的判定口径给一条用例打分。

    `acceptable_tools` 是给模型留的合理余地（多个能力都能处理同一请求），
    不算松动：它定义的是「答案集」，不是「宽松的比对」。
    """
    want = normalize(case["expected_tool"])
    acceptable = {normalize(x) for x in case.get("acceptable_tools") or []}
    got = _infer_label(called or [], raw)
    return {
        "input": case["input"],
        "want": want,
        "got": got,
        "raw": raw[:120],
        "passed": got == want or got in acceptable,
    }


async def _collect(adapter: DshAdapter, session_id: str, text: str) -> tuple[str, list]:
    texts: list[str] = []
    called: list[tuple[str, str]] = []

    def collect(ev: object) -> None:
        # 只要可见回复块：thought 是思维链，让它参与判分会把「模型想了什么」
        # 当成「模型选了什么」。
        if type(ev).__name__ == "ToolCallEvent":
            called.append((getattr(ev, "title", "") or "", getattr(ev, "raw_input", "") or ""))
            return
        if type(ev).__name__ != "TextChunkEvent":
            return
        if getattr(getattr(ev, "kind", None), "name", "") != "MESSAGE":
            return
        chunk = getattr(ev, "text", "")
        if isinstance(chunk, str) and chunk:
            texts.append(chunk)

    adapter._on_event = collect
    await adapter.session_prompt(session_id, text)
    return "".join(texts).strip(), called


async def run(adapter: DshAdapter, cases: list[dict], isolated: bool) -> list[dict]:
    workspace = str(PROJECT_DIR / "deploy" / "dsh")
    session_id = await adapter.session_new(workspace)
    results: list[dict] = []
    for case in cases:
        sid = session_id
        if isolated:
            sid = await adapter.session_new(workspace)
        try:
            raw, called = await _collect(adapter, sid, case["input"])
        except DshError as e:
            results.append({"input": case["input"], "want": normalize(case["expected_tool"]),
                            "got": f"<error: {e}>", "raw": "", "passed": False})
            continue
        finally:
            if isolated:
                await adapter.session_close(sid)
        results.append(_judge(case, raw, called))
    return results


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="意图识别行为级回归（dsh 链路）")
    parser.add_argument("--min-score", type=float, default=0.92)
    parser.add_argument("--case-tags", default=None)
    parser.add_argument("--isolated", action="store_true",
                        help="每条用例一个新会话（查上下文污染用，耗时长）")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not cfg.dsh.resolved_api_key:
        print("缺少网关凭据：config.toml 的 [projects.dsh].api_key 与 AIGW_API_KEY 都为空",
              file=sys.stderr)
        return 2

    cases = load_cases(args.case_tags)
    if not cases:
        print(f"没有匹配的用例（tag={args.case_tags}）", file=sys.stderr)
        return 2

    sup_cfg = SupervisorConfig(
        dsh_home=Path(cfg.dsh.dsh_home) if cfg.dsh.dsh_home else SupervisorConfig().dsh_home,
        patch_path=DEFAULT_PATCH,
        env={"AIGW_API_KEY": cfg.dsh.resolved_api_key},
    )
    adapter = DshAdapter(
        dsh_home=sup_cfg.dsh_home,
        patch_path=str(sup_cfg.patch_path),
        env=dict(sup_cfg.env or {}),
    )
    try:
        await adapter.start()
    except DshError as e:
        print(f"dsh 不可用：{e}", file=sys.stderr)
        return 2

    try:
        results = await run(adapter, cases, args.isolated)
    finally:
        await adapter.shutdown()

    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['input'][:36]:<38} want={r['want']:<24} got={r['got']}")
    passed = sum(1 for r in results if r["passed"])
    score = passed / len(results)
    print(f"\n  总分: {passed}/{len(results)} = {score:.1%}（门槛 {args.min_score:.0%}）")
    return 0 if score >= args.min_score else 1


if __name__ == "__main__":
    # 报告里有中文：Windows 控制台默认 GBK，不改编码会在打印结果时崩，
    # 而不是在判定时崩——看起来像「跑完了没输出」。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
