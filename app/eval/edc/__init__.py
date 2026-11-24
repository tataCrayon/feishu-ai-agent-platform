"""EDC (Evaluation-Driven Collection) — metrics aggregation for EDD tools.

Bridges the Python app runtime with the existing EDD toolchain
(bot/eval/collector.py, bot/loop/engine.py).

Provides:
- EDCMetricsCollector: aggregates Agent-level metrics
- EDC snapshot: written to logs/edc/metrics.json for EDD tools to consume
- Per-agent stats: count, errors, timeouts, avg duration, p50/p95/p99 latency
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("edc")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parents[3]  # app/eval/edc/ -> feishu-agent-platform/
AGENTS_LOG_DIR = PROJECT_DIR / "logs" / "agents"
EDC_LOG_DIR = PROJECT_DIR / "logs" / "edc"
HEALTH_LOG_DIR = PROJECT_DIR / "logs" / "health"


class EDCMetricsCollector:
    """Aggregates agent metrics for EDD tools.

    Reads logs/agents/ and logs/health/ and produces structured metrics
    snapshots that the existing EDD toolchain (bot/eval/collector.py)
    can consume.

    Usage:
        collector = EDCMetricsCollector()
        snapshot = collector.collect(days=7)
        collector.write_snapshot(snapshot)
    """

    def __init__(self):
        EDC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    def collect(self, days: int = 7) -> dict[str, Any]:
        """Collect and aggregate metrics for the last N days.

        Returns:
            Dict with:
            - period: time range
            - agent_stats: per-agent metrics
            - health_stats: per-agent health status
            - summary: totals and rates
            - daily_breakdown: metrics per day
        """
        now = datetime.now(TZ)
        period_start = (now - timedelta(days=days)).isoformat()

        # Collect agent trace stats
        agent_stats = self._collect_agent_stats(days)

        # Collect health stats
        health_stats = self._collect_health_stats(days)

        # Daily breakdown
        daily = self._collect_daily_breakdown(days)

        # Summary
        total_calls = sum(s["count"] for s in agent_stats.values())
        total_errors = sum(s["errors"] for s in agent_stats.values())
        total_timeouts = sum(s["timeouts"] for s in agent_stats.values())
        total_duration = sum(s["total_duration_ms"] for s in agent_stats.values())

        return {
            "timestamp": now.isoformat(),
            "period": {
                "days": days,
                "start": period_start,
                "end": now.isoformat(),
            },
            "summary": {
                "total_calls": total_calls,
                "total_errors": total_errors,
                "total_timeouts": total_timeouts,
                "total_duration_ms": total_duration,
                "error_rate": round(total_errors / total_calls, 3) if total_calls > 0 else 0.0,
                "timeout_rate": round(total_timeouts / total_calls, 3) if total_calls > 0 else 0.0,
                "avg_duration_ms": round(total_duration / total_calls, 1) if total_calls > 0 else 0.0,
            },
            "agent_stats": agent_stats,
            "health_stats": health_stats,
            "daily_breakdown": daily,
        }

    def _collect_agent_stats(self, days: int) -> dict[str, dict[str, Any]]:
        """Aggregate per-agent stats from trace logs."""
        stats: dict[str, dict] = {}

        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            trace_file = AGENTS_LOG_DIR / f"{date}.jsonl"
            chain_file = AGENTS_LOG_DIR / f"{date}.chain.jsonl"

            # Read trace logs
            self._read_trace_file(trace_file, stats)

            # Read chain logs (for chain-level metrics)
            self._read_chain_file(chain_file, stats)

        # Calculate derived metrics
        for _agent, s in stats.items():
            if s["count"] > 0:
                s["avg_duration_ms"] = round(s["total_duration_ms"] / s["count"], 1)
                s["error_rate"] = round((s["errors"] + s["timeouts"]) / s["count"], 3)
                s["avg_cache_hit_rate"] = round(
                    s["total_cache_read"] / max(
                        s["total_cache_read"] + s["total_cache_creation"] + s["total_tokens"], 1
                    ), 4
                )
                s["total_cost"] = round(s["total_cost"], 6)
                s["avg_cost_per_call"] = round(s["total_cost"] / s["count"], 6)
                # Estimate p50/p95 from sorted durations (simplified)
                if s["durations"]:
                    sorted_d = sorted(s["durations"])
                    n = len(sorted_d)
                    s["p50_ms"] = sorted_d[n // 2]
                    s["p95_ms"] = sorted_d[min(int(n * 0.95), n - 1)]
                    s["p99_ms"] = sorted_d[min(int(n * 0.99), n - 1)]
                else:
                    s["p50_ms"] = 0
                    s["p95_ms"] = 0
                    s["p99_ms"] = 0
                del s["durations"]  # Clean up raw data

        return stats

    def _read_trace_file(self, file_path: Path, stats: dict):
        """Read a single trace JSONL file and aggregate into stats.

        扩展字段（对齐迁移包能力）：
        - cache_hit_rate: 按 agent 聚合的缓存命中率
        - total_cost: 估算总成本（USD）
        """
        if not file_path.exists():
            return

        try:
            for line in file_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                agent = entry.get("agent", "unknown")

                if agent not in stats:
                    stats[agent] = {
                        "count": 0,
                        "errors": 0,
                        "timeouts": 0,
                        "total_duration_ms": 0,
                        "total_tokens": 0,
                        "total_cache_read": 0,
                        "total_cache_creation": 0,
                        "total_cost": 0.0,
                        "durations": [],
                        "chains": 0,
                    }

                s = stats[agent]
                s["count"] += 1
                status = entry.get("status", "")
                if status == "error":
                    s["errors"] += 1
                elif status == "timeout":
                    s["timeouts"] += 1
                s["total_duration_ms"] += entry.get("duration_ms", 0)
                s["total_tokens"] += entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
                s["total_cache_read"] += entry.get("cache_read_tokens", 0) or 0
                s["total_cache_creation"] += entry.get("cache_creation_tokens", 0) or 0
                s["total_cost"] += entry.get("cost_usd", 0.0) or 0.0
                s["durations"].append(entry.get("duration_ms", 0))
        except Exception as e:
            logger.error(f"Failed to read trace file {file_path}: {e}")

    def _read_chain_file(self, file_path: Path, stats: dict):
        """Read chain trace files for chain-level metrics."""
        if not file_path.exists():
            return

        try:
            for line in file_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line:
                    continue
                entry = json.loads(line)
                # Count chains per agent
                for span in entry.get("spans", []):
                    agent = span.get("agent", "unknown")
                    if agent in stats:
                        stats[agent]["chains"] = stats[agent].get("chains", 0) + 1
        except Exception as e:
            logger.error(f"Failed to read chain file {file_path}: {e}")

    def _collect_health_stats(self, days: int) -> dict[str, Any]:
        """Collect health check statistics."""
        health_file = HEALTH_LOG_DIR / "current.json"
        if not health_file.exists():
            return {"status": "no_data", "agents": {}}

        try:
            data = json.loads(health_file.read_text(encoding="utf-8"))
            return {
                "status": "available",
                "total": data.get("total", 0),
                "healthy": data.get("healthy", 0),
                "critical": data.get("critical", 0),
                "agents": data.get("agents", {}),
            }
        except Exception as e:
            logger.error(f"Failed to read health stats: {e}")
            return {"status": "error", "detail": str(e)}

    def _collect_daily_breakdown(self, days: int) -> list[dict[str, Any]]:
        """Collect daily breakdown of metrics."""
        breakdown = []

        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            trace_file = AGENTS_LOG_DIR / f"{date}.jsonl"

            day_stats: dict[str, dict] = {}
            self._read_trace_file(trace_file, day_stats)

            total = sum(s["count"] for s in day_stats.values())
            errors = sum(s["errors"] for s in day_stats.values())
            duration = sum(s["total_duration_ms"] for s in day_stats.values())

            if total > 0:
                breakdown.append({
                    "date": date,
                    "calls": total,
                    "errors": errors,
                    "total_duration_ms": duration,
                    "error_rate": round(errors / total, 3),
                    "agents": {
                        name: {"count": s["count"], "errors": s["errors"]}
                        for name, s in day_stats.items()
                    },
                })

        return breakdown

    CACHE_HIT_RATE_WARN: float = 0.5  # 命中率 < 50% 触发告警

    def write_snapshot(self, snapshot: dict[str, Any]):
        """Write metrics snapshot to logs/edc/metrics.json.

        This is the file that bot/eval/collector.py can read.
        Also checks cache hit rate and writes health alerts for low-hit agents.
        """
        try:
            metrics_file = EDC_LOG_DIR / "metrics.json"
            with open(metrics_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            logger.info(f"EDC snapshot written: {metrics_file} ({snapshot['summary']['total_calls']} calls)")
        except Exception as e:
            logger.error(f"Failed to write EDC snapshot: {e}")

        # ── 缓存命中率告警 ──
        self._check_cache_hit_alerts(snapshot)

    def _check_cache_hit_alerts(self, snapshot: dict[str, Any]):
        """检查缓存命中率，低于阈值时写 health 告警日志。"""
        agent_stats = snapshot.get("agent_stats", {})
        if not agent_stats:
            return

        alerts = []
        for agent_name, s in agent_stats.items():
            hit_rate = s.get("avg_cache_hit_rate", 0.0)
            if s["count"] > 0 and hit_rate < self.CACHE_HIT_RATE_WARN:
                alerts.append({
                    "agent": agent_name,
                    "avg_cache_hit_rate": hit_rate,
                    "total_calls": s["count"],
                    "total_cache_read": s.get("total_cache_read", 0),
                    "total_cache_creation": s.get("total_cache_creation", 0),
                    "severity": "warning",
                })

        if not alerts:
            return

        # 写 health 告警日志
        health_file = HEALTH_LOG_DIR / "cache_alerts.jsonl"
        HEALTH_LOG_DIR.mkdir(parents=True, exist_ok=True)

        for alert in alerts:
            entry = {
                "timestamp": datetime.now(TZ).isoformat(),
                "type": "cache_hit_alert",
                "agent": alert["agent"],
                "avg_cache_hit_rate": alert["avg_cache_hit_rate"],
                "total_calls": alert["total_calls"],
                "total_cache_read": alert["total_cache_read"],
                "total_cache_creation": alert["total_cache_creation"],
                "severity": alert["severity"],
                "message": (
                    f"缓存命中率 {alert['avg_cache_hit_rate']:.1%} < 50% "
                    f"（{alert['total_calls']} 次调用，"
                    f"cache_read={alert['total_cache_read']}, "
                    f"cache_creation={alert['total_cache_creation']}）"
                ),
                "suggestion": "审查 Prompt 结构：动态内容是否落在静态前缀中？System Prompt 是否稳定？",
            }
            try:
                with open(health_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                logger.warning(
                    "Cache hit alert: %s hit_rate=%.1f%% (%d calls)",
                    alert["agent"], alert["avg_cache_hit_rate"] * 100, alert["total_calls"],
                )
            except Exception as e:
                logger.error(f"Failed to write cache alert: {e}")


# ── Convenience ─────────────────────────────────────────────────


def collect_and_write(days: int = 7) -> dict[str, Any]:
    """One-shot: collect metrics and write snapshot."""
    collector = EDCMetricsCollector()
    snapshot = collector.collect(days=days)
    collector.write_snapshot(snapshot)
    return snapshot