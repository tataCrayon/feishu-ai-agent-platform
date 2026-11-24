"""会话轨迹检索 — 「哪些轮次用了某工具 / 哪轮慢 / 哪轮没答上」。

用法（项目根）：
    python scripts/turn_review.py --days 7                     # 最近 7 天全部
    python scripts/turn_review.py --tool webportal             # 碰过工单能力的轮次
    python scripts/turn_review.py --status no_response         # 没答上来的
    python scripts/turn_review.py --task demo-001              # 打印某一轮全文
    python scripts/turn_review.py --dir temp/turns-demo        # 指定轨迹目录（演练用）

数据源：`logs/turns/{date}.index.jsonl`（机器筛）+ `logs/turns/{date}/{task_id}.md`（人读）。
两处 task_id 同值：先用索引缩小范围，再用 `--task` 打开某一轮的完整过程。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.observability.turn_trace import TurnTraceRecorder  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="会话轨迹检索")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tool", default="", help="工具名子串（如 webportal / skill）")
    ap.add_argument("--status", default="", help="success / error / no_response")
    ap.add_argument("--chat", default="", help="只看某个会话")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--task", default="", help="打印某一轮的完整轨迹")
    ap.add_argument("--dir", default="", help="轨迹目录（默认 logs/turns）")
    args = ap.parse_args(argv)

    rec = TurnTraceRecorder(Path(args.dir) if args.dir else None)
    if args.task:
        text = rec.read(args.task)
        print(text or f"找不到 task_id={args.task} 的轨迹", end="")
        return 0 if text else 1

    rows = rec.search(days=args.days, tool=args.tool, status=args.status,
                      chat_id=args.chat, limit=args.limit)
    if not rows:
        print("没有匹配的轮次")
        return 0
    print(f"{'时间':19} {'状态':12} {'耗时':>8}  {'工具链':36} 问题")
    for r in rows:
        # 只显示末段（`mcp__capabilities__watch_cmdb_order` → `watch_cmdb_order`），
        # 前缀都一样，占满宽度反而看不出调了什么
        tools = ",".join(t.split("__")[-1] for t in (r.get("tools") or []))[:36] or "-"
        print(f"{r.get('ts', '')[:19]} {r.get('status', ''):12} "
              f"{r.get('duration_ms', 0):>7.0f}ms  {tools:36} {(r.get('query') or '')[:40]}")
    print(f"\n共 {len(rows)} 轮；打开某一轮：python scripts/turn_review.py --task <task_id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
