#!/usr/bin/env python3
"""
缓存命中率 & 成本报告工具
==========================
从 trace 日志中聚合缓存命中率和成本数据，输出报告。

用法：
  python app/observability/cache_monitor.py report              # 今日报告
  python app/observability/cache_monitor.py report --week       # 本周
  python app/observability/cache_monitor.py report --month      # 本月
  python app/observability/cache_monitor.py report --json       # JSON 输出
  python app/observability/cache_monitor.py alerts              # 查看缓存告警
  python app/observability/cache_monitor.py watch               # 持续监控（每 10 分钟）
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
AGENTS_LOG_DIR = PROJECT_DIR / "logs" / "agents"
HEALTH_LOG_DIR = PROJECT_DIR / "logs" / "health"
TZ = timezone(timedelta(hours=8))

CACHE_HIT_RATE_WARN = 0.5  # 告警阈值

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("cache_monitor")


# ── 数据读取 ──────────────────────────────────────────────────


def _read_trace_logs(days: int) -> list[dict[str, Any]]:
    """读取最近 N 天的 trace 日志。"""
    entries = []
    now = datetime.now(TZ)
    for d in range(days):
        date = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        fp = AGENTS_LOG_DIR / f"{date}.jsonl"
        if not fp.exists():
            continue
        try:
            for line in fp.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    entries.append(json.loads(line))
        except Exception as e:
            logger.warning(f"Failed to read {fp}: {e}")
    return entries


def _read_cache_alerts(days: int = 7) -> list[dict[str, Any]]:
    """读取缓存告警日志。"""
    fp = HEALTH_LOG_DIR / "cache_alerts.jsonl"
    if not fp.exists():
        return []
    entries = []
    now = datetime.now(TZ)
    try:
        for line in fp.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            entry = json.loads(line)
            ts = entry.get("timestamp", "")
            try:
                entry_dt = datetime.fromisoformat(ts)
                if (now - entry_dt).days <= days:
                    entries.append(entry)
            except (ValueError, TypeError):
                entries.append(entry)
    except Exception as e:
        logger.warning(f"Failed to read cache alerts: {e}")
    return entries


# ── 聚合逻辑 ──────────────────────────────────────────────────


def _aggregate(days: int) -> dict[str, dict[str, Any]]:
    """按 agent 聚合缓存指标。"""
    entries = _read_trace_logs(days)
    stats: dict[str, dict] = {}

    for entry in entries:
        agent = entry.get("agent", "unknown")
        if agent not in stats:
            stats[agent] = {
                "count": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "total_cost": 0.0,
                "total_duration_ms": 0.0,
            }
        s = stats[agent]
        s["count"] += 1
        s["tokens_in"] += entry.get("tokens_in", 0) or 0
        s["tokens_out"] += entry.get("tokens_out", 0) or 0
        s["cache_read"] += entry.get("cache_read_tokens", 0) or 0
        s["cache_creation"] += entry.get("cache_creation_tokens", 0) or 0
        s["total_cost"] += entry.get("cost_usd", 0.0) or 0.0
        s["total_duration_ms"] += entry.get("duration_ms", 0) or 0

    # 计算派生指标
    for _agent, s in stats.items():
        total_input = s["cache_read"] + s["cache_creation"] + s["tokens_in"]
        s["cache_hit_rate"] = round(s["cache_read"] / total_input, 4) if total_input > 0 else 0.0
        s["avg_duration_ms"] = round(s["total_duration_ms"] / s["count"], 1) if s["count"] > 0 else 0.0
        s["scaled_cost"] = round(s["total_cost"], 6)

    return stats


# ── 报告输出 ──────────────────────────────────────────────────


def _fmt_pct(value: float) -> str:
    """格式化百分比，低命中率加颜色标记。"""
    pct = f"{value * 100:.1f}%"
    if value < CACHE_HIT_RATE_WARN:
        return f"{pct} ⚠️"
    if value >= 0.8:
        return f"{pct} ✅"
    return pct


def _fmt_cost(value: float) -> str:
    """格式化成本（USD）。"""
    if value < 0.01:
        return f"${value:.6f}"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _build_report_dict(stats: dict, days: int) -> dict:
    """构建结构化报告字典。"""
    total_calls = sum(s["count"] for s in stats.values())
    total_cost = sum(s["total_cost"] for s in stats.values())
    total_cache_read = sum(s["cache_read"] for s in stats.values())
    total_cache_creation = sum(s["cache_creation"] for s in stats.values())
    total_tokens = sum(s["tokens_in"] for s in stats.values()) + sum(s["tokens_out"] for s in stats.values())

    total_input = total_cache_read + total_cache_creation + sum(s["tokens_in"] for s in stats.values())
    overall_hit_rate = round(total_cache_read / total_input, 4) if total_input > 0 else 0.0

    # 按 call 数排序
    sorted_agents = sorted(stats.items(), key=lambda x: -x[1]["count"])

    return {
        "period_days": days,
        "period_label": _period_label(days),
        "timestamp": datetime.now(TZ).isoformat(),
        "summary": {
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "total_cache_read": total_cache_read,
            "total_cache_creation": total_cache_creation,
            "overall_cache_hit_rate": overall_hit_rate,
            "total_cost": round(total_cost, 6),
            "avg_cost_per_call": round(total_cost / total_calls, 6) if total_calls > 0 else 0.0,
        },
        "agents": [
            {
                "name": name,
                "calls": s["count"],
                "cache_hit_rate": s["cache_hit_rate"],
                "cache_read": s["cache_read"],
                "cache_creation": s["cache_creation"],
                "tokens_in": s["tokens_in"],
                "tokens_out": s["tokens_out"],
                "total_cost": round(s["total_cost"], 6),
                "avg_duration_ms": s["avg_duration_ms"],
            }
            for name, s in sorted_agents
        ],
    }


def _period_label(days: int) -> str:
    now = datetime.now(TZ)
    if days == 1:
        return now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    return f"{start} ~ {end}"


def _report_text(stats: dict, days: int):
    """文本格式报告。"""
    total_calls = sum(s["count"] for s in stats.values())
    total_cost = sum(s["total_cost"] for s in stats.values())
    total_cache_read = sum(s["cache_read"] for s in stats.values())
    total_cache_creation = sum(s["cache_creation"] for s in stats.values())
    total_tokens_in = sum(s["tokens_in"] for s in stats.values())
    total_tokens_out = sum(s["tokens_out"] for s in stats.values())

    total_input = total_cache_read + total_cache_creation + total_tokens_in
    overall_hit_rate = round(total_cache_read / total_input, 4) if total_input > 0 else 0.0

    sorted_agents = sorted(stats.items(), key=lambda x: -x[1]["count"])

    print(f"📊 缓存命中率 & 成本报告 — {_period_label(days)}")
    print("=" * 60)

    print("\n📈 总览")
    print(f"  Agent 调用:     {total_calls}")
    print(f"  总输入 Token:   {total_tokens_in:,}")
    print(f"  总输出 Token:   {total_tokens_out:,}")
    print(f"  缓存命中:       {total_cache_read:,}")
    print(f"  缓存创建:       {total_cache_creation:,}")
    print(f"  整体缓存命中率: {_fmt_pct(overall_hit_rate)}")
    print(f"  总成本:         {_fmt_cost(total_cost)}")
    if total_calls > 0:
        print(f"  平均成本/调用:  {_fmt_cost(total_cost / total_calls)}")

    print("\n🤖 Agent 明细")
    print(f"  {'Agent':<18} {'调用':>6} {'命中率':>10} {'缓存读':>10} {'缓存写':>10} {'成本':>12} {'平均耗时':>10}")
    print(f"  {'-'*18} {'-'*6} {'-'*10} {'-'*10} {'-'*10} {'-'*12} {'-'*10}")

    hit_agents = [n for n, s in sorted_agents if s["cache_hit_rate"] < CACHE_HIT_RATE_WARN]
    for name, s in sorted_agents:
        print(f"  {name:<18} {s['count']:>6} {_fmt_pct(s['cache_hit_rate']):>10} "
              f"{s['cache_read']:>10,} {s['cache_creation']:>10,} "
              f"{_fmt_cost(s['total_cost']):>12} {s['avg_duration_ms']:>8}ms")

    if hit_agents:
        print("\n⚠️  缓存命中率 < 50% 的 Agent（建议审查 Prompt 结构）：")
        for name in hit_agents:
            s = stats[name]
            print(f"  • {name} — {s['cache_hit_rate'] * 100:.1f}% ({s['count']} 次调用)")
            print("    建议：检查 System Prompt 中动态内容是否落在静态前缀位置")

    # 成本节省估算
    if total_cache_read > 0 and total_input > 0:
        # 假设缓存命中节省了 cache_read 数量的 input 成本
        # 按平均 input 单价估算
        deepseek_input_price = 0.00014  # USD/1K tokens
        saved = total_cache_read / 1000 * deepseek_input_price
        if saved > 0.001:
            print("\n💵 成本节省估算（缓存命中）")
            print(f"  缓存命中节省 input 成本约 {_fmt_cost(saved)}（基于 deepseek-v4-flash 定价）")


# ── CLI 命令 ──────────────────────────────────────────────────


def cmd_report(days: int = 1, json_fmt: bool = False):
    """生成缓存命中率 & 成本报告。"""
    stats = _aggregate(days)
    if not stats:
        print("📭 无 trace 数据")
        return

    if json_fmt:
        report = _build_report_dict(stats, days)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _report_text(stats, days)


def cmd_alerts(days: int = 7, json_fmt: bool = False):
    """查看缓存告警。"""
    alerts = _read_cache_alerts(days)
    if not alerts:
        print("✅ 无缓存告警")
        return

    if json_fmt:
        print(json.dumps(alerts, ensure_ascii=False, indent=2))
        return

    print(f"⚠️  缓存命中率告警（最近 {days} 天）")
    print("=" * 60)
    for alert in alerts:
        ts = alert.get("timestamp", "?")[:19]
        agent = alert.get("agent", "?")
        rate = alert.get("avg_cache_hit_rate", 0) * 100
        calls = alert.get("total_calls", 0)
        msg = alert.get("message", "")
        print(f"  [{ts}] {agent}: {rate:.1f}% ({calls} 次)")
        print(f"     {msg}")


def cmd_watch(interval: int = 10):
    """持续监控模式。"""
    print(f"🔍 开始缓存监控（每 {interval} 分钟检查一次）")
    print(f"  告警阈值：命中率 < {CACHE_HIT_RATE_WARN * 100}%")
    print("  按 Ctrl+C 退出")
    try:
        while True:
            stats = _aggregate(days=1)
            if stats:
                low_hit = [n for n, s in stats.items() if s["cache_hit_rate"] < CACHE_HIT_RATE_WARN]
                if low_hit:
                    print(f"[{datetime.now(TZ).strftime('%H:%M')}] ⚠️  低命中率: {', '.join(low_hit)}")
                else:
                    print(f"[{datetime.now(TZ).strftime('%H:%M')}] ✅ 缓存正常")
            else:
                print(f"[{datetime.now(TZ).strftime('%H:%M')}] 📭 无数据")
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        print("\n👋 监控已停止")


# ── 入口 ─────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="缓存命中率 & 成本报告工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python app/observability/cache_monitor.py report              # 今日报告
  python app/observability/cache_monitor.py report --week       # 本周
  python app/observability/cache_monitor.py report --json       # JSON 输出
  python app/observability/cache_monitor.py alerts              # 查看缓存告警
  python app/observability/cache_monitor.py watch               # 持续监控
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_report = sub.add_parser("report", help="生成缓存命中率 & 成本报告")
    p_report.add_argument("--week", action="store_true", help="本周")
    p_report.add_argument("--month", action="store_true", help="本月")
    p_report.add_argument("--json", action="store_true", help="JSON 输出")

    p_alerts = sub.add_parser("alerts", help="查看缓存告警")
    p_alerts.add_argument("--days", type=int, default=7, help="回溯天数")
    p_alerts.add_argument("--json", action="store_true", help="JSON 输出")

    p_watch = sub.add_parser("watch", help="持续监控")
    p_watch.add_argument("--interval", type=int, default=10, help="扫描间隔（分钟），默认 10")

    args = parser.parse_args()

    if args.command == "report":
        days = 1
        if args.week:
            days = 7
        elif args.month:
            days = 30
        cmd_report(days=days, json_fmt=args.json)
    elif args.command == "alerts":
        cmd_alerts(days=args.days, json_fmt=args.json)
    elif args.command == "watch":
        cmd_watch(interval=args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()