"""PM 日计划卡片 —— 把「一份长文本」变成能扫、能下钻、能点的一张卡。

为什么改卡片
------------
原渲染是一整段纯文本：`【今日计划】`、`【本周未完成】`、`【版本闸门】` 三块
顺序排下来。问题不在信息量，而在**扫不动**：他早上第一眼要回答的是
「今天要收什么」，而文本里三块等权重平铺，卡点和计划挤在一起。

卡片用两种手段解决：
1. **分区**：分成三个 `column_set`（计划 / 未完成 / 闸门），未完成带挂账天数徽标；
2. **下钻**：底部一排按钮直接跳到对应表格，不用再从聊天里翻链接。

不变量
------
- **数据仍来自 `build_daily_brief`**，这里只换渲染，不动取数。
- **保留纯文本渲染**：卡片是增强，`render_daily_brief` 仍是卡片发不出去时的
  兜底，也是 `pm_agent` 的 `pm_daily_brief` 工具返回的形态（模型读文本更稳）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.config import load_config
from app.domains.pm.pm_daily_brief import DailyBrief, _WEEKDAY_CN, _fmt_days

logger = logging.getLogger("pm_hub.daily_card")

# 卡片里每块最多列多少条。超出折叠成「还有 N 条」，点按钮去看全量。
MAX_PLANNED = 6
MAX_ITEMS = 8
MAX_WARNINGS = 4


def _sheet_url(token: str, sheet_id: str = "") -> str:
    """表格直达链接。token 为空就不给按钮——死链比没有更糟。"""
    if not token:
        return ""
    base = f"https://feishu.cn/sheets/{token}"
    return f"{base}?sheet={sheet_id}" if sheet_id else base


def _bitable_url() -> str:
    cfg = getattr(load_config(), "pm", None)
    outputs = getattr(cfg, "outputs", None)
    base = (getattr(outputs, "base_token", "") or "").strip()
    table = (getattr(outputs, "table_id", "") or "").strip()
    if not base:
        return ""
    return f"https://feishu.cn/base/{base}?table={table}" if table else f"https://feishu.cn/base/{base}"


def _stat_columns(pairs: List[tuple]) -> Dict[str, Any]:
    """一排统计块（`[(标签, 值), ...]`），横向等分。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                "tag": "markdown",
                "content": f"<font color='grey'>{label}</font>\n**{value}**",
            }]} for label, value in pairs
        ],
    }


def _title(brief: DailyBrief) -> str:
    wd = _WEEKDAY_CN[brief.today.weekday()]
    return f"📋 今日计划 · {brief.today.strftime('%m-%d')} {wd}"


def _template(brief: DailyBrief) -> str:
    """卡片头部配色：有 HIGH 级卡点用红，只有普通卡点用橙，其余绿。"""
    if any(w.get("level") == "HIGH" for w in brief.warnings or []):
        return "red"
    if brief.warnings:
        return "orange"
    return "green"


def build_daily_card(brief: DailyBrief) -> Dict[str, Any]:
    """把 `DailyBrief` 渲染成日计划卡片。"""
    elements: List[Dict[str, Any]] = []

    # ── 概览：四个数字，一眼看完今天的分量 ──
    high = sum(1 for w in brief.warnings or [] if w.get("level") == "HIGH")
    elements.append(_stat_columns([
        ("今日计划", str(len(brief.planned))),
        ("本周未完成", str(len(brief.items))),
        ("临近版本", str(len(brief.versions))),
        ("卡点", f"{len(brief.warnings)}(🔴{high})" if high else str(len(brief.warnings))),
    ]))

    if brief.week.block:
        label = brief.week.block.label
        if brief.week.is_stale and brief.week.stale_days > 0:
            elements.append({
                "tag": "markdown",
                "content": (f"<font color='red'>工作簿停在「{label}」，"
                            f"已过期 {brief.week.stale_days} 天</font>"),
            })
        else:
            elements.append({
                "tag": "markdown",
                "content": f"<font color='grey'>{label} · {brief.week.block.span}</font>",
            })

    # ── 今日计划 ──
    if brief.planned:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**今日计划**"})
        for i, p in enumerate(brief.planned[:MAX_PLANNED], 1):
            gate = f"  <font color='grey'>{p['gate']}</font>" if p.get("gate") else ""
            elements.append({"tag": "markdown", "content": f"**今{i}.** {p['title']}{gate}"})
        if len(brief.planned) > MAX_PLANNED:
            rest = len(brief.planned) - MAX_PLANNED
            elements.append({"tag": "markdown",
                             "content": f"<font color='grey'>…还有 {rest} 项</font>"})

    # ── 本周未完成 ──
    if brief.items:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**本周未完成**"})
        for it in brief.items[:MAX_ITEMS]:
            no = it.no if it.no not in (None, "") else "-"
            age = _stale_days(it, brief)
            # 挂超过 5 天才提示，否则每条都带天数会变成噪音（与文本渲染同口径）
            badge = (f"  <font color='red'>挂 {age} 天</font>"
                     if age > 5 else f"  <font color='grey'>{it.status}</font>")
            elements.append({"tag": "markdown", "content": f"**{no}.** {it.task}{badge}"})
        if len(brief.items) > MAX_ITEMS:
            rest = len(brief.items) - MAX_ITEMS
            elements.append({"tag": "markdown",
                             "content": f"<font color='grey'>…还有 {rest} 项</font>"})
    elif not brief.planned:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "本周事项已清空"})

    # ── 版本闸门 ──
    if brief.versions or brief.warnings:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**版本闸门**"})
        for v in brief.versions:
            gate = v.get("current_gate") or "未进闸门"
            elements.append({
                "tag": "markdown",
                "content": (f"`{v['version']}` · {_fmt_days(v.get('workdays_left', 0))} · "
                            f"{v.get('story_count', 0)} 条 · {gate}"),
            })

        if brief.warnings:
            for w in brief.warnings[:MAX_WARNINGS]:
                title = w.get("title") or w.get("msg") or ""
                icon = "🔴" if w.get("level") == "HIGH" else "🟡"
                who = ""
                if w.get("owner"):
                    who = f" · {w['owner']}" + (f"/{w['qa']}" if w.get("qa") else "")
                msg = f"\n<font color='grey'>{w['msg']}</font>" if w.get("msg") and w.get("title") else ""
                elements.append({"tag": "markdown",
                                 "content": f"{icon} {title}{who}{msg}"})
            if len(brief.warnings) > MAX_WARNINGS:
                rest = len(brief.warnings) - MAX_WARNINGS
                elements.append({"tag": "markdown",
                                 "content": f"<font color='grey'>…还有 {rest} 条卡点</font>"})

    if brief.error:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown",
                         "content": f"<font color='red'>取数异常：{brief.error}</font>"})

    # ── 下钻按钮 ──
    actions = _action_buttons()
    if actions:
        elements.append({"tag": "hr"})
        elements.append({"tag": "column_set", "flex_mode": "flow", "columns": actions})

    if brief.planned or brief.items:
        elements.append({
            "tag": "markdown",
            "content": "<font color='grey'>回「今2 完成」改日计划；回「3 完成」改本周事项</font>",
        })

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": _title(brief)},
            "template": _template(brief),
        },
        "body": {"elements": elements},
    }


def _stale_days(item: Any, brief: DailyBrief) -> int:
    """挂账天数。取不到就当 0（不显示徽标），不猜。"""
    try:
        from app.domains.pm.pm_weekly_plan import stale_age

        return int(stale_age(item, brief.today) or 0)
    except Exception:
        return 0


def _action_buttons() -> List[Dict[str, Any]]:
    """底部直达按钮。链接拿不到就不出按钮——死链比没有更糟。"""
    from app.domains.pm.pm_hub_engine import SHEET_ID_VERSIONS, SHEET_ID_WEEKLY, SPREADSHEET_TOKEN

    out: List[Dict[str, Any]] = []

    def _btn(label: str, url: str) -> Dict[str, Any]:
        return {"tag": "column", "width": "auto", "elements": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "multi_url": {"url": url, "pc_url": url, "android_url": url, "ios_url": url},
        }]}

    bitable = _bitable_url()
    if bitable:
        out.append(_btn("日计划表", bitable))
    weekly = _sheet_url(SPREADSHEET_TOKEN, SHEET_ID_WEEKLY)
    if weekly:
        out.append(_btn("周计划跟踪", weekly))
    versions = _sheet_url(SPREADSHEET_TOKEN, SHEET_ID_VERSIONS)
    if versions:
        out.append(_btn("版本计划", versions))
    return out
