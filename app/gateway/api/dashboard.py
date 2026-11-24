"""Dashboard API endpoints — FastAPI router.

Endpoint groups:
  GET /api/stats/dashboard         → DashboardStats (overview KPI)
  GET /api/stats/dashboard/trends  → TrendsOut (N-day trend data)
  GET /api/stats/trace/{trace_id}  → TraceDetailOut (full chain detail)
  GET /api/stats/users             → UserStatsOut (user aggregation)
  GET /api/stats/users/{sender_id} → UserDetailOut (user detail)
  GET /api/stats/behavior          → BehaviorOut (user-view message behavior loop)
  GET /api/stats/behavior/trends   → BehaviorTrendsOut (N-day behavior trend)
  GET /api/stats/runtime           → RuntimeOut (runtime guard observation)
  GET /api/stats/cc-usage          → CcUsageOut (Claude Code 通道 token/费用聚合)
  GET /api/stats/reports           → ReportsOut (报告列表：有全文回复的任务档案)
  GET /api/stats/reports/{task_id} → ReportDetailOut (报告全文 markdown)
  GET /api/agents                  → 能力清单 + 运行时开关状态
  POST /api/agents/switch          → 动态上下线某能力（写开关文件 → 热重建路由）
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.observability import cc_usage_service, dashboard_service
from app.schemas.dashboard import (
    BehaviorOut,
    BehaviorTrendsOut,
    CcUsageOut,
    DashboardStats,
    ReportDetailOut,
    ReportsOut,
    RuntimeOut,
    ScheduledTasksOut,
    TraceDetailOut,
    TrendsOut,
    UserDetailOut,
    UserStatsOut,
)

logger = logging.getLogger("api.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/stats/dashboard", response_model=DashboardStats)
def dashboard_stats() -> DashboardStats:
    """Get dashboard overview stats (KPI cards)."""
    return dashboard_service.get_dashboard_stats()


@router.get("/stats/dashboard/trends", response_model=TrendsOut)
def dashboard_trends(days: int = 7) -> TrendsOut:
    """Get multi-day trend data (7-day chart)."""
    return dashboard_service.get_trends(days=days)


@router.get("/stats/trace/{trace_id}", response_model=TraceDetailOut)
def trace_detail(trace_id: str) -> TraceDetailOut:
    """Get full trace detail for a specific trace_id."""
    result = dashboard_service.get_trace_detail(trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"trace_id={trace_id} not found")
    return result


@router.get("/stats/users", response_model=UserStatsOut)
def user_stats(days: int = 7) -> UserStatsOut:
    """Get aggregated user statistics."""
    return dashboard_service.get_user_stats(days=days)


@router.get("/stats/users/{sender_id}", response_model=UserDetailOut)
def user_detail(sender_id: str, days: int = 7) -> UserDetailOut:
    """Get full trace detail for a specific user."""
    result = dashboard_service.get_user_detail(sender_id, days=days)
    if result is None:
        raise HTTPException(status_code=404, detail=f"sender_id={sender_id} not found")
    return result


@router.get("/stats/behavior", response_model=BehaviorOut)
def behavior(days: int = Query(1, ge=1, le=30)) -> BehaviorOut:
    """消息行为闭环 — 用户视角「发一句话会发生什么」（logs/tasks/）。"""
    return dashboard_service.get_behavior(days=days)


@router.get("/stats/behavior/trends", response_model=BehaviorTrendsOut)
def behavior_trends(days: int = Query(7, ge=1, le=30)) -> BehaviorTrendsOut:
    """最近 N 天行为闭环分布趋势。"""
    return dashboard_service.get_behavior_trends(days=days)


@router.get("/stats/runtime", response_model=RuntimeOut)
def runtime() -> RuntimeOut:
    """运行时护栏观测 — 并发闸/排队/中断任务（logs/runtime_tasks/）。"""
    return dashboard_service.get_runtime()


@router.get("/stats/scheduled", response_model=ScheduledTasksOut)
def scheduled_tasks(days: int = Query(30, ge=1, le=90)) -> ScheduledTasksOut:
    """定时任务监控 — 档案全景 + 逐次执行流水（logs/scheduled_tasks/ + scheduler_runs/）。"""
    return dashboard_service.get_scheduled_tasks(days=days)


@router.get("/stats/cc-usage", response_model=CcUsageOut)
def cc_usage(days: int = Query(7, ge=1, le=90)) -> CcUsageOut:
    """Claude Code 通道费用 — 聚合 ~/.claude/projects 转录的 token 用量与成本。"""
    raw = cc_usage_service.scan_cc_usage(days=days)
    return CcUsageOut(
        model=raw["model"],
        days=raw["days"],
        total_calls=raw["totals"]["calls"],
        total_input_tokens=raw["totals"]["input_tokens"],
        total_output_tokens=raw["totals"]["output_tokens"],
        total_cache_read_tokens=raw["totals"]["cache_read_tokens"],
        total_cost_usd=raw["totals"]["cost_usd"],
        scanned_files=raw["scanned_files"],
        cached_files=raw["cached_files"],
    )

@router.get("/stats/reports", response_model=ReportsOut)
def reports(
    days: int = Query(7, ge=1, le=30),
    agent: str = Query("", max_length=60),
) -> ReportsOut:
    """报告列表 — 最近 N 天有全文回复的任务档案（新→旧）。"""
    return dashboard_service.get_reports(days=days, agent=agent)


@router.get("/stats/reports/{task_id}", response_model=ReportDetailOut)
def report_detail(task_id: str) -> ReportDetailOut:
    """报告全文 — 按 task_id 取 logs/task_replies/ 下的 markdown 原文。"""
    result = dashboard_service.get_report_detail(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"report {task_id!r} not found")
    return result

# ── 能力开关（控制台动态上下线） ─────────────────────────────────
# main.py 启动时注入 registry 与 rebuild 回调；dashboard 与 bot 同进程，
# 开关变更即刻热生效（无跨进程通信）。

_agent_registry: Any | None = None
_rebuild_callback: Any | None = None


def configure_agent_control(registry: Any, rebuild_callback: Any) -> None:
    """注入 registry 与 CrewOrchestrator.rebuild（main.py 启动时调用一次）。"""
    global _agent_registry, _rebuild_callback
    _agent_registry = registry
    _rebuild_callback = rebuild_callback


class AgentSwitchIn(BaseModel):
    """POST /api/agents/switch 请求体。"""

    name: str
    enabled: bool


@router.get("/agents")
def list_agents() -> dict[str, Any]:
    """能力清单：路由可见性、通道、来源（内置/扩展技能/热插拔）+ 开关状态。"""
    if _agent_registry is None:
        raise HTTPException(status_code=503, detail="agent control not wired")

    hotplug: dict[str, str] = {}
    try:
        from app.ops.capability.store import CapabilityStore

        for rec in CapabilityStore().active_capabilities():
            reg_name = f"skill-{rec.name}" if rec.type == "skill" else rec.name
            hotplug[reg_name] = rec.capability_id
    except Exception as e:
        logger.warning("[agents] hotplug listing failed: %s", e)

    switch_off = _agent_registry.switch_off
    agents = []
    for a in _agent_registry.list():
        if not a.enabled:
            continue
        if a.name in hotplug:
            source = "hotplug"
        elif a.skill_name:
            source = "skill"
        else:
            source = "builtin"
        agents.append({
            "name": a.name,
            "display_name": a.display_name,
            "transport": a.transport,
            "permission_level": a.permission_level,
            # 路由可见性 = yaml routable ∧ 运行时开关（与 registry.routable 同口径）
            "routable": a.routable and a.name not in switch_off,
            "switch_enabled": a.name not in switch_off,
            "source": source,
            "capability_id": hotplug.get(a.name, ""),
        })
    return {
        "agents": agents,
        "switches_off": sorted(switch_off),
        "rebuild_hooked": _rebuild_callback is not None,
    }


@router.post("/agents/switch")
def switch_agent(payload: AgentSwitchIn) -> dict[str, Any]:
    """动态上下线一个能力：写开关文件 → registry 热更新 → 路由图重建。

    enabled=false 仅「强制下线」（路由/help/分解不可见），不影响健康探测
    与直通分派；enabled=true 恢复到 agent.yaml 的声明值（dev-bot 等
    routable=false 的 Agent 不会被开关强行上线）。
    """
    if _agent_registry is None or _rebuild_callback is None:
        raise HTTPException(status_code=503, detail="agent control not wired")
    if _agent_registry.get(payload.name) is None:
        raise HTTPException(status_code=404, detail=f"agent {payload.name!r} not found")

    from app.agents.switches import load_switches, save_switches

    switch_off = load_switches()
    if payload.enabled:
        switch_off.discard(payload.name)
    else:
        switch_off.add(payload.name)
    save_switches(switch_off)
    _agent_registry.apply_switches(switch_off)
    ok, detail = _rebuild_callback()
    logger.info("[agents] switch %s -> %s (rebuild: %s, %s)",
                payload.name, "on" if payload.enabled else "off", ok, detail)
    if not ok:
        # 重建失败：registry 内存开关已更新但路由图未换——如实暴露给调用方
        raise HTTPException(status_code=500, detail=f"switch saved but rebuild failed: {detail}")
    return {
        "ok": True,
        "name": payload.name,
        "enabled": payload.enabled,
        "switches_off": sorted(_agent_registry.switch_off),
        "rebuild": detail,
    }
