"""PM 催办卡片 —— 把「发出去就收不回」的动作从口令字改成按钮确认。

为什么必须用卡片
----------------
纯文本确认的口径是「说『确认』我就以你的身份全发出去」。这个动作有三层风险
（挑错人 / 说错话 / 发错地方），而确认方式是一个可以手滑打出来的词。
更要紧的是**改草稿**：原设计要用户手打「第2条改成：……」，手机上极难用，
实际效果是「要么照发要么全弃」，中间的修正这条路等于没有。

卡片把三件事变成一次点击：每条一个「改这条」输入、底部「全部发送 / 全部取消」。

不变量
------
1. **草稿仍归 `pm_nudge._PENDING` 管**。卡片只是它的视图与入口；
   权威状态永远在 `pm_nudge`，卡片被删、超时、回调丢失都不影响这份状态。
2. **发送动作仍走 `pm_nudge.commit`**，一行不改。卡片不自己拼发消息逻辑，
   否则「文本路径能发、卡片路径发不出去」这种双实现迟早分化。
3. **卡片回调一定要验归属**：飞书的卡片回调广播给所有注册 handler，
   判据是 value 里的 `pn` 键（nudge 专用），不认识的静默让过。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.protocols.feishu_card import ensure_button_behaviors

logger = logging.getLogger("pm_hub.nudge_card")

# 回调 value 的判别键。与反馈卡的 `fb`、审批卡的 `task` 并列，互不认领。
NUDGE_KEY = "pn"

ACTION_SEND = "send"
ACTION_DROP = "drop"
ACTION_EDIT = "edit"
ACTION_ONE_SEND = "onesend"
ACTION_ONE_DROP = "onedrop"

_ROLE_ICON = {"dev": "👨‍💻", "qa": "🧪", "pm": "📋"}


def build_nudge_card(items: List[Any], batch_id: str,
                     title: str = "") -> Dict[str, Any]:
    """把一批 PendingNudge 渲染成确认卡片。

    Args:
        items: `pm_nudge.PendingNudge` 列表（只用 `.nudge` 与 `.body`）。
        batch_id: 本次暂存的标识，随按钮回传——回调据此确认「这是哪一批」，
                  避免用户点了几天前的旧卡片却改到当前草稿。
        title: 可选标题前缀（带筛选范围说明时用）。
    """
    elements: List[Dict[str, Any]] = []

    if title:
        elements.append({"tag": "markdown",
                         "content": f"<font color='grey'>{title}</font>"})
    elements.append({
        "tag": "markdown",
        "content": f"**✍️ 拟了 {len(items)} 条催办** —— 确认后才发，发出去收不回。",
    })
    elements.append({"tag": "hr"})

    for idx, item in enumerate(items, 1):
        n = item.nudge
        icon = _ROLE_ICON.get(n.role, "·")
        meta = f"{icon} 第 {idx} 条 · 找 **{n.audience}** · {n.reason}"
        elements.append({"tag": "markdown", "content": meta})
        # 正文单独一块并加底色：这是真正会发给对方的话，必须和元信息区分开
        elements.append({
            "tag": "markdown",
            "content": f"> {item.body.replace(chr(10), chr(10) + '> ')}",
        })
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "改这条"},
                    "type": "default",
                    "value": {NUDGE_KEY: ACTION_EDIT, "bid": batch_id, "i": idx},
                }]},
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "发这条"},
                    "type": "default",
                    "value": {NUDGE_KEY: ACTION_ONE_SEND, "bid": batch_id, "i": idx},
                }]},
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "不发这条"},
                    "type": "default",
                    "value": {NUDGE_KEY: ACTION_ONE_DROP, "bid": batch_id, "i": idx},
                }]},
            ],
        })
        if idx < len(items):
            elements.append({"tag": "hr"})

    if len(items) > 1:
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"全部发送（{len(items)} 条）"},
                    "type": "primary",
                    "value": {NUDGE_KEY: ACTION_SEND, "bid": batch_id},
                }]},
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "全部取消"},
                    "type": "danger",
                    "value": {NUDGE_KEY: ACTION_DROP, "bid": batch_id},
                }]},
            ],
        })
    else:
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "发送"},
                    "type": "primary",
                    "value": {NUDGE_KEY: ACTION_SEND, "bid": batch_id},
                }]},
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消"},
                    "type": "danger",
                    "value": {NUDGE_KEY: ACTION_DROP, "bid": batch_id},
                }]},
            ],
        })

    elements.append({
        "tag": "markdown",
        "content": "<font color='grey'>也可以直接回文字：「第2条改成：……」/「确认」/「取消」</font>",
    })

    return ensure_button_behaviors({
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "催办确认"},
            "template": "orange",
        },
        "body": {"elements": elements},
    })


def build_ask_card(batch_id: str, index: int, current: str) -> Dict[str, Any]:
    """点「改这条」后的卡片：**不做**卡片内输入框，改为引导回一句话。

    为什么不在卡片里放输入框：飞书卡片输入框的值要等用户点「提交」才回传，
    而改写正文常常是整段重打，卡片里的小框体验比聊天框差得多；
    而且「点提交 → 校验 → 落盘 → 重绘卡」这条链比「回一句话」多三次失败点。
    """
    return ensure_button_behaviors({
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"修改第 {index} 条"},
            "template": "blue",
        },
        "body": {"elements": [
            {"tag": "markdown", "content": "**当前正文**"},
            {"tag": "markdown",
             "content": f"> {current.replace(chr(10), chr(10) + '> ')}"},
            {"tag": "hr"},
            {"tag": "markdown",
             "content": (f"直接回一句话，我把它作为第 {index} 条的新正文：\n"
                         f"`第{index}条改成：<你的文案>`")},
            {"tag": "column_set", "flex_mode": "flow", "columns": [
                {"tag": "column", "width": "auto", "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "算了不改"},
                    "type": "default",
                    "value": {NUDGE_KEY: ACTION_DROP, "bid": batch_id, "cancel_edit": True},
                }]},
            ]},
        ]},
    })


def build_result_card(body: str, ok: bool) -> Dict[str, Any]:
    """发送/取消之后的终态卡（无按钮：一次确认即终态，避免重复点）。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "催办结果"},
            "template": "green" if ok else "red",
        },
        "body": {"elements": [{"tag": "markdown", "content": body}]},
    }


# ── 回调解析 ──────────────────────────────────────────────────────

def parse_action(event: Any) -> Tuple[str, str, int, Dict[str, Any]]:
    """→ (action, batch_id, index, 原始 value)。非本卡返回空三元组。

    `index` 从 1 开始（与卡片上的「第 N 条」一致），整批操作时为 0。
    """
    value = _action_value(event)
    if not isinstance(value, dict) or not value.get(NUDGE_KEY):
        return "", "", 0, {}
    action = str(value.get(NUDGE_KEY) or "")
    bid = str(value.get("bid") or "")
    try:
        index = int(value.get("i") or 0)
    except (TypeError, ValueError):
        index = 0
    return action, bid, index, value


def is_nudge_action(event: Any) -> bool:
    action, _, _, _ = parse_action(event)
    return bool(action)


def _action_value(event: Any) -> Any:
    action = getattr(event, "action", None)
    return getattr(action, "value", None) if action is not None else None


def operator_of(event: Any) -> str:
    """点按钮的人 open_id。用于校验「只有草稿主人能操作」。"""
    operator = getattr(event, "operator", None)
    return str(getattr(operator, "open_id", "") or "") if operator is not None else ""


def message_id_of(event: Any) -> str:
    return str(getattr(event, "message_id", "") or "")
