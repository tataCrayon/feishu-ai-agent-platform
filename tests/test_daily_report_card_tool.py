"""daily_report_card 的工具层契约 —— 日报必须作为卡片投递，且失败可降级。

守三条：
1. 卡片发到**当前会话**（身份取自 env，不取模型参数）；
2. 发不出去时回执里带 `push_text`，模型能用原文兜底，用户不至于空手；
3. 对端没有当日成品时 `available=false`，不拿别的日期顶替。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.domains.daily import aihot
from app.domains.daily import store as daily_store
from app.mcp import domains_server


@pytest.fixture
def report() -> dict:
    return {
        "date": "2026-11-20",
        "links": {"aihot": "https://aihot.news/daily/2026-11-20"},
        "lead": {"leadParagraph": "开盘价高出发行价 33%。"},
        "sections": [{
            "label": "行业动态",
            "items": [{"title": "Anthropic 登陆纳斯达克",
                       "links": {"aihot": "https://aihot.news/items/b"}}],
        }],
        "flashes": [],
    }


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_store, "DEFAULT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(aihot, "_PAIR_CACHE", {})
    return tmp_path


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, dict]]:
    cards: list[tuple[str, dict]] = []
    monkeypatch.setattr(domains_server, "send_card",
                        lambda chat_id, card: cards.append((chat_id, card)) or "om_daily")
    monkeypatch.setenv("PROMATE_CALLER_CHAT_ID", "ou_me")
    return cards


def _body(out: str) -> dict:
    return json.loads(out)


def test_card_sent_to_current_session(report, cache_dir, sent, monkeypatch):
    async def fake_report(date_iso: str = ""):
        return report

    monkeypatch.setattr(domains_server, "get_daily_report", fake_report)
    body = _body(asyncio.run(domains_server.daily_report_card()))
    assert body["ok"] and body["sent"] and body["date"] == "2026-11-20"
    assert sent[0][0] == "ou_me"
    body_text = json.dumps(sent[0][1], ensure_ascii=False)
    assert "[Anthropic 登陆纳斯达克](https://aihot.news/items/b)" in body_text


def test_send_failure_falls_back_to_push_text(report, cache_dir, monkeypatch):
    def boom(chat_id: str, card: dict):
        from app.protocols.feishu_send import FeishuSendError

        raise FeishuSendError("no permission")

    monkeypatch.setattr(domains_server, "send_card", boom)
    monkeypatch.setenv("PROMATE_CALLER_CHAT_ID", "ou_me")

    async def fake_report(date_iso: str = ""):
        return report

    monkeypatch.setattr(domains_server, "get_daily_report", fake_report)
    body = _body(asyncio.run(domains_server.daily_report_card()))
    assert body["ok"] and body["sent"] is False
    assert "no permission" in body["error"]
    assert "Anthropic 登陆纳斯达克" in body["push_text"]


def test_missing_chat_id_does_not_fake_success(report, cache_dir, sent, monkeypatch):
    monkeypatch.delenv("PROMATE_CALLER_CHAT_ID", raising=False)

    async def fake_report(date_iso: str = ""):
        return report

    monkeypatch.setattr(domains_server, "get_daily_report", fake_report)
    body = _body(asyncio.run(domains_server.daily_report_card()))
    assert body["sent"] is False and sent == []


def test_no_report_available_is_reported_honestly(cache_dir, sent, monkeypatch):
    async def none_report(date_iso: str = ""):
        return None

    monkeypatch.setattr(domains_server, "get_daily_report", none_report)
    body = _body(asyncio.run(domains_server.daily_report_card("2026-11-19")))
    assert body["ok"] and body["available"] is False and sent == []
