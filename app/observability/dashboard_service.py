"""Dashboard service — aggregates metrics from logs/ file system (no DB dependency).

Reads from:
  - logs/agents/{date}.jsonl — AgentTrace records
  - logs/agents/trace.jsonl — structured trace events
  - logs/health/current.json — health check snapshot
  - bot/todos/pending/ — pending message count
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas.dashboard import (
    AgentStats,
    BehaviorDay,
    BehaviorOut,
    BehaviorTrendsOut,
    DailyTrend,
    DashboardStats,
    ReportDetailOut,
    ReportItem,
    ReportsOut,
    RouteOutcome,
    RuntimeInterruptedItem,
    RuntimeOut,
    ScheduledRunItem,
    ScheduledTaskItem,
    ScheduledTasksOut,
    TaskView,
    TraceDetailOut,
    TraceStep,
    TrendsOut,
    UserDetailOut,
    UserStats,
    UserStatsOut,
    UserTrace,
)

logger = logging.getLogger("dashboard_service")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
AGENTS_LOG_DIR = PROJECT_DIR / "logs" / "agents"
HEALTH_LOG_DIR = PROJECT_DIR / "logs" / "health"
TODOS_DIR = PROJECT_DIR / "bot" / "todos"
TASKS_LOG_DIR = PROJECT_DIR / "logs" / "tasks"
TASK_REPLIES_DIR = PROJECT_DIR / "logs" / "task_replies"
RUNTIME_TASKS_DIR = PROJECT_DIR / "logs" / "runtime_tasks"


# ── Helpers ──────────────────────────────────────────────────


def _today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _read_agent_log(date: str) -> list[dict[str, Any]]:
    """Read a single day's agent trace log."""
    fp = AGENTS_LOG_DIR / f"{date}.jsonl"
    if not fp.exists():
        return []
    try:
        return [
            json.loads(line)
            for line in fp.read_text(encoding="utf-8").strip().split("\n")
            if line.strip()
        ]
    except Exception as e:
        logger.warning(f"Failed to read {fp}: {e}")
        return []


def _count_pending() -> int:
    """Count pending messages in bot/todos/pending/."""
    pending_dir = TODOS_DIR / "pending"
    if not pending_dir.exists():
        return 0
    return len(list(pending_dir.glob("*.md")))


def _read_health_snapshot() -> dict:
    """Read current health snapshot."""
    fp = HEALTH_LOG_DIR / "current.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Public API ───────────────────────────────────────────────


# ── Helpers ──


def _percentile(values: list[float], p: int) -> int:
    """Compute p-th percentile from a list of values."""
    if not values:
        return 0
    sorted_vals = sorted(values)
    k = (p / 100.0) * (len(sorted_vals) - 1)
    f = int(k)
    c = f + 1
    if c >= len(sorted_vals):
        return int(sorted_vals[-1])
    return int(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]))


def get_dashboard_stats() -> DashboardStats:
    """Aggregate dashboard overview stats from today's logs."""
    today = _today_str()
    entries = _read_agent_log(today)

    total_calls = len(entries)
    total_errors = 0
    total_success = 0
    llm_calls = 0
    total_tokens_in = 0
    total_tokens_out = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cost = 0.0
    total_duration = 0.0
    all_durations: list[float] = []
    recent_traces: list[dict] = []
    per_agent: dict[str, dict[str, Any]] = {}

    for entry in entries:
        agent = entry.get("agent", "?")
        status = entry.get("status", "")
        dur = entry.get("duration_ms", 0) or 0

        if status == "success":
            total_success += 1
        elif status == "error":
            total_errors += 1
        total_tokens_in += entry.get("tokens_in", 0) or 0
        total_tokens_out += entry.get("tokens_out", 0) or 0
        cr = entry.get("cache_read_tokens", 0) or 0
        cc = entry.get("cache_creation_tokens", 0) or 0
        total_cache_read += cr
        total_cache_creation += cc
        total_cost += entry.get("cost_usd", 0.0) or 0.0
        total_duration += dur
        all_durations.append(dur)

        if entry.get("model"):
            llm_calls += 1

        # Per-agent aggregation
        if agent not in per_agent:
            per_agent[agent] = {"calls": 0, "errors": 0, "timeouts": 0,
                                "durations": [], "total_tokens": 0, "total_cost": 0.0}
        pa = per_agent[agent]
        pa["calls"] += 1
        if status == "error":
            pa["errors"] += 1
        elif status == "timeout":
            pa["timeouts"] += 1
        pa["durations"].append(dur)
        pa["total_tokens"] += (entry.get("tokens_in", 0) or 0) + (entry.get("tokens_out", 0) or 0)
        pa["total_cost"] += entry.get("cost_usd", 0.0) or 0.0

        # Collect recent traces (last 20)
        if len(recent_traces) < 20:
            recent_traces.append({
                "trace_id": entry.get("trace_id", ""),
                "agent": agent,
                "status": status,
                "duration_ms": dur,
                "timestamp": entry.get("timestamp", ""),
                "tokens": (entry.get("tokens_in", 0) or 0) + (entry.get("tokens_out", 0) or 0),
            })

    # Build AgentStats
    agent_stats: dict[str, AgentStats] = {}
    for name, pa in per_agent.items():
        c = pa["calls"]
        agent_stats[name] = AgentStats(
            calls=c,
            errors=pa["errors"],
            timeouts=pa["timeouts"],
            success_rate=round((c - pa["errors"] - pa["timeouts"]) / c, 4) if c > 0 else 0.0,
            avg_latency_ms=round(sum(pa["durations"]) / c) if c > 0 else 0,
            p50_latency_ms=_percentile(pa["durations"], 50),
            p95_latency_ms=_percentile(pa["durations"], 95),
            total_tokens=pa["total_tokens"],
            total_cost=round(pa["total_cost"], 6),
        )

    # Cache hit rate
    total_input = total_cache_read + total_cache_creation + total_tokens_in
    cache_hit_rate = round(total_cache_read / total_input, 4) if total_input > 0 else 0.0

    # Success rate
    success_rate = round(total_success / total_calls, 4) if total_calls > 0 else 0.0

    # P50 / P95 latency (global)
    p50 = _percentile(all_durations, 50)
    p95 = _percentile(all_durations, 95)

    # Health snapshot
    health = _read_health_snapshot()

    # Pending count
    pending = _count_pending()

    # Calculate total across all days
    all_days_total = 0
    for d in range(7):
        date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
        past = _read_agent_log(date)
        all_days_total += len(past)

    return DashboardStats(
        today_calls=total_calls,
        total_calls=all_days_total,
        pending_review=pending,
        today_runs=total_calls,
        success_rate=success_rate,
        today_llm_calls=llm_calls,
        today_llm_tokens=total_tokens_in + total_tokens_out,
        today_llm_cost=round(total_cost, 6),
        cache_read_tokens=total_cache_read,
        cache_creation_tokens=total_cache_creation,
        cache_hit_rate=cache_hit_rate,
        agent_health=health,
        agent_stats=agent_stats,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        recent_traces=recent_traces,
    )


def get_trends(days: int = 7) -> TrendsOut:
    """Aggregate daily trends for the last N days.

    数据源 logs/tasks/（TaskRecord 任务档案）——LLM 用量（tokens/cost/model）
    的真实落盘处。logs/agents/ 的 trace 日志不携带 token/cost（写入侧未填），
    仅用于最近消息列表，不再作为趋势源。
    llm_calls：新档案直接累加；旧档案（无该字段）按「有 model 即一次调用」近似。
    cache_hit_rate：无真实落盘数据源，恒 0（前端已下架该指标）。
    """
    now = datetime.now(TZ)
    trends: list[DailyTrend] = []

    for d in range(days):
        date = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        entries = _read_task_log(date)

        if not entries:
            continue

        total_calls = len(entries)
        total_success = 0
        llm_calls = 0
        total_tokens = 0
        total_cost = 0.0
        all_durations: list[float] = []

        for entry in entries:
            if entry.get("status") == "success":
                total_success += 1
            model = entry.get("model", "")
            llm_calls += int(entry.get("llm_calls", 0) or 0) or (1 if model else 0)
            total_tokens += entry.get("tokens_total", 0) or 0
            total_cost += entry.get("cost_usd", 0.0) or 0.0
            all_durations.append(entry.get("duration_ms", 0) or 0)

        success_rate = round(total_success / total_calls, 4) if total_calls > 0 else 0.0
        avg_latency = round(sum(all_durations) / total_calls) if total_calls > 0 else 0

        trends.append(DailyTrend(
            date=date,
            llm_calls=llm_calls,
            llm_tokens=total_tokens,
            llm_cost=round(total_cost, 6),
            cache_hit_rate=0.0,
            avg_latency_ms=avg_latency,
            p50_latency_ms=_percentile(all_durations, 50),
            p95_latency_ms=_percentile(all_durations, 95),
            success_rate=success_rate,
        ))

    # Sort by date ascending
    trends.sort(key=lambda t: t.date)
    return TrendsOut(days=trends)


def get_trace_detail(trace_id: str, days: int = 7) -> TraceDetailOut | None:
    """Get full trace detail for a specific trace_id."""
    now = datetime.now(TZ)
    steps: list[TraceStep] = []
    total_latency = 0
    total_tokens = 0
    total_cost = 0.0
    total_cache_read = 0
    total_cache_creation = 0
    total_input = 0

    for d in range(days):
        date = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        entries = _read_agent_log(date)

        for entry in entries:
            if entry.get("trace_id") != trace_id:
                continue

            ti = entry.get("tokens_in", 0) or 0
            to = entry.get("tokens_out", 0) or 0
            cr = entry.get("cache_read_tokens", 0) or 0
            cc = entry.get("cache_creation_tokens", 0) or 0
            latency = entry.get("duration_ms", 0) or 0

            step_input = cr + cc + ti
            step_hit = round(cr / step_input, 4) if step_input > 0 else 0.0

            steps.append(TraceStep(
                agent=entry.get("agent", ""),
                model=entry.get("model", ""),
                latency_ms=round(latency),
                status=entry.get("status", "success"),
                tokens_in=ti,
                tokens_out=to,
                cache_hit_rate=step_hit,
                timestamp=entry.get("timestamp", ""),
            ))

            total_latency += latency
            total_tokens += ti + to
            total_cost += entry.get("cost_usd", 0.0) or 0.0
            total_cache_read += cr
            total_cache_creation += cc
            total_input += step_input

    if not steps:
        return None

    steps.sort(key=lambda s: s.timestamp)
    avg_hit = round(total_cache_read / total_input, 4) if total_input > 0 else 0.0

    return TraceDetailOut(
        trace_id=trace_id,
        steps=steps,
        total_latency_ms=round(total_latency),
        total_tokens=total_tokens,
        total_cost=round(total_cost, 6),
        avg_cache_hit_rate=avg_hit,
    )


# ── User Stats ──


def _read_all_agent_logs(days: int = 7) -> list[dict]:
    """Read all agent logs across days, newest first."""
    now = datetime.now(TZ)
    entries: list[dict] = []
    for d in range(days):
        date = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        entries.extend(_read_agent_log(date))
    return entries


# ── Behavior lens（用户视角行为闭环）─────────────────────────


# 路由首跳 → 用户视角标签（与 _help_node 的能力命名保持一致口径）
_ROUTE_LABELS: dict[str, str] = {
    "direct_answer": "直接自答",
    "route_to_help": "查看能力介绍",
    "route_to_clarify": "反问澄清",
    "route_to_giveup": "诚实告知做不到",
    # dsh 链路没有「路由到某 Agent」这一步，档案里的 final_agent 是执行面本身；
    # 链路上写的是实际调用过的工具名（见 app/dsh/task_record.py）。
    "dsh": "dsh 内核",
}


def _route_label(step: str) -> str:
    if step in _ROUTE_LABELS:
        return _ROUTE_LABELS[step]
    if step.endswith("-agent"):
        base = step[: -len("-agent")]
        names = {
            "db": "数据库查询",
            "deploy": "发布管理",
            "config": "配置管理",
            "daily": "技术日报",
            "webportal": "工单系统",
            "schedule": "定时任务",
            "digital-oracle": "数字先知",
        }
        return names.get(base, base)
    if step == "dev-bot":
        return "开发助手"
    if step.startswith("skill-"):
        return {"skill-elk-log-query": "ELK 日志查询", "skill-order-sql": "工单→SQL",
                "skill-demo-guard": "项目巡检"}.get(step, step[len("skill-"):])
    return step


def _read_task_log(date: str) -> list[dict[str, Any]]:
    """读 logs/tasks/{date}.jsonl（TaskRecord 档案）。"""
    fp = TASKS_LOG_DIR / f"{date}.jsonl"
    if not fp.exists():
        return []
    try:
        return [
            json.loads(line)
            for line in fp.read_text(encoding="utf-8").strip().split("\n")
            if line.strip()
        ]
    except Exception as e:
        logger.warning(f"Failed to read {fp}: {e}")
        return []


def _task_view(e: dict[str, Any]) -> TaskView:
    chain = e.get("route_chain") or []
    return TaskView(
        task_id=e.get("task_id", ""),
        ts=e.get("ts", ""),
        sender_name=e.get("sender_name", "") or "用户",
        query=(e.get("query") or "")[:120],
        route_chain=list(chain),
        route_labels=[_route_label(s) for s in chain],
        final_agent=e.get("final_agent", ""),
        final_label=_route_label(
            chain[0] if chain else (e.get("final_agent") or "")
        ),
        status=e.get("status", ""),
        duration_ms=e.get("duration_ms", 0) or 0,
        response_preview=(e.get("response_preview") or "")[:120],
        gate_blocked=bool(e.get("gate_blocked")),
        results_quality=e.get("results_quality", "ok") or "ok",
        demoted=bool(e.get("demoted")),
        feedback_negative=bool((e.get("feedback") or {}).get("negative")),
        clarify_question=(e.get("clarify_question") or "")[:120],
    )


def get_behavior(days: int = 1) -> BehaviorOut:
    """消息行为闭环统计 — 从用户视角回答「我发一句话会发生什么」。

    数据源 logs/tasks/（TaskRecord），区分：
    direct_answered / routed_to_agent / clarified / helped / gaveup / gate_blocked。
    """
    entries: list[dict[str, Any]] = []
    for d in range(max(1, days)):
        date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
        entries.extend(_read_task_log(date))
    entries.sort(key=lambda e: e.get("ts", ""))

    total = len(entries)
    durations: list[float] = []
    route_counter: dict[str, int] = {}
    outcomes = BehaviorOut(period_days=days, total_messages=total)
    recent: list[TaskView] = []

    for e in entries:
        status = e.get("status", "")
        durations.append(e.get("duration_ms", 0) or 0)
        fb = e.get("feedback") or {}
        if fb.get("negative"):
            outcomes.negative_feedback += 1
        if e.get("gate_blocked"):
            outcomes.gate_blocked += 1
        if e.get("demoted"):
            outcomes.demoted += 1

        chain = e.get("route_chain") or []
        first = chain[0] if chain else ""
        if first:
            route_counter[first] = route_counter.get(first, 0) + 1

        if first == "direct_answer" or e.get("final_agent") == "direct-answer":
            outcomes.direct_answered += 1
        elif first == "route_to_help" or e.get("final_agent") == "help":
            outcomes.helped += 1
        elif first == "route_to_clarify" or e.get("final_agent") == "clarify":
            outcomes.clarified += 1
        elif first == "route_to_giveup":
            outcomes.gaveup += 1
        elif first:
            outcomes.routed_to_agent += 1

        if status == "success":
            outcomes.replied += 1
        elif status == "error":
            outcomes.errors += 1
        elif status == "failed":
            outcomes.gaveup += 1

        recent.append(_task_view(e))

    outcomes.errors = max(outcomes.errors, total - outcomes.replied)
    outcomes.avg_duration_ms = int(sum(durations) / total) if total else 0
    outcomes.p50_duration_ms = _percentile(durations, 50)
    outcomes.p95_duration_ms = _percentile(durations, 95)
    outcomes.route_outcomes = [
        RouteOutcome(key=k, label=_route_label(k), count=c)
        for k, c in sorted(route_counter.items(), key=lambda kv: -kv[1])
    ]
    outcomes.recent_tasks = list(reversed(recent))[-30:]
    return outcomes


def get_behavior_trends(days: int = 7) -> BehaviorTrendsOut:
    """最近 N 天行为闭环分布趋势。"""
    now = datetime.now(TZ)
    out: list[BehaviorDay] = []
    for d in range(days):
        date = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        entries = _read_task_log(date)
        if not entries:
            continue
        day = BehaviorDay(date=date, total=len(entries))
        for e in entries:
            chain = e.get("route_chain") or []
            first = chain[0] if chain else ""
            if e.get("gate_blocked"):
                day.gate_blocked += 1
            if first == "direct_answer" or e.get("final_agent") == "direct-answer":
                day.direct_answered += 1
            elif first == "route_to_help" or e.get("final_agent") == "help":
                day.helped += 1
            elif first == "route_to_clarify" or e.get("final_agent") == "clarify":
                day.clarified += 1
            elif first:
                day.routed_to_agent += 1
            if e.get("status") == "success":
                day.errors += 0
            else:
                day.errors += 1
        out.append(day)
    out.sort(key=lambda x: x.date)
    return BehaviorTrendsOut(days=out)


# ── Runtime guard 观测 ───────────────────────────────────────


def get_runtime() -> RuntimeOut:
    """运行时护栏观测 — 并发闸配置、排队体验、中断任务。

    从 logs/runtime_tasks/ 的生命周期档案推导：
    - active_running: 最后状态仍为 running 的任务数（≈当前在跑）
    - experienced_queue: queued_ms > 0 的任务数（经历过「正在排队处理」的用户）
    - outcomes: replied / busy_rejected / queue_timeout / error 分布
    """
    out = RuntimeOut(scanned_at=datetime.now(TZ).isoformat())
    guard_cfg: dict[str, Any] = {}
    try:
        from app.config import load_config

        cfg = load_config()
        rg = cfg.runtime_guard
        out.guard_enabled = rg.enabled
        out.max_concurrent = rg.max_concurrent
        out.max_queue = rg.max_queue
        out.queue_timeout_sec = rg.queue_timeout_sec
        guard_cfg = {"persist_enabled": rg.persist_enabled}
    except Exception as e:
        logger.warning("Failed to load runtime_guard config: %s", e)

    last_by_task: dict[str, dict[str, Any]] = {}
    if guard_cfg.get("persist_enabled", True) and RUNTIME_TASKS_DIR.exists():
        for path in sorted(RUNTIME_TASKS_DIR.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    last_by_task[rec.get("task_id", "")] = rec
            except Exception as e:
                logger.warning("Failed to read %s: %s", path, e)

    outcomes_counter: dict[str, int] = {}
    interrupted: list[RuntimeInterruptedItem] = []
    for rec in last_by_task.values():
        status = rec.get("status", "")
        outcome = rec.get("outcome", "")
        if status == "running":
            out.active_running += 1
            interrupted.append(RuntimeInterruptedItem(
                task_id=rec.get("task_id", ""),
                query_preview=(rec.get("query_preview") or "")[:80],
                sender_name=rec.get("sender_name", "") or "用户",
                updated_at=rec.get("updated_at", ""),
            ))
        if outcome:
            outcomes_counter[outcome] = outcomes_counter.get(outcome, 0) + 1
        if (rec.get("queued_ms") or 0) > 0:
            out.experienced_queue += 1
    interrupted.sort(key=lambda r: r.updated_at, reverse=True)
    out.outcomes = outcomes_counter
    out.interrupted = interrupted[:20]
    return out


# ── Scheduled tasks 观测 ─────────────────────────────────────


_STATUS_LABELS_SVT = {
    "pending": "待审批",
    "approved": "调度中",
    "paused": "已暂停",
    "rejected": "已驳回",
    "done": "已过期",
    "cancelled": "已取消",
}


def get_scheduled_tasks(days: int = 30) -> ScheduledTasksOut:
    """定时任务监控 — 档案全景（logs/scheduled_tasks/*/manifest.json）+ 逐次执行流水。

    数据源零 DB：任务状态读目录树，执行历史读 logs/scheduler_runs/{date}.jsonl。
    """
    out = ScheduledTasksOut(scanned_at=datetime.now(TZ).isoformat())
    try:
        from app.ops.scheduler.runs import run_recorder
        from app.ops.scheduler.store import TASKS_DIR, ScheduledTaskStore

        store = ScheduledTaskStore(TASKS_DIR)
        all_tasks = store.list_by_status(
            "pending", "approved", "paused", "rejected", "done", "cancelled"
        )
        today = _today_str()
        next_ts = 0.0
        for t in all_tasks:
            if t.status == "approved":
                out.kpi_active += 1
                if t.next_run_ts and (next_ts == 0 or t.next_run_ts < next_ts):
                    next_ts = t.next_run_ts
            elif t.status == "paused":
                out.kpi_paused += 1
            elif t.status == "pending":
                out.kpi_pending += 1
            out.tasks.append(ScheduledTaskItem(
                task_id=t.task_id,
                status=_STATUS_LABELS_SVT.get(t.status, t.status),
                instruction=t.instruction[:80],
                schedule=t.describe(),
                agent=t.agent,
                target_name=t.target_name or t.target_id,
                creator_name=t.creator_name or t.creator_id,
                run_count=t.run_count,
                last_run_ok=t.last_run_ok if t.run_count else None,
                last_run_at=t.last_run_at,
                last_error=t.last_error[:80],
                next_run_at=(
                    datetime.fromtimestamp(t.next_run_ts, TZ).strftime("%m-%d %H:%M")
                    if t.status == "approved" and t.next_run_ts else ""
                ),
            ))
        if next_ts:
            out.kpi_next_run_at = datetime.fromtimestamp(next_ts, TZ).strftime("%m-%d %H:%M")

        runs = run_recorder.read_recent(days=days)
        out.recent_runs = [
            ScheduledRunItem(
                task_id=r.get("task_id", ""),
                instruction=(r.get("instruction") or "")[:60],
                started_at=r.get("started_at", ""),
                duration_ms=r.get("duration_ms", 0),
                outcome=r.get("outcome", ""),
                error=(r.get("error") or "")[:80],
            )
            for r in runs[:50]
        ]
        out.kpi_runs_today = sum(1 for r in runs if (r.get("started_at") or "").startswith(today))
        out.kpi_fail_today = sum(
            1 for r in runs
            if (r.get("started_at") or "").startswith(today) and r.get("outcome") == "fail"
        )
        # 新→旧：任务按最近执行排前
        out.tasks.sort(key=lambda t: t.last_run_at or "", reverse=True)
    except Exception as e:
        logger.warning("get_scheduled_tasks failed: %s", e)
    return out


def get_user_stats(days: int = 7) -> UserStatsOut:
    """Aggregate per-user statistics from agent logs."""
    entries = _read_all_agent_logs(days)

    # Group by sender_id
    user_data: dict[str, dict] = {}
    for e in entries:
        uid = e.get("sender_id", "") or "unknown"
        if uid not in user_data:
            user_data[uid] = {
                "sender_name": e.get("sender_name", "") or uid[:16],
                "chats": set(),
                "calls": 0,
                "errors": 0,
                "tokens": 0,
                "cost": 0.0,
                "timestamps": [],
                "queries": [],
                "first_seen": e.get("timestamp", ""),
            }
        u = user_data[uid]
        u["chats"].add(e.get("chat_id", ""))
        u["calls"] += 1
        if e.get("status") == "error":
            u["errors"] += 1
        u["tokens"] += (e.get("tokens_in", 0) or 0) + (e.get("tokens_out", 0) or 0)
        u["cost"] += e.get("cost_usd", 0.0) or 0.0
        u["timestamps"].append(e.get("timestamp", ""))
        if e.get("input_text"):
            u["queries"].append(e["input_text"][:100])
        if e.get("timestamp", "") and (not u["first_seen"] or e["timestamp"] < u["first_seen"]):
            u["first_seen"] = e["timestamp"]

    users: list[UserStats] = []
    for uid, u in user_data.items():
        timestamps = sorted(u["timestamps"], reverse=True)
        queries = u["queries"][-5:]  # last 5 queries
        success_rate = round((u["calls"] - u["errors"]) / u["calls"], 4) if u["calls"] > 0 else 0.0
        users.append(UserStats(
            sender_id=uid,
            sender_name=u["sender_name"],
            chat_count=len(u["chats"]),
            total_calls=u["calls"],
            error_count=u["errors"],
            last_active=timestamps[0] if timestamps else "",
            first_seen=u["first_seen"],
            success_rate=success_rate,
            total_tokens=u["tokens"],
            total_cost=round(u["cost"], 6),
            recent_queries=queries,
        ))

    users.sort(key=lambda u: u.total_calls, reverse=True)
    total_calls = sum(u.total_calls for u in users)
    return UserStatsOut(total_users=len(users), total_calls=total_calls, users=users)


def get_user_detail(sender_id: str, days: int = 7) -> UserDetailOut | None:
    """Get full trace detail for a specific user."""
    entries = _read_all_agent_logs(days)
    user_entries = [e for e in entries if e.get("sender_id") == sender_id]

    if not user_entries:
        return None

    # Build user stats
    chats = set()
    errors = 0
    tokens = 0
    cost = 0.0
    timestamps = []
    queries = []
    first_seen = ""

    for e in user_entries:
        chats.add(e.get("chat_id", ""))
        if e.get("status") == "error":
            errors += 1
        tokens += (e.get("tokens_in", 0) or 0) + (e.get("tokens_out", 0) or 0)
        cost += e.get("cost_usd", 0.0) or 0.0
        timestamps.append(e.get("timestamp", ""))
        if e.get("input_text"):
            queries.append(e["input_text"][:100])
        if e.get("timestamp", "") and (not first_seen or e["timestamp"] < first_seen):
            first_seen = e["timestamp"]

    timestamps.sort(reverse=True)
    success_rate = round((len(user_entries) - errors) / len(user_entries), 4)
    user_name = user_entries[0].get("sender_name", "") or sender_id[:16]

    user_stats = UserStats(
        sender_id=sender_id,
        sender_name=user_name,
        chat_count=len(chats),
        total_calls=len(user_entries),
        error_count=errors,
        last_active=timestamps[0] if timestamps else "",
        first_seen=first_seen,
        success_rate=success_rate,
        total_tokens=tokens,
        total_cost=round(cost, 6),
        recent_queries=queries[-5:],
    )

    # Build trace list
    traces: list[UserTrace] = []
    for e in sorted(user_entries, key=lambda x: x.get("timestamp", ""), reverse=True)[:50]:
        traces.append(UserTrace(
            trace_id=e.get("trace_id", ""),
            timestamp=e.get("timestamp", ""),
            input_text=(e.get("input_text", "") or "")[:200],
            output_text=(e.get("output_text", "") or "")[:200],
            agent=e.get("agent", ""),
            status=e.get("status", "success"),
            duration_ms=e.get("duration_ms", 0) or 0,
            tokens=(e.get("tokens_in", 0) or 0) + (e.get("tokens_out", 0) or 0),
            cost_usd=e.get("cost_usd", 0.0) or 0.0,
        ))

    return UserDetailOut(user=user_stats, traces=traces)

# ── 报告中心（logs/tasks/ 档案 + logs/task_replies/ 全文） ─────────


def _has_reply(task_id: str) -> bool:
    """该任务是否有落盘的回复全文。"""
    return bool(task_id) and (TASK_REPLIES_DIR / f"{task_id}.md").is_file()


def get_reports(days: int = 7, agent: str = "", limit: int = 200) -> ReportsOut:
    """报告列表 — 有全文回复的任务档案（新→旧），供控制台「报告」tab。"""
    items: list[ReportItem] = []
    for d in range(max(1, days)):
        date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
        for e in _read_task_log(date):
            tid = e.get("task_id", "")
            if not _has_reply(tid):
                continue
            agent_name = e.get("final_agent", "")
            if agent and agent_name != agent:
                continue
            items.append(ReportItem(
                task_id=tid,
                ts=e.get("ts", ""),
                agent=agent_name,
                agent_label=_route_label(agent_name),
                query=(e.get("query") or "")[:120],
                status=e.get("status", ""),
                duration_ms=e.get("duration_ms", 0) or 0,
                preview=(e.get("response_preview") or "")[:120],
                has_reply=True,
            ))
    items.sort(key=lambda r: r.ts, reverse=True)
    return ReportsOut(days=days, total=len(items), reports=items[:limit])


def get_report_detail(task_id: str) -> ReportDetailOut | None:
    """报告全文 — logs/task_replies/{task_id}.md + 档案元信息。"""
    from app.observability.task_record import reply_store

    content = reply_store.read(task_id)
    if content is None:
        return None
    # 剥掉落盘时的 YAML 前置头（元信息从档案读，正文才是报告）
    if content.startswith("---\n"):
        _, _, rest = content.partition("\n---\n")
        content = rest.lstrip("\n") if rest else content

    meta = None
    for d in range(7):
        date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
        for e in _read_task_log(date):
            if e.get("task_id") == task_id:
                meta = e
                break
        if meta:
            break
    agent_name = (meta or {}).get("final_agent", "")
    return ReportDetailOut(
        task_id=task_id,
        ts=(meta or {}).get("ts", ""),
        agent=agent_name,
        agent_label=_route_label(agent_name),
        query=(meta or {}).get("query", ""),
        status=(meta or {}).get("status", ""),
        duration_ms=(meta or {}).get("duration_ms", 0) or 0,
        content=content,
    )
