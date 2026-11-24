"""Dashboard statistics DTOs — real system operational data from logs/ file system.

All fields have safe defaults for empty/no-data states.
"""
from __future__ import annotations

from pydantic import BaseModel


class AgentStats(BaseModel):
    """Per-agent aggregated statistics."""

    calls: int = 0
    errors: int = 0
    timeouts: int = 0
    success_rate: float = 0.0
    avg_latency_ms: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


class DashboardStats(BaseModel):
    """Dashboard overview stats — aggregated from logs/agents/ + bot/todos/ + logs/health/."""

    today_calls: int = 0
    total_calls: int = 0
    pending_review: int = 0
    today_runs: int = 0
    success_rate: float = 0.0
    today_llm_calls: int = 0
    today_llm_tokens: int = 0
    today_llm_cost: float = 0.0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_hit_rate: float = 0.0
    agent_health: dict = {}  # from logs/health/current.json
    agent_stats: dict[str, AgentStats] = {}  # per-agent breakdown
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    recent_traces: list[dict] = []


class DailyTrend(BaseModel):
    """Single-day aggregated metrics."""

    date: str = ""
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost: float = 0.0
    cache_hit_rate: float = 0.0
    avg_latency_ms: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    success_rate: float = 0.0


class TrendsOut(BaseModel):
    """Multi-day trend data."""

    days: list[DailyTrend] = []


class TraceStep(BaseModel):
    """Single step within a trace chain."""

    agent: str = ""
    model: str = ""
    latency_ms: int = 0
    status: str = "success"
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit_rate: float = 0.0
    timestamp: str = ""


class TraceDetailOut(BaseModel):
    """Full trace detail for a single request."""

    trace_id: str = ""
    steps: list[TraceStep] = []
    total_latency_ms: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    avg_cache_hit_rate: float = 0.0


class RouteOutcome(BaseModel):
    """路由首跳聚合（用户视角：这句话被交给了谁）。"""

    key: str = ""
    label: str = ""
    count: int = 0


class TaskView(BaseModel):
    """单条编排任务的用户视角视图（logs/tasks/ TaskRecord 投影）。"""

    task_id: str = ""
    ts: str = ""
    sender_name: str = ""
    query: str = ""
    route_chain: list[str] = []
    route_labels: list[str] = []
    final_agent: str = ""
    final_label: str = ""
    status: str = ""
    duration_ms: float = 0
    response_preview: str = ""
    gate_blocked: bool = False
    results_quality: str = "ok"
    demoted: bool = False
    feedback_negative: bool = False
    clarify_question: str = ""


class ReportItem(BaseModel):
    """报告列表条目（logs/tasks/ 投影 + has_reply 标记）。"""

    task_id: str = ""
    ts: str = ""
    agent: str = ""
    agent_label: str = ""
    query: str = ""
    status: str = ""
    duration_ms: float = 0
    preview: str = ""
    has_reply: bool = False


class ReportsOut(BaseModel):
    """报告列表输出。"""

    days: int = 7
    total: int = 0
    reports: list[ReportItem] = []


class ReportDetailOut(BaseModel):
    """报告全文输出（logs/task_replies/{task_id}.md + 档案元信息）。"""

    task_id: str = ""
    ts: str = ""
    agent: str = ""
    agent_label: str = ""
    query: str = ""
    status: str = ""
    duration_ms: float = 0
    content: str = ""


class BehaviorOut(BaseModel):
    """消息行为闭环统计 — 从用户视角回答「我发一句话会发生什么」。"""


    period_days: int = 1
    total_messages: int = 0
    replied: int = 0
    errors: int = 0
    gaveup: int = 0
    direct_answered: int = 0
    routed_to_agent: int = 0
    clarified: int = 0
    helped: int = 0
    gate_blocked: int = 0
    demoted: int = 0
    negative_feedback: int = 0
    avg_duration_ms: int = 0
    p50_duration_ms: int = 0
    p95_duration_ms: int = 0
    route_outcomes: list[RouteOutcome] = []
    recent_tasks: list[TaskView] = []


class RuntimeInterruptedItem(BaseModel):
    """一条重启中断任务的摘要。"""

    task_id: str = ""
    query_preview: str = ""
    sender_name: str = ""
    updated_at: str = ""


class RuntimeOut(BaseModel):
    """运行时护栏观测 — 并发闸/排队/中断（logs/runtime_tasks/ + config.toml）。"""

    guard_enabled: bool = False
    max_concurrent: int = 0
    max_queue: int = 0
    queue_timeout_sec: float = 0.0
    active_running: int = 0
    experienced_queue: int = 0
    outcomes: dict[str, int] = {}
    interrupted: list[RuntimeInterruptedItem] = []
    scanned_at: str = ""


class ScheduledTaskItem(BaseModel):
    """一条定时任务档案摘要（控制台任务表行）。"""

    task_id: str = ""
    status: str = ""
    instruction: str = ""
    schedule: str = ""          # 人话描述（每 30 分钟 → 测试群）
    agent: str = ""
    target_name: str = ""
    creator_name: str = ""
    run_count: int = 0
    last_run_ok: bool | None = None
    last_run_at: str = ""
    last_error: str = ""
    next_run_at: str = ""


class ScheduledRunItem(BaseModel):
    """一条逐次执行流水（logs/scheduler_runs/）。"""

    task_id: str = ""
    instruction: str = ""
    started_at: str = ""
    duration_ms: int = 0
    outcome: str = ""
    error: str = ""


class ScheduledTasksOut(BaseModel):
    """定时任务监控面板 — KPI + 任务表 + 执行流水（logs/scheduled_tasks/ + scheduler_runs/）。"""

    kpi_active: int = 0          # 调度中
    kpi_paused: int = 0
    kpi_pending: int = 0
    kpi_runs_today: int = 0
    kpi_fail_today: int = 0
    kpi_next_run_at: str = ""    # 最近一次将要触发的时刻
    tasks: list[ScheduledTaskItem] = []
    recent_runs: list[ScheduledRunItem] = []
    scanned_at: str = ""


class UserTrace(BaseModel):
    """Single trace entry for a user."""

    trace_id: str = ""
    timestamp: str = ""
    input_text: str = ""
    output_text: str = ""
    agent: str = ""
    status: str = "success"
    duration_ms: float = 0
    tokens: int = 0
    cost_usd: float = 0.0


class UserStats(BaseModel):
    """User statistics aggregated from agent logs."""

    sender_id: str = ""
    sender_name: str = ""
    chat_count: int = 0
    total_calls: int = 0
    error_count: int = 0
    last_active: str = ""
    first_seen: str = ""
    success_rate: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    recent_queries: list[str] = []


class UserStatsOut(BaseModel):
    """User list API response."""

    total_users: int = 0
    total_calls: int = 0
    users: list[UserStats] = []


class UserDetailOut(BaseModel):
    """Full user trace detail."""

    user: UserStats
    traces: list[UserTrace] = []


class BehaviorDay(BaseModel):
    """单日行为闭环分布（趋势用）。"""

    date: str = ""
    total: int = 0
    direct_answered: int = 0
    routed_to_agent: int = 0
    clarified: int = 0
    helped: int = 0
    gate_blocked: int = 0
    errors: int = 0


class BehaviorTrendsOut(BaseModel):
    """多日行为闭环趋势。"""

    days: list[BehaviorDay] = []

class CcUsageDay(BaseModel):
    """单日 Claude Code 通道用量（转录聚合）。"""

    date: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


class CcUsageOut(BaseModel):
    """Claude Code 通道费用观测 — 数据源 ~/.claude/projects 转录。"""

    model: str = ""
    days: list[CcUsageDay] = []
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    scanned_files: int = 0
    cached_files: int = 0
