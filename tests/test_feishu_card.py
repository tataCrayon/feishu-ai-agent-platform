"""卡片按钮「补 behaviors」的出口护栏。

飞书卡片 JSON 2.0：按钮只写 `value` 不写 `behaviors=[{type: callback, value}]`，
**卡片照样发得出去**，点下去却没有任何回调——客户端提示「目标回调服务当前未在线」，
发送端一切正常、日志里也没有失败。1.0 的写法在 2.0 里不再触发回调，而发送接口
不校验这个字段，只有人点的时候才暴露。

所以两个发送出口都要过同一层垫片：
主线 `BotChannel.send_card`（SDK）与旁线 `feishu_send.send_card`（纯 HTTP，
dsh 的 MCP 子进程只有这一条，没有 channel 对象）。只补一条，就会有一类卡片的
按钮永远点不动——这正是「定时任务审批卡点了没反应」的成因。

定时任务的审批门已下线（卡片不再有按钮），此处改用完成卡上的反馈按钮做样本：
同样带 `value`、同样走两个出口，护栏目标不变。
"""

from __future__ import annotations

from typing import Any

from app.dsh.timeline import Timeline
from app.dsh.progress_card import build_done as build_progress_done
from app.protocols import feishu_send
from app.protocols.feishu_card import ensure_button_behaviors


def _buttons(card: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                out.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(card)
    return out


def _button_card() -> dict[str, Any]:
    return build_progress_done("问题", Timeline(), "答案", task_id="task-test")


def test_approval_buttons_gain_behaviors_and_keep_value() -> None:
    card = ensure_button_behaviors(_button_card())
    buttons = _buttons(card)
    assert buttons
    for btn in buttons:
        assert btn["behaviors"] == [{"type": "callback", "value": btn["value"]}]
        # 顶层 value 是 1.0 兼容兜底，回调侧两种包装都要能取到
        assert btn["value"]["fb_task"] == "task-test"


def test_existing_behaviors_without_value_are_filled_not_replaced() -> None:
    card = {"body": {"elements": [{
        "tag": "button", "value": {"cr": "CR-1", "act": "adopt"},
        "behaviors": [{"type": "callback"}],
    }]}}
    ensure_button_behaviors(card)
    assert card["body"]["elements"][0]["behaviors"][0]["value"] == {"cr": "CR-1", "act": "adopt"}


def test_buttons_without_value_are_left_alone() -> None:
    """url 按钮（只跳转）不该被塞一个 callback——飞书会把它当回调按钮。"""
    card = {"body": {"elements": [{"tag": "button", "url": "https://example.com"}]}}
    ensure_button_behaviors(card)
    assert "behaviors" not in card["body"]["elements"][0]


def test_nested_buttons_in_columns_are_fixed() -> None:
    """按钮常被包在 column_set 里（PRD 卡片就是），遍历必须下钻。"""
    card = {"body": {"elements": [{"tag": "column_set", "columns": [
        {"tag": "column", "elements": [{"tag": "button", "value": {"cr": "CR-2"}}]},
    ]}]}}
    ensure_button_behaviors(card)
    nested = card["body"]["elements"][0]["columns"][0]["elements"][0]
    assert nested["behaviors"][0]["value"] == {"cr": "CR-2"}


def test_http_send_card_patches_behaviors(monkeypatch) -> None:
    """dsh MCP 子进程只有这条出口：不补 behaviors，审批卡片按钮点不动。"""
    sent: dict[str, Any] = {}

    def _fake_send(chat_id: str, msg_type: str, content: dict) -> str:
        sent.update(chat_id=chat_id, msg_type=msg_type, content=content)
        return "om_x"

    monkeypatch.setattr(feishu_send, "_send", _fake_send)
    feishu_send.send_card("oc_test", _button_card())

    assert sent["msg_type"] == "interactive"
    for btn in _buttons(sent["content"]):
        assert btn["behaviors"][0]["type"] == "callback"


def test_patching_is_idempotent(monkeypatch) -> None:
    """补垫片必须幂等：同一份 card 重发/更新（审批卡片就是发完再 update）不能再包一层。"""
    sent: list[dict] = []

    def _fake_send(chat_id: str, msg_type: str, content: dict) -> dict:
        sent.append(content)
        return "om_x"

    monkeypatch.setattr(feishu_send, "_send", _fake_send)
    card = _button_card()
    feishu_send.send_card("oc_test", card)
    feishu_send.send_card("oc_test", card)

    for content in sent:
        for btn in _buttons(content):
            assert len(btn["behaviors"]) == 1
            assert btn["behaviors"][0]["value"] == btn["value"]
