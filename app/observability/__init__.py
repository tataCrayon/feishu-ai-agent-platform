"""Agent-level observability — call traces, token usage, cache metrics, cost tracking.

Logs each agent invocation to logs/agents/ for traceability and cost analysis.

Features:
- TraceContext: per-request trace_id + span_id chain for multi-hop tracking
- AgentTrace: single agent invocation record (with cache_metrics + cost)
- ChainTrace: multi-agent chain invocation record
- log_trace_event: structured JSON trace log (aligned with LLM Agent 迁移包 trace.py)
- AgentTracer: writes trace + audit + stats + EDC snapshot
- EDCMetrics: aggregated metrics for EDD tools (including cache hit rate)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.llm import (
    CacheMetrics,
    estimate_cost,
)

logger = logging.getLogger("observability")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
AGENTS_LOG_DIR = PROJECT_DIR / "logs" / "agents"
AUDIT_LOG_DIR = PROJECT_DIR / "logs" / "audit"
HEALTH_LOG_DIR = PROJECT_DIR / "logs" / "health"
EDC_LOG_DIR = PROJECT_DIR / "logs" / "edc"


# ── Trace Context ───────────────────────────────────────────────

@dataclass
class TraceContext:
    """Per-request trace context for multi-agent chain tracking.

    Each incoming user message gets one trace_id.
    Each agent invocation within that request gets a span_id.
    parent_span_id allows building a tree of agent calls.
    """

    trace_id: str
    span_id: str
    parent_span_id: str = ""
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""

    @classmethod
    def new(cls, chat_id: str = "", sender_id: str = "", sender_name: str = "") -> TraceContext:
        """Create a new trace context (root span)."""
        trace_id = uuid.uuid4().hex[:16]
        span_id = uuid.uuid4().hex[:12]
        return cls(
            trace_id=trace_id,
            span_id=span_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
        )

    def child(self) -> TraceContext:
        """Create a child span (for multi-hop agent chains)."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:12],
            parent_span_id=self.span_id,
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            sender_name=self.sender_name,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
        }


# ── Agent Trace ─────────────────────────────────────────────────


@dataclass
class AgentTrace:
    """A single agent invocation trace with chain context.

    扩展字段（对齐迁移包能力）：
        tokens_in/tokens_out: 输入/输出 token。
        cache_read_tokens: 缓存命中的输入 token。
        cache_creation_tokens: 用于创建缓存的输入 token。
        cache_hit_rate: 缓存命中率。
        cost_usd: 估算成本（按手势模型定价）。
        prompt_version: 所用提示词版本号（评估可回溯）。
    """

    agent: str
    intent: str
    chat_id: str
    sender_id: str
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    sender_name: str = ""
    input_text: str = ""
    output_text: str = ""
    status: str = "success"  # success / error / timeout
    duration_ms: float = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_hit_rate: float = 0.0
    cost_usd: float = 0.0
    model: str = ""
    prompt_version: str = ""  # 所用提示词版本号
    timestamp: str = field(default_factory=lambda: datetime.now(TZ).isoformat())
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_usage(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_metrics: CacheMetrics | None = None,
        model: str = "",
    ) -> AgentTrace:
        """带 token/cache 用量创建新 trace。

        Args:
            tokens_in: 输入 token（prompt_tokens）。
            tokens_out: 输出 token（completion_tokens）。
            cache_metrics: Prompt Caching 指标（可为 None）。
            model: 模型名（用于成本估算）。

        Returns:
            更新后的 AgentTrace。
        """
        self.tokens_in = tokens_in or self.tokens_in
        self.tokens_out = tokens_out or self.tokens_out
        if cache_metrics:
            self.cache_read_tokens = cache_metrics.cache_read_input_tokens
            self.cache_creation_tokens = cache_metrics.cache_creation_input_tokens
            self.cache_hit_rate = cache_metrics.hit_rate
        self.model = model or self.model
        self.cost_usd = estimate_cost(self.model, {
            "prompt_tokens": self.tokens_in,
            "completion_tokens": self.tokens_out,
            "cache_read_input_tokens": self.cache_read_tokens,
        })
        return self


@dataclass
class ChainTrace:
    """Multi-agent chain invocation trace.

    Records the full chain of agent calls for a single user request.
    """

    trace_id: str
    chat_id: str
    sender_id: str
    sender_name: str = ""
    spans: list[dict[str, Any]] = field(default_factory=list)
    total_duration_ms: float = 0
    status: str = "success"
    timestamp: str = field(default_factory=lambda: datetime.now(TZ).isoformat())

    def add_span(self, span: dict[str, Any]):
        self.spans.append(span)


# ── Agent Tracer ────────────────────────────────────────────────


class AgentTracer:
    """Tracer for agent invocations with chain support."""

    def __init__(self):
        AGENTS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        EDC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ── Trace ──

    def trace(self, trace: AgentTrace):
        """Record an agent invocation trace.

        写入两种格式：
        1. logs/agents/{date}.jsonl — 完整 AgentTrace（含 cache_metrics + cost）
        2. logs/agents/trace.jsonl — 结构化 JSON trace 日志
        """
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        daily_file = AGENTS_LOG_DIR / f"{today}.jsonl"
        trace_file = AGENTS_LOG_DIR / "trace.jsonl"

        entry = asdict(trace)
        try:
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trace: {e}")

        # 同时写结构化 trace 日志（供日志聚合器消费）
        trace_event = {
            "timestamp": trace.timestamp,
            "type": "agent_step",
            "trace_id": trace.trace_id,
            "span_id": trace.span_id,
            "parent_span_id": trace.parent_span_id,
            "agent": trace.agent,
            "intent": trace.intent,
            "model": trace.model,
            "latency_ms": trace.duration_ms,
            "token_usage": {
                "prompt_tokens": trace.tokens_in,
                "completion_tokens": trace.tokens_out,
                "total_tokens": trace.tokens_in + trace.tokens_out,
            },
            "cache_metrics": {
                "cache_read_input_tokens": trace.cache_read_tokens,
                "cache_creation_input_tokens": trace.cache_creation_tokens,
                "cache_hit_rate": trace.cache_hit_rate,
            },
            "estimated_cost": trace.cost_usd,
            "prompt_version": trace.prompt_version,
            "status": trace.status,
            "error": trace.error or "",
        }
        try:
            with open(trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trace_event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trace event: {e}")

    def trace_chain(self, chain: ChainTrace):
        """Record a full chain trace."""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        chain_file = AGENTS_LOG_DIR / f"{today}.chain.jsonl"

        entry = asdict(chain)
        try:
            with open(chain_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write chain trace: {e}")

    # ── Audit ──

    def audit(self, action: str, user: str, target: str, result: str, detail: str = ""):
        """Write an audit log entry."""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        audit_file = AUDIT_LOG_DIR / f"{today}.jsonl"

        entry = {
            "timestamp": datetime.now(TZ).isoformat(),
            "action": action,
            "user": user,
            "target": target,
            "result": result,
            "detail": detail,
            "trace_id": "",  # filled if audit is called within a trace
        }
        try:
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit: {e}")

    # ── Stats ──

    def stats(self, days: int = 7) -> dict[str, dict[str, Any]]:
        """Aggregate agent stats for the last N days."""
        agent_stats: dict[str, dict] = {}

        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            daily_file = AGENTS_LOG_DIR / f"{date}.jsonl"

            if not daily_file.exists():
                continue

            try:
                for line in daily_file.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue
                    entry = json.loads(line)
                    agent = entry.get("agent", "unknown")
                    if agent not in agent_stats:
                        agent_stats[agent] = {
                            "count": 0,
                            "errors": 0,
                            "timeouts": 0,
                            "total_duration_ms": 0,
                            "total_tokens": 0,
                            "avg_duration_ms": 0,
                            "error_rate": 0.0,
                        }
                    s = agent_stats[agent]
                    s["count"] += 1
                    status = entry.get("status", "")
                    if status == "error":
                        s["errors"] += 1
                    elif status == "timeout":
                        s["timeouts"] += 1
                    s["total_duration_ms"] += entry.get("duration_ms", 0)
                    s["total_tokens"] += entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
            except Exception as e:
                logger.error(f"Failed to read stats for {date}: {e}")

        # Calculate derived metrics
        for _agent, s in agent_stats.items():
            if s["count"] > 0:
                s["avg_duration_ms"] = round(s["total_duration_ms"] / s["count"], 1)
                s["error_rate"] = round((s["errors"] + s["timeouts"]) / s["count"], 3)

        return agent_stats

    # ── EDD Metrics ──

    def edc_snapshot(self) -> dict[str, Any]:
        """Take a full metrics snapshot for EDD tools."""
        stats = self.stats(days=1)

        # Calculate totals
        total_calls = sum(s["count"] for s in stats.values())
        total_errors = sum(s["errors"] for s in stats.values())
        total_duration = sum(s["total_duration_ms"] for s in stats.values())

        snapshot = {
            "timestamp": datetime.now(TZ).isoformat(),
            "period_days": 1,
            "total_calls": total_calls,
            "total_errors": total_errors,
            "total_duration_ms": total_duration,
            "error_rate": round(total_errors / total_calls, 3) if total_calls > 0 else 0.0,
            "agents": stats,
            "health": {},
        }

        # Write to EDC directory for EDD tools
        try:
            edc_file = EDC_LOG_DIR / "metrics.json"
            with open(edc_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write EDC snapshot: {e}")

        return snapshot

    # ── Health Log ──

    def health_log(self, agent_name: str, status: str, duration_ms: float, detail: str = ""):
        """Write a health check result."""
        HEALTH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        health_file = HEALTH_LOG_DIR / "status.jsonl"

        entry = {
            "timestamp": datetime.now(TZ).isoformat(),
            "agent": agent_name,
            "status": status,  # green / yellow / red / dead
            "duration_ms": round(duration_ms, 1),
            "detail": detail,
        }
        try:
            with open(health_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write health log: {e}")

    # ── Trace 汇总 ──

    def trace_summary(self, trace_id: str, days: int = 7) -> dict[str, Any] | None:
        """按 trace_id 汇总一个请求在所有产物中的全链指标。

        读取 logs/agents/{date}.jsonl + {date}.chain.jsonl，
        聚合该 trace 的成本/耗时/Tokens/缓存命中率。

        Args:
            trace_id: 目标 trace_id。
            days: 回溯天数（默认 7）。

        Returns:
            Dict 含 trace_id、chat_id、spans、total_latency_ms、total_tokens、
            total_cache_read、total_cache_creation、total_cost、avg_cache_hit_rate，
            未找到时返回 None。
        """
        spans: list[dict[str, Any]] = []
        total_latency = 0.0
        total_tokens = 0
        total_cache_read = 0
        total_cache_creation = 0
        total_cost = 0.0
        chat_id = ""
        sender_id = ""

        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            trace_file = AGENTS_LOG_DIR / f"{date}.jsonl"
            if not trace_file.exists():
                continue
            try:
                for line in trace_file.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("trace_id") != trace_id:
                        continue
                    chat_id = entry.get("chat_id", chat_id)
                    sender_id = entry.get("sender_id", sender_id)
                    spans.append(entry)
                    total_latency += entry.get("duration_ms", 0)
                    total_tokens += entry.get("tokens_in", 0) + entry.get("tokens_out", 0)
                    total_cache_read += entry.get("cache_read_tokens", 0) or 0
                    total_cache_creation += entry.get("cache_creation_tokens", 0) or 0
                    total_cost += entry.get("cost_usd", 0.0) or 0.0
            except Exception as e:
                logger.error(f"Failed to read trace summary for {date}: {e}")

        if not spans:
            return None

        # 去重后排序（按 span 时间）
        spans.sort(key=lambda s: s.get("timestamp", "") or "")

        hit_total = total_cache_read + total_cache_creation + total_tokens
        return {
            "trace_id": trace_id,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "span_count": len(spans),
            "agents": sorted({s.get("agent", "?") for s in spans}),
            "spans": spans,
            "total_latency_ms": round(total_latency, 1),
            "total_tokens": total_tokens,
            "total_cache_read": total_cache_read,
            "total_cache_creation": total_cache_creation,
            "total_cost": round(total_cost, 6),
            "avg_cache_hit_rate": round(
                total_cache_read / hit_total, 4
            ) if hit_total > 0 else 0.0,
            "timestamp": datetime.now(TZ).isoformat(),
        }


# Global tracer
tracer = AgentTracer()