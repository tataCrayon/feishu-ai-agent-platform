#!/usr/bin/env python3
"""任务表现报表 — 聚合 logs/tasks/ 任务档案。

用法：
    python app/observability/task_report.py               # 最近 7 天
    python app/observability/task_report.py --days 30     # 指定天数
    python app/observability/task_report.py --negative    # 只看负反馈明细
    python app/observability/task_report.py --json        # 机器可读输出

产出（对齐 EDD 闭环）：
- 按 agent 的调用量 / 错误率 / 门禁拦截率 / 平均耗时 / 负反馈率
- 整体门禁拦截率（执行失败信号）
- 最近负反馈案例明细（优化线索）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.observability.task_record import task_recorder


def main() -> int:
    parser = argparse.ArgumentParser(description="任务表现报表")
    parser.add_argument("--days", type=int, default=7, help="统计回溯天数")
    parser.add_argument("--negative", action="store_true", help="只看负反馈明细")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    summary = task_recorder.summarize(days=args.days)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.negative:
        cases = summary.get("recent_negative_cases", [])
        if not cases:
            print("✅ 统计期内无负反馈记录")
            return 0
        print(f"## 负反馈明细（最近 {len(cases)} 条）\n")
        for c in cases:
            print(f"- [{c['ts'][:16]}] {c['agent']}")
            print(f"  用户问: {c['query']}")
            print(f"  回复:   {c['response']}")
            print(f"  反馈:   ❌ {c['feedback_text']}\n")
        return 0

    print(f"═══ 任务表现报表（最近 {summary['period_days']} 天）═══\n")
    print(f"总任务数: {summary['total_tasks']}   "
          f"整体门禁拦截率: {summary['gate_blocked_rate']:.1%}   "
          f"负反馈数: {summary['negative_count']}")

    agents = summary.get("agents", {})
    if not agents:
        print("\n（暂无任务档案数据）")
        return 0

    print(f"\n{'Agent':<22}{'调用量':>6}{'错误率':>8}{'门禁拦截':>9}{'负反率':>8}{'均耗时(ms)':>11}")
    print("─" * 64)
    for name, s in sorted(agents.items(), key=lambda kv: -kv[1]["count"]):
        print(f"{name:<22}{s['count']:>6}"
              f"{s['error_rate']:>8.1%}"
              f"{s['gate_blocked_rate']:>9.1%}"
              f"{s['negative_rate']:>8.1%}"
              f"{s['avg_duration_ms']:>11.0f}")

    if summary.get("recent_negative_cases"):
        print("\n💡 运行 `python app/observability/task_report.py --negative` 查看负反馈明细")
    return 0


if __name__ == "__main__":
    sys.exit(main())
