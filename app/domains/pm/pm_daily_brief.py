"""PM Daily Brief - 双轨日计划聚合与渲染

问题
----
机器人的"日计划"一直只有研发轨（sht000 的版本闸门任务），
而 PMO 真正占时间的事（合同台账、客户对接、报价模型）在 sht001 运营轨里，
机器完全看不见。用户看到推送自然觉得"内容太少"。

两条轨的分工
------------
    研发轨 sht000  需求 → 责任人 → 闸门 → 倒计时
                   我能做什么：按 SOP 倒排，提示哪条要提测/冻结
    运营轨 sht001  我自己的事项 → 状态 → 挂了多少天
                   我能做什么：提醒未完成项，接收状态回写

接口分层
--------
    build_daily_brief(today)  -> DailyBrief   数据，可被 agent 复用
    render_daily_brief(brief) -> str          给人看的文案

数据与渲染分开，是因为后续 agent 需要「结构化判断」而不只是「拼一段话」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm.pm_weekly_plan import (
    ActivePlan,
    WeeklyItem,
    load_active_plan,
    stale_age,
)
from app.domains.pm.pm_todo_intake import planned_items
from app.domains.pm import pm_scope

logger = logging.getLogger("pm_hub.daily_brief")

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 研发轨只展示这么近的版本。再远就是噪音，会稀释真正要收口的事。
VERSION_HORIZON_DAYS = 10
# 卡点最多列这么多条，多了群里不看
MAX_WARNINGS = 5
# 运营轨最多列这么多条
MAX_ITEMS = 12


@dataclass
class DailyBrief:
    """一天的全部输入，供渲染与 agent 共用。"""
    today: date
    week: ActivePlan
    planned: List[Dict[str, Any]] = field(default_factory=list)   # 今日计划：他自己录的
    items: List[WeeklyItem] = field(default_factory=list)     # 运营轨未完成
    versions: List[Dict[str, Any]] = field(default_factory=list)  # 研发轨临近版本
    warnings: List[Dict[str, Any]] = field(default_factory=list)  # 研发轨卡点
    error: Optional[str] = None                               # 取数失败时的说明


# ══════════════════════════════════════════════════════════════════════
# 取数
# ══════════════════════════════════════════════════════════════════════

def _load_radar(today: date) -> Dict[str, Any]:
    """版本表的故事先剔掉不归他管的业务线，再算雷达。

    版本表是多业务线共用的，星企通、7709 这些混在同一张表里。不先剔掉，
    日计划里就会出现「星企通 showcase 延期」这种他根本不负责的卡点。
    这里只做减法，不做白名单 —— 表里有些需求标题不带业务线字样，白名单会误删。
    """
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    stories = [
        s for s in parse_stories(vals)
        if not pm_scope.out_of_scope_hit(s.title)
    ]
    return evaluate_daily_radar(stories, today=today)


def _dedupe_warnings(warnings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一需求可能命中多条规则（比如既卡提测又写错测试责任人），
    群里刷三条一样的没意义，按 (版本, 需求, 提示) 去重。"""
    seen: set[tuple] = set()
    out: List[Dict[str, Any]] = []
    for w in warnings or []:
        key = (w.get("version"), w.get("title"), w.get("msg"))
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def build_daily_brief(today: Optional[date] = None) -> DailyBrief:
    """聚合两条轨。任一轨失败不影响另一轨，错误写进 error 字段。"""
    today = today or date.today()
    errors: List[str] = []

    # 运营轨
    try:
        plan = load_active_plan(today)
    except Exception as e:
        logger.exception("运营轨读取失败")
        plan = ActivePlan(block=None, stale_days=0, is_stale=True)
        errors.append(f"运营轨读取失败: {e}")

    items = plan.block.open_items()[:MAX_ITEMS] if plan.block else []

    # 今日计划：他自己昨天/今天录的。和上面的「本周未完成」是两个来源，
    # 一个是日粒度（多维表格日计划表），一个是周粒度（sht001 周块）。
    planned: List[Dict[str, Any]] = []
    try:
        planned = planned_items(today)
    except Exception as e:
        logger.exception("读取今日计划失败")
        errors.append(f"今日计划读取失败: {e}")

    # 研发轨
    versions: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    try:
        radar = _load_radar(today)
        versions = [
            v for v in radar.get("active_versions", [])
            if v.get("workdays_left", 999) <= VERSION_HORIZON_DAYS
        ]
        versions.sort(key=lambda v: v.get("workdays_left", 999))
        warnings = _dedupe_warnings(radar.get("warnings", []))[:MAX_WARNINGS]
    except Exception as e:
        logger.exception("研发轨读取失败")
        errors.append(f"研发轨读取失败: {e}")

    return DailyBrief(
        today=today,
        week=plan,
        planned=planned,
        items=items,
        versions=versions,
        warnings=warnings,
        error="；".join(errors) if errors else None,
    )


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def _render_week_header(brief: DailyBrief) -> str:
    """周信息只保留必要提示。停更时必须说出来，否则用户以为是旧数据。"""
    if not brief.week.block:
        return ""
    label = brief.week.block.label
    if brief.week.is_stale and brief.week.stale_days > 0:
        return f"\n（注意：工作簿停留在「{label}」，已过期 {brief.week.stale_days} 天）"
    return f"\n（{label} · {brief.week.block.span}）"


def _render_planned(brief: DailyBrief) -> List[str]:
    """他自己录的计划。没录就不占版面。

    带「今N」编号，和下面「本周未完成」的裸数字分开：两套编号指向两张表
    （日计划表 vs sht001 周计划），前缀撞了的话「3 完成」就不知道该改哪张。
    """
    if not brief.planned:
        return []

    lines = [f"【今日计划】{len(brief.planned)} 项"]
    for i, p in enumerate(brief.planned, 1):
        tail = f"  {p['gate']}" if p.get("gate") else ""
        lines.append(f"今{i}. {p['title']}{tail}")
    return lines


def _render_items(brief: DailyBrief) -> List[str]:
    if not brief.items:
        return ["【本周未完成】本周事项已清空"]

    lines = [f"【本周未完成】{len(brief.items)} 项"]
    for it in brief.items:
        no = it.no if it.no not in (None, "") else "-"
        age = stale_age(it, brief.today)
        # 挂超过 5 天才提示，否则每条都带天数会变成噪音
        tail = f"{it.status} · 挂 {age} 天" if age and age > 5 else it.status
        lines.append(f"{no}. {it.task}    {tail}")
    return lines


def _fmt_days(left: int) -> str:
    if left > 0:
        return f"剩 {left} 个工作日"
    if left == 0:
        return "今天上线"
    return f"已过上线 {abs(left)} 天"


def _render_gates(brief: DailyBrief) -> List[str]:
    if not brief.versions and not brief.warnings:
        return []

    lines = ["", "【版本闸门】"]
    for v in brief.versions:
        gate = v.get("current_gate") or "未进闸门"
        lines.append(
            f"{v['version']} · {_fmt_days(v.get('workdays_left', 0))} · "
            f"{v.get('story_count', 0)} 条需求 · {gate}"
        )

    if brief.warnings:
        lines.append("")
        lines.append(f"卡点 {len(brief.warnings)} 条")
        for w in brief.warnings:
            title = w.get("title") or w.get("msg") or ""
            tag = "🔴" if w.get("level") == "HIGH" else "🟡"
            extra = ""
            if w.get("owner"):
                extra = f"  {w['owner']}" + (f"/{w['qa']}" if w.get("qa") else "")
            lines.append(f"{tag} {title}{extra}")
            if w.get("msg") and w.get("title"):
                lines.append(f"    {w['msg']}")

    return lines


def render_daily_brief(brief: DailyBrief) -> str:
    """面向群消息的纯文本。飞书 text 消息不渲染 markdown，所以不用任何 * 语法。"""
    wd = _WEEKDAY_CN[brief.today.weekday()]
    head = f"📋 今日计划 · {brief.today.strftime('%m-%d')} {wd}"

    lines: List[str] = [head + _render_week_header(brief), ""]
    planned_lines = _render_planned(brief)
    if planned_lines:
        lines += planned_lines
        lines.append("")
    lines += _render_items(brief)
    lines += _render_gates(brief)

    if brief.error:
        lines += ["", f"（有取数异常：{brief.error}）"]

    if brief.planned or brief.items:
        tips = []
        if brief.planned:
            tips.append("今日计划说「今2 完成」")
        if brief.items:
            tips.append("本周事项说「3 完成」")
        lines += ["", "；".join(tips) + "，我直接改对应那张表。"]

    return "\n".join(lines)


def build_and_render(today: Optional[date] = None) -> str:
    """一步到位，供 daemon 的定时任务调用。"""
    return render_daily_brief(build_daily_brief(today))


if __name__ == "__main__":
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8")
    print(build_and_render())