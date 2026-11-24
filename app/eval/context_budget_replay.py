"""上下文预算回放实验（idea-vetting 验证步骤，一次性脚本）。

数据源（零 DB，全部读落盘日志）：
- logs/agents/{date}.jsonl   AgentTrace 每次 LLM 调用的 tokens_in（prompt_tokens 口径）
- logs/tasks/{date}.jsonl    TaskRecord 档案（query 长度、merged_count）
- logs/decisions/{date}.jsonl DecisionRecorder（is_fallback / demoted / error 线索）

回答两个问题：
1. 真实流量每次 LLM 调用的输入 token 分布（p50/p90/p95/max），距窗口上限多远？
2. is_fallback 到底发生过几次、原因是什么（超限？网络？别的？）

用法：python app/eval/context_budget_replay.py [--days 14] [--window 128000]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
AGENTS_LOG_DIR = PROJECT_DIR / "logs" / "agents"
TASKS_LOG_DIR = PROJECT_DIR / "logs" / "tasks"
DECISIONS_LOG_DIR = PROJECT_DIR / "logs" / "decisions"

# 疑似上下文超限的错误特征（llm 报错文案常见关键词）
OVERLONG_PAT = re.compile(
    r"context.{0,30}(length|window)|too (many|long).{0,20}token|input.{0,20}(length|too long)"
    r"|maximum context|exceeds?.{0,20}(token|length)|range of input",
    re.I,
)


def _read_jsonl(fp: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not fp.exists():
        return out
    for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue  # 半行写入损坏容忍
    return out


def _pct(values: list[int | float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return float(s[k])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14, help="回放窗口（天），默认 14")
    ap.add_argument("--window", type=int, default=128000, help="模型输入窗口上限（token）近似值")
    args = ap.parse_args()

    dates = [(date.today() - timedelta(days=i)).isoformat() for i in range(args.days)]

    # ── 1) tokens_in 分布（按 agent 分桶 + 全局） ──────────────────────────
    by_agent: dict[str, list[int]] = {}
    all_in: list[int] = []
    over_traces: list[dict[str, Any]] = []
    for d in dates:
        for r in _read_jsonl(AGENTS_LOG_DIR / f"{d}.jsonl"):
            t = r.get("tokens_in")
            if not isinstance(t, (int, float)) or t <= 0:
                continue
            agent = str(r.get("agent") or "?")
            by_agent.setdefault(agent, []).append(int(t))
            all_in.append(int(t))
            if OVERLONG_PAT.search(str(r.get("error") or "")):
                over_traces.append(r)

    print(f"== LLM 调用 tokens_in 分布（近 {args.days} 天，共 {len(all_in)} 次调用）==")
    print(f"{'agent':<22}{'calls':>7}{'p50':>8}{'p90':>8}{'p95':>8}{'max':>8}{'max/窗口':>10}")
    for agent in sorted(by_agent, key=lambda a: -max(by_agent[a])):
        v = by_agent[agent]
        mx = max(v)
        print(
            f"{agent:<22}{len(v):>7}{_pct(v,50):>8.0f}{_pct(v,90):>8.0f}"
            f"{_pct(v,95):>8.0f}{mx:>8}{mx/args.window:>9.1%}"
        )
    if all_in:
        p99 = _pct(all_in, 99)
        print(f"{'GLOBAL':<22}{len(all_in):>7}{_pct(all_in,50):>8.0f}{_pct(all_in,90):>8.0f}"
              f"{_pct(all_in,95):>8.0f}{max(all_in):>8}{max(all_in)/args.window:>9.1%}")
        print(f"\np99 = {p99:.0f} tokens；放弃信号线（60% 窗口）= {args.window*0.6:.0f} → "
              f"{'p99 远低于线，触发放弃信号' if p99 < args.window*0.6 else 'p99 已逼近窗口，压缩机制有必要'}")

    # ── 2) is_fallback 归因 ───────────────────────────────────────────────
    print(f"\n== is_fallback 归因（近 {args.days} 天 decisions 日志）==")
    fb = 0
    reasons: Counter[str] = Counter()
    for d in dates:
        for r in _read_jsonl(DECISIONS_LOG_DIR / f"{d}.jsonl"):
            if not r.get("is_fallback"):
                continue
            fb += 1
            q = str(r.get("query") or "")
            reasons[f"tool={r.get('tool','?')} demoted={r.get('demoted',False)}"] += 1
            print(f"  [{r.get('ts','?')[:16]}] q={q[:40]!r} tool={r.get('tool')} "
                  f"conf={r.get('args',{}).get('confidence')}")
    print(f"共 {fb} 次 fallback；分布：{dict(reasons)}")
    for r in over_traces:
        print(f"  ⚠️ 疑似超限 trace: {r.get('timestamp')} agent={r.get('agent')} error={str(r.get('error'))[:120]}")
    if not over_traces:
        print("未发现任何疑似上下文超限错误特征（error 字段无 overlength 关键词）。")

    # ── 3) 输入侧构成风险：单条 query/回复的最大体量 ─────────────────────
    print(f"\n== 输入体量（task 档案，近 {args.days} 天）==")
    qlen: list[int] = []
    rlen: list[int] = []
    for d in dates:
        for r in _read_jsonl(TASKS_LOG_DIR / f"{d}.jsonl"):
            qlen.append(len(str(r.get("query") or "")))
            rlen.append(len(str(r.get("response_preview") or "")))
    if qlen:
        print(f"query 字符数: p95={_pct(qlen,95):.0f} max={max(qlen)}")
    if rlen:
        print(f"response_preview 字符数: p95={_pct(rlen,95):.0f} max={max(rlen)}（注：preview 有截断，是下界）")


if __name__ == "__main__":
    main()
