"""PM 卡片回调的业务处理 —— 按钮点下去之后发生什么。

这一层**不认识飞书**：入参是一个事件 dict，出参是一句要回给点按者的话。
所有与飞书的往来（发卡片、更新卡片）由 `pm_card_bridge` 注入，这样单测
不需要真发消息。

三条硬规则
----------
1. **只有草稿主人能动**。草稿里是「以他的名义发给谁、说什么」，别人点到按钮
   不该能发出去。判据是 `operator.open_id == batch.sender`。
2. **只认当前批次**。用户可能翻出几天前的旧卡片，`batch_by_id` 对不上就明确
   说「这批已失效」，绝不去改当前草稿。
3. **发送动作走 `pm_nudge`**，这一层不自己拼发送逻辑。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from app.domains.pm import pm_nudge
from app.domains.pm import pm_nudge_card as card

logger = logging.getLogger("pm_hub.nudge_actions")

# 注入的飞书能力：发文本 / 发卡片（返回 message_id）/ 更新卡片
SendText = Callable[[str, str], None]
SendCard = Callable[[str, Dict[str, Any]], str]
UpdateCard = Callable[[str, Dict[str, Any]], bool]
# 单条发送的投递函数：给人看的结果文本 -> 是否成功
SendOne = Callable[[str], bool]


class NudgeCardActions:
    """处理催办卡片的按钮回调。"""

    def __init__(self, send_text: SendText, send_card: SendCard,
                 update_card: UpdateCard, send_one: Optional[SendOne] = None) -> None:
        self._send_text = send_text
        self._send_card = send_card
        self._update_card = update_card
        # 单条发送的实际投递。不传就退回生产实现（真发飞书），
        # 传了就是测试用——这是唯一能拦住「单测真给人发消息」的接缝。
        self._send_one = send_one

    # ── 事件入口 ──────────────────────────────────────────────

    def handle(self, event: Dict[str, Any]) -> str:
        """返回 toast 文案（空串表示不额外提示）。"""
        parsed = _parse_event(event)
        if parsed is None:
            return ""
        action, batch_id, index, value, operator, target = parsed

        batch = pm_nudge.batch_by_id(batch_id)
        if batch is None:
            return "这批催办已经处理过或已失效，什么都没有改。"
        if operator and operator != batch.sender:
            logger.warning("催办卡片被非本人点击：operator=%s owner=%s",
                           operator, batch.sender)
            return "这是别人的催办草稿，不能代发。"

        if action == card.ACTION_EDIT:
            return self._on_edit(batch, index, target)
        if action == card.ACTION_ONE_SEND:
            return self._on_send_one(batch, index, target)
        if action == card.ACTION_ONE_DROP:
            return self._on_drop_one(batch, index, target)
        if action == card.ACTION_SEND:
            return self._on_send_all(batch, target)
        if action == card.ACTION_DROP:
            return self._on_drop_all(batch, value, target)
        logger.info("未知催办动作 %s，忽略", action)
        return ""

    # ── 各动作 ────────────────────────────────────────────────

    def _on_edit(self, batch, index: int, target: str) -> str:
        if not (1 <= index <= len(batch.items)):
            return f"只有 {len(batch.items)} 条，没有第 {index} 条。"
        pm_nudge.set_editing(batch, index)
        current = batch.items[index - 1].body
        self._refresh(target, batch.message_id, card.build_ask_card(
            batch.batch_id, index, current))
        return "回一句话改这条"

    def _on_send_one(self, batch, index: int, target: str) -> str:
        text = pm_nudge.send_one(batch, index, self._send_one)
        logger.info("单条催办发送：%s", text[:80])
        self._refresh(target, batch.message_id, card.build_result_card(
            text, ok=text.startswith("发了")))
        # 结果卡已就地更新，不再另发文本；toast 只给一句短的
        return "已发送"

    def _on_drop_one(self, batch, index: int, target: str) -> str:
        text = pm_nudge.drop_one(batch, index)
        remaining = pm_nudge.get_batch(batch.sender)
        if remaining is None:
            self._refresh(target, batch.message_id,
                          card.build_result_card(text + "\n\n全部处理完了。", ok=True))
        else:
            self._refresh(target, batch.message_id,
                          card.build_nudge_card(remaining.items, remaining.batch_id))
        return "已跳过"

    def _on_send_all(self, batch, target: str) -> str:
        sender = batch.sender
        text = pm_nudge.commit(sender)
        logger.info("整批催办发送：%s", text[:80])
        self._refresh(target, batch.message_id, card.build_result_card(
            text, ok=text.startswith("发了")))
        return "已发送"

    def _on_drop_all(self, batch, value: Dict[str, Any], target: str) -> str:
        if value.get("cancel_edit"):
            # 「算了不改」：只是退出编辑态，不丢草稿
            pm_nudge.set_editing(batch, 0)
            self._refresh(target, batch.message_id,
                          card.build_nudge_card(batch.items, batch.batch_id))
            return "已取消修改"
        text = pm_nudge.drop_batch(batch.sender)
        self._refresh(target, batch.message_id, card.build_result_card(text, ok=True))
        return "已取消"

    # ── 卡片刷新 ──────────────────────────────────────────────

    def _refresh(self, target: str, message_id: str, new_card: Dict[str, Any]) -> None:
        """就地更新原卡片；更新失败就发一张新的。

        为什么允许退化成「发新卡」：`message_id` 可能取不到（发送返回体形态
        变化），而**结果必须让用户看到**——宁可多一条消息，也不能点了没反应。
        """
        if message_id and self._update_card(message_id, new_card):
            return
        try:
            self._send_card(target, new_card)
        except Exception:
            logger.exception("催办卡片刷新失败，回落文本")
            body = _card_plain_text(new_card)
            if body:
                self._send_text(target, body)


def _card_plain_text(card_dict: Dict[str, Any]) -> str:
    """把卡片里的 markdown 元素拼成纯文本兜底。"""
    parts = []
    body = card_dict.get("body") or {}
    for el in body.get("elements") or []:
        if isinstance(el, dict) and el.get("tag") == "markdown":
            parts.append(str(el.get("content") or ""))
    return "\n".join(p for p in parts if p)


def _parse_event(event: Dict[str, Any]):
    """把飞书卡片回调事件摊平成便于处理的元组。

    飞书的事件体有多种包装（`event.action.value` / `action.value`），
    这里两种都认——认不出返回 None，由调用方静默让过。
    """
    action_obj = event.get("action") or (event.get("event") or {}).get("action") or {}
    value = action_obj.get("value") if isinstance(action_obj, dict) else None
    if not isinstance(value, dict) or not value.get(card.NUDGE_KEY):
        return None

    operator_obj = event.get("operator") or (event.get("event") or {}).get("operator") or {}
    operator = str(operator_obj.get("open_id") or "") if isinstance(operator_obj, dict) else ""

    # 回哪个会话：优先 chat_id（群），其次 open_id（私聊）
    context = event.get("context") or (event.get("event") or {}).get("context") or {}
    target = ""
    if isinstance(context, dict):
        target = str(context.get("open_chat_id") or context.get("open_message_id") or "")
    if not target:
        target = str(event.get("open_chat_id") or "")

    return (
        str(value.get(card.NUDGE_KEY) or ""),
        str(value.get("bid") or ""),
        _as_int(value.get("i")),
        value,
        operator,
        target,
    )


def _as_int(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def make_actions(send_text: SendText, send_card: SendCard,
                 update_card: UpdateCard,
                 send_one: Optional[SendOne] = None) -> NudgeCardActions:
    return NudgeCardActions(send_text, send_card, update_card, send_one)
