"""PM 日计划卡片 —— 信息密度与「不丢信息」的平衡。

守四条：
1. 三个分区都在（计划 / 未完成 / 闸门），不会因为某一区为空就整段消失时误读；
2. 挂账天数超过阈值才标红，否则只是灰字状态（与文本渲染同口径）；
3. 表格链接拿不到就不出按钮——死链比没有更糟；
4. 超量条目要显式说「还有 N 条」，不能静默截断。
"""

from __future__ import annotations

import json
from datetime import date


from app.domains.pm import pm_daily_card
from app.domains.pm.pm_daily_brief import DailyBrief
from app.domains.pm.pm_weekly_plan import ActivePlan, WeeklyItem


class _Block:
    label = "2026年11月第 2 周"
    span = "11-10 ~ 11-16"
    items: list = []
    period = ""


def _week(stale_days: int = 0) -> ActivePlan:
    return ActivePlan(block=_Block(), stale_days=stale_days, is_stale=stale_days > 0)


def _brief(planned=None, items=None, versions=None, warnings=None,
           stale_days: int = 0, error=None) -> DailyBrief:
    return DailyBrief(
        today=date(2026, 11, 16),
        week=_week(stale_days),
        planned=planned or [],
        items=items or [],
        versions=versions or [],
        warnings=warnings or [],
        error=error,
    )


def _item(no, task, status="进行中", start=""):
    return WeeklyItem(no=no, task=task, priority="", owner="", start=start,
                      due="", status=status, remark="", row=no)


def _flat(card) -> str:
    return json.dumps(card, ensure_ascii=False)


def test_header_carries_date_and_weekday():
    card = pm_daily_card.build_daily_card(_brief())
    assert card["header"]["title"]["content"] == "📋 今日计划 · 11-16 周一"


def test_header_turns_red_on_high_warning():
    """有 HIGH 级卡点时头部要红——他一眼要能看出今天有硬堵点。"""
    card = pm_daily_card.build_daily_card(_brief(warnings=[
        {"level": "HIGH", "title": "某需求", "msg": "卡住了"},
    ]))
    assert card["header"]["template"] == "red"


def test_header_orange_on_normal_warning():
    card = pm_daily_card.build_daily_card(_brief(warnings=[
        {"level": "LOW", "title": "某需求", "msg": "注意下"},
    ]))
    assert card["header"]["template"] == "orange"


def test_stale_week_is_called_out():
    """工作簿停更必须显式说，否则用户以为是旧数据。"""
    card = pm_daily_card.build_daily_card(_brief(stale_days=3))
    assert "已过期 3 天" in _flat(card)


def test_over_threshold_item_shows_day_badge():
    """挂账超过 5 天标红——这是「为什么不推进」的直接线索。"""
    card = pm_daily_card.build_daily_card(_brief(items=[
        _item(4, "预约买入需求排期", start="2026-11-09"),
    ]))
    assert "挂" in _flat(card)


def test_truncation_is_explicit():
    """截断必须说「还有 N 项」，不能静默少列。"""
    items = [_item(i, f"事项{i}") for i in range(1, 21)]
    card = pm_daily_card.build_daily_card(_brief(items=items))
    assert f"还有 {20 - pm_daily_card.MAX_ITEMS} 项" in _flat(card)


def test_warning_truncation_is_explicit():
    warnings = [{"level": "LOW", "title": f"卡点{i}", "msg": "x"} for i in range(10)]
    card = pm_daily_card.build_daily_card(_brief(warnings=warnings))
    assert f"还有 {10 - pm_daily_card.MAX_WARNINGS} 条卡点" in _flat(card)


def test_no_buttons_when_tokens_missing(monkeypatch):
    """链接拿不到就不要按钮：死链比没有更糟。"""
    import app.domains.pm.pm_hub_engine as engine

    monkeypatch.setattr(engine, "SPREADSHEET_TOKEN", "")
    monkeypatch.setattr(pm_daily_card, "_bitable_url", lambda: "")
    card = pm_daily_card.build_daily_card(_brief())
    assert '"tag": "button"' not in _flat(card)


def test_error_is_surfaced():
    card = pm_daily_card.build_daily_card(_brief(error="读表超时"))
    assert "读表超时" in _flat(card)


def test_stat_row_always_present():
    """概览四个数字恒在：即使全空也要能看出「今天真的没事」。"""
    card = pm_daily_card.build_daily_card(_brief())
    flat = _flat(card)
    for label in ("今日计划", "本周未完成", "临近版本", "卡点"):
        assert label in flat


def test_empty_state_says_cleared():
    card = pm_daily_card.build_daily_card(_brief())
    assert "本周事项已清空" in _flat(card)


def test_item_with_markdown_chars_does_not_crash():
    """事项名里带尖括号/星号时不能崩——表格是人工维护的，脏字符很正常。"""
    card = pm_daily_card.build_daily_card(_brief(items=[_item(1, "a<b>c *d*")]))
    assert "a<b>c" in _flat(card)
