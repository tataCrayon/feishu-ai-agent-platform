"""通知卡工具的结构保证 — 模型只填纯文本，卡片结构由代码钉死。

守的是三条：非法参数在工具层被拒（不静默截断、不抛异常）、`<at>` 无法借卡片
@全体、署名取真实发送者而不是模型填的名字。
"""

from __future__ import annotations

import json

import pytest

from app.config import MCPConfig, McpServerConfig
from app.mcp import _common, feishu_server
from app.protocols.notify_card import CARD_MAX_BYTES, check_card_args


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    cfg = _common.app_config()
    monkeypatch.setattr(
        cfg, "mcp", MCPConfig(feishu_max_text_chars=3000, feishu=McpServerConfig()))
    monkeypatch.setenv("PROMATE_CALLER_SENDER_NAME", "张三")
    yield


@pytest.fixture
def sent_cards(monkeypatch) -> list[tuple[str, dict]]:
    cards: list[tuple[str, dict]] = []

    def _fake_send_card(chat_id: str, card: dict) -> str:
        cards.append((chat_id, card))
        return "om_card_1"

    monkeypatch.setattr(feishu_server, "send_card", _fake_send_card)
    return cards


def _body(out: str) -> dict:
    return json.loads(out)


def _texts(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


def test_illegal_target_rejected_before_rendering(sent_cards):
    body = _body(feishu_server.send_card_message("badchat", "标题", "正文"))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert sent_cards == []


@pytest.mark.parametrize("kwargs", [
    {"title": "", "body": "正文"},
    {"title": "标题", "body": ""},
    {"title": "长" * 61, "body": "正文"},
    {"title": "标题", "body": "长" * 2001},
    {"title": "标题", "body": "正文", "template": "rainbow"},
    {"title": "标题", "body": "正文", "fields": {"标签": "值"} | {f"k{i}": "v" for i in range(12)}},
])
def test_bad_args_rejected_with_actionable_reason(sent_cards, kwargs):
    body = _body(feishu_server.send_card_message("ou_x", **kwargs))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert "超过上限" in body["error"] or "非法" in body["error"] or "不能为空" in body["error"] \
        or "最多" in body["error"]
    assert sent_cards == []


def test_at_all_is_stripped_from_body_and_fields(sent_cards):
    """`<at>` 是 @所有人 的唯一入口：留着就能让一张卡片惊动整个群。"""
    body = _body(feishu_server.send_card_message(
        "ou_x", "同步", "进度如下 <at user_id=\"all\">所有人</at>",
        fields={"负责人": "<at id=all></at>张三"},
    ))
    assert body["ok"] is True
    text = _texts(sent_cards[0][1])
    assert "<at" not in text
    assert "所有人" not in text.split('"content": "进度如下 ')[0]
    assert "张三" in text


def test_card_header_marks_the_real_sender(sent_cards):
    """副标题写「来自某某的消息」：署名必须取真实发送者，不是模型填的名字。"""
    body = _body(feishu_server.send_card_message(
        "ou_x", "早上好", "今天先看发布单 5784", from_name="张三"))
    assert body["ok"] is True
    card = sent_cards[0][1]
    assert card["header"]["subtitle"]["content"] == "来自 张三 的消息"
    assert body["sender"] == "张三"


def test_sender_falls_back_to_model_value_without_env(monkeypatch, sent_cards):
    monkeypatch.delenv("PROMATE_CALLER_SENDER_NAME", raising=False)
    body = _body(feishu_server.send_card_message(
        "ou_x", "早上好", "正文", from_name="张三"))
    assert body["sender"] == "张三"
    assert sent_cards[0][1]["header"]["subtitle"]["content"] == "来自 张三 的消息"


def test_fields_become_separate_lines(sent_cards):
    body = _body(feishu_server.send_card_message(
        "oc_x", "任务提醒", "有 2 项待处理", fields={"单号": "5784", "截止": "今天 18:00"}))
    assert body["ok"] is True
    text = _texts(sent_cards[0][1])
    assert "**单号：** 5784" in text
    assert "**截止：** 今天 18:00" in text


def test_worst_case_legal_card_stays_under_the_size_limit(sent_cards):
    """字数上限已经保证体积安全：最坏合法输入也不该触发体积拒收。

    体积检查是**防线**不是常规路径——真触发时说明字数上限被放开了，
    那时这里会先红。
    """
    body = _body(feishu_server.send_card_message(
        "ou_x", "标" * 60, "正" * 2000,
        fields={f"字段{i}": "值" * 99 for i in range(12)}))
    assert body["ok"] is True
    assert len(_texts(sent_cards[0][1]).encode("utf-8")) < CARD_MAX_BYTES


def test_check_card_args_allows_the_happy_path():
    assert check_card_args("标题", "正文") == ""
