"""飞书 MCP 工具的三红线单测 — 非法目标/超长文本在工具层硬拒，合法输入走 mock。

不发真实请求：`send_text` / `reply_text` 被替换，断言的是「工具层决定了什么」，
而不是飞书的网络行为。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import MCPConfig, McpServerConfig
from app.mcp import _common, feishu_server

_LIMIT = 50


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """钉住配置，避免单测读到开发机 config.toml 里的真实上限。"""
    cfg = _common.app_config()
    monkeypatch.setattr(
        cfg, "mcp", MCPConfig(feishu_max_text_chars=_LIMIT, feishu=McpServerConfig()))
    yield


def _mock_backend(monkeypatch) -> list[tuple[str, str]]:
    """替换发送后端，返回记录列表（断言「真的发了什么」）。"""
    sent: list[tuple[str, str]] = []

    def _fake_send(chat_id: str, text: str) -> str:
        sent.append((chat_id, text))
        return "om_sent_1"

    def _fake_reply(message_id: str, text: str) -> str:
        sent.append((message_id, text))
        return "om_reply_1"

    monkeypatch.setattr(feishu_server, "send_text", _fake_send)
    monkeypatch.setattr(feishu_server, "reply_text", _fake_reply)
    return sent


def _body(out: str) -> dict:
    return json.loads(out)


def test_send_message_illegal_chat_id_rejects(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.send_message("badchat", "hi"))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert "oc_" in body["error"] and "ou_" in body["error"]
    assert sent == []


def test_send_message_empty_text_rejects(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.send_message("oc_abc", "   "))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert sent == []


def test_send_message_over_limit_rejects(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.send_message("oc_abc", "x" * (_LIMIT + 1)))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert "超过上限" in body["error"]
    assert sent == []


def test_send_message_at_limit_goes_through(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.send_message("oc_abc", "x" * _LIMIT))
    assert body == {"ok": True, "message_id": "om_sent_1"}
    assert sent == [("oc_abc", "x" * _LIMIT)]


def test_send_message_valid_returns_message_id(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.send_message("ou_user1", "构建完成"))
    assert body == {"ok": True, "message_id": "om_sent_1"}
    assert sent == [("ou_user1", "构建完成")]


def test_send_message_backend_failure_returns_error(monkeypatch):
    def _boom(chat_id: str, text: str) -> str:
        raise feishu_server.FeishuSendError("获取 tenant_access_token 失败: code=10003")

    monkeypatch.setattr(feishu_server, "send_text", _boom)
    body = _body(feishu_server.send_message("oc_abc", "hi"))
    assert body["ok"] is False
    assert "发送失败" in body["error"]


def test_reply_message_illegal_message_id_rejects(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.reply_message("abc", "hi"))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
    assert "om_" in body["error"]
    assert sent == []


def test_reply_message_over_limit_rejects(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.reply_message("om_m1", "y" * (_LIMIT + 1)))
    assert body["ok"] is False
    assert sent == []


def test_reply_message_valid_returns_message_id(monkeypatch):
    sent = _mock_backend(monkeypatch)
    body = _body(feishu_server.reply_message("om_m1", "收到"))
    assert body == {"ok": True, "message_id": "om_reply_1"}
    assert sent == [("om_m1", "收到")]


def test_read_doc_non_feishu_url_rejects():
    body = _body(asyncio.run(feishu_server.read_doc("https://evil.example.com/doc")))
    assert body["ok"] is False
    assert body["error"].startswith("REJECT:")
