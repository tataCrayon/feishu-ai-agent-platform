#!/usr/bin/env python3
"""
审计日志工具
=============
平台能力调用审计，独立于 token 监控。

用法：
  python app/guardrails/audit_logger.py --read-today
  python app/guardrails/audit_logger.py --stats 7
  python app/guardrails/audit_logger.py --export --days 30
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
AUDIT_DIR = PROJECT_DIR / "logs" / "audit"
TZ = timezone(timedelta(hours=8))


def write(entry: dict) -> str:
    """写入一条审计日志，返回日志行"""
    date = datetime.now(TZ).strftime("%Y-%m-%d")
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = AUDIT_DIR / f"{date}.jsonl"

    entry["ts"] = datetime.now(TZ).isoformat()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)
    return line.strip()


def read(date_str: str | None = None) -> list[dict]:
    """读取指定日期的审计日志"""
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    log_file = AUDIT_DIR / f"{date_str}.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def stats(days: int = 7) -> dict:
    """统计审计日志"""
    now = datetime.now(TZ)
    by_user: dict[str, int] = defaultdict(int)
    by_command: dict[str, int] = defaultdict(int)
    by_hour: dict[int, int] = defaultdict(int)
    total = 0
    errors = 0

    for i in range(days):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        for entry in read(date):
            total += 1
            user = entry.get("user_name", entry.get("user", "?"))
            by_user[user] += 1
            cmd = entry.get("command", "?")
            by_command[cmd] += 1
            try:
                ts = datetime.fromisoformat(entry.get("ts", ""))
                by_hour[ts.hour] += 1
            except (ValueError, TypeError):
                pass
            if entry.get("result") != "ok":
                errors += 1

    return {
        "days": days,
        "total": total,
        "errors": errors,
        "error_rate": f"{errors/total:.1%}" if total > 0 else "0%",
        "avg_per_day": round(total / max(days, 1), 1),
        "top_users": sorted(by_user.items(), key=lambda x: -x[1])[:10],
        "top_commands": sorted(by_command.items(), key=lambda x: -x[1])[:10],
        "peak_hours": sorted(by_hour.items(), key=lambda x: -x[1])[:5],
    }


def export(days: int = 30) -> list[dict]:
    """导出所有审计日志"""
    now = datetime.now(TZ)
    all_entries = []
    for i in range(days):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        all_entries.extend(read(date))
    return all_entries


def main():
    parser = argparse.ArgumentParser(description="审计日志工具")
    parser.add_argument("--read-today", action="store_true", help="读取今日日志")
    parser.add_argument("--stats", action="store_true", help="统计最近 N 天")
    parser.add_argument("--days", type=int, default=7, help="统计天数，默认 7")
    parser.add_argument("--export", action="store_true", help="导出日志")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.export:
        entries = export(args.days)
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            for e in entries:
                print(json.dumps(e, ensure_ascii=False))

    elif args.stats:
        s = stats(args.days)
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print(f"📊 审计统计 ({s['days']}天)")
            print("=" * 40)
            print(f"总调用: {s['total']},  异常: {s['errors']} ({s['error_rate']}),  日均: {s['avg_per_day']}")
            print("\n👤 TOP 用户:")
            for name, count in s["top_users"]:
                print(f"  {name}: {count}")
            print("\n🔧 TOP 命令:")
            for cmd, count in s["top_commands"]:
                print(f"  {cmd}: {count}")
            if s["peak_hours"]:
                print("\n⏰ 高峰时段:")
                for hour, count in s["peak_hours"]:
                    print(f"  {hour}:00 — {count} 次")

    elif args.read_today:
        entries = read()
        if args.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            print(f"今日审计日志 ({len(entries)} 条):")
            for e in entries:
                ts = e.get("ts", "?")[:19]
                user = e.get("user_name", e.get("user", "?"))
                cmd = e.get("command", "?")
                result = e.get("result", "?")
                print(f"  {ts} | {user} | {cmd} | {result}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
