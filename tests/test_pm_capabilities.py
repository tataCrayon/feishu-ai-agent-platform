"""PM 能力接口的结构保证 — 三条出口纪律 + 配置读得到。

守的是：字段名/单位不被改写、空值不被 omit、`last_at`（property）确实塞进去了、
未启用时是 503 而不是空数组、`[projects.pm]` 写在 `[[projects]]` 里能读到。
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import dashboard as dashboard_mod
from app.config import PmConfig, PmIdentity, PmOutputs
from app.domains.pm import pm_thread, pm_watch
from app.domains.pm.pm_config import pm_config


@pytest.fixture
def client() -> TestClient:
    return TestClient(dashboard_mod.dashboard_app)


@pytest.fixture
def enabled(monkeypatch) -> PmConfig:
    cfg = PmConfig(
        enabled=True,
        identity=PmIdentity(open_id="ou_pm", names=["李四", "李四"]),
        outputs=PmOutputs(spreadsheet_token="sht", sheet_id_versions="sht000"),
    )
    pm_config.cache_clear()
    monkeypatch.setattr("app.api.pm_capabilities.pm_config", lambda: cfg)
    yield cfg
    pm_config.cache_clear()


def test_disabled_returns_503_not_empty_list(client: TestClient, monkeypatch):
    """未启用回 503。空数组会被当成「今天真的没事」，那是错误信息。"""
    monkeypatch.setattr("app.api.pm_capabilities.pm_config", lambda: PmConfig(enabled=False))
    resp = client.get("/api/pm/threads")
    assert resp.status_code == 503
    assert "未启用" in resp.json()["detail"]


def test_threads_returns_raw_fields_not_rendered_string(client: TestClient, enabled, monkeypatch):
    """`/threads` 给结构，不给 `queue()` 渲染好的字符串。

    `business` 为空必须显式出现空串——空值是信息（「还没归」），不是缺数据。
    """
    sample = pm_thread.Thread(key="biz:1126", title="1126 提测", kind="demand",
                              business="", settled="", settled_at=0.0, snooze_until=0.0)
    monkeypatch.setattr(pm_thread, "threads_of", lambda day=None, days_back=1: [sample])

    body = client.get("/api/pm/threads").json()
    assert body["day"] == date.today().isoformat()
    row = body["threads"][0]
    assert row["key"] == "biz:1126"
    assert row["business"] == ""
    assert row["settled_at"] == 0.0
    # `last_at` 是 @property，asdict 拿不到——漏了它调用方无法按最近动静排序
    assert "last_at" in row


def test_thread_fields_are_not_renamed(client: TestClient, enabled, monkeypatch):
    """字段名是契约：`text`/`ts`/`who` 不改名，历史数据就是这个名字。"""
    ev = pm_watch.WatchEvent(
        chat_id="oc_1", chat_name="群", chat_mode="group", message_id="om_1",
        sender_id="ou_1", sender_name="张三", text="你过一眼", ts=1794313728766,
        reason="@我", replied=False, directed=True, context="",
    )
    monkeypatch.setattr(pm_watch.WatchState, "load", classmethod(lambda cls: pm_watch.WatchState()))
    monkeypatch.setattr(pm_watch, "poll", lambda *a, **kw: [ev])

    body = client.get("/api/pm/events?since=0").json()
    row = body["events"][0]
    assert set(row) == {f.name for f in fields(pm_watch.WatchEvent)}
    assert isinstance(row["ts"], int) and row["ts"] == 1794313728766
    assert body["next_since"] == 1794313728766


def test_events_next_since_is_max_ts(client: TestClient, enabled, monkeypatch):
    evs = [
        pm_watch.WatchEvent(chat_id="oc", chat_name="", chat_mode="group", message_id="om1",
                            sender_id="", sender_name="", text="", ts=100, reason=""),
        pm_watch.WatchEvent(chat_id="oc", chat_name="", chat_mode="group", message_id="om2",
                            sender_id="", sender_name="", text="", ts=900, reason=""),
    ]
    monkeypatch.setattr(pm_watch.WatchState, "load", classmethod(lambda cls: pm_watch.WatchState()))
    monkeypatch.setattr(pm_watch, "poll", lambda *a, **kw: evs)
    body = client.get("/api/pm/events?since=50").json()
    assert body["since"] == 50
    assert body["next_since"] == 900


def test_events_directed_is_a_field_not_a_reason_substring(client: TestClient, enabled, monkeypatch):
    """`directed` 是布尔字段。判据不能从 `reason` 文案反推——文案一改下游静默失效。"""
    ev = pm_watch.WatchEvent(chat_id="oc", chat_name="", chat_mode="p2p", message_id="om",
                             sender_id="", sender_name="", text="", ts=1,
                             reason="私聊里问我事", directed=False)
    monkeypatch.setattr(pm_watch.WatchState, "load", classmethod(lambda cls: pm_watch.WatchState()))
    monkeypatch.setattr(pm_watch, "poll", lambda *a, **kw: [ev])
    row = client.get("/api/pm/events").json()["events"][0]
    assert row["directed"] is False
    assert row["replied"] is False
    assert row["context"] == ""


def test_settle_rejects_illegal_action(client: TestClient, enabled):
    resp = client.post("/api/pm/threads/k/settle", json={"action": "delete"})
    assert resp.status_code == 400


def test_bad_day_is_400_not_silent_today(client: TestClient, enabled):
    """日期写错必须报错。静默回落「今天」会让调用方拿到一份别的时间线的数据。"""
    assert client.get("/api/pm/threads?day=2026/11/06").status_code == 400


def test_sheet_without_token_configured_is_503(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.api.pm_capabilities.pm_config",
                        lambda: PmConfig(enabled=True, outputs=PmOutputs()))
    assert client.get("/api/pm/sheet?range=sht000!A1").status_code == 503


def test_people_keeps_dept_key_when_missing(client: TestClient, enabled, monkeypatch):
    """`dept` 缺值也要出现（空串），不靠 omit 表达缺数据。"""
    monkeypatch.setattr("app.api.pm_capabilities.resolve_many", lambda ids: {})
    monkeypatch.setattr("app.api.pm_capabilities.name_of", lambda oid, fb="": "张三")
    body = client.get("/api/pm/people?ids=ou_a,ou_b").json()
    assert [p["dept"] for p in body["people"]] == ["", ""]
    assert [p["open_id"] for p in body["people"]] == ["ou_a", "ou_b"]


def test_card_send_requires_target(client: TestClient, enabled):
    resp = client.post("/api/pm/cards", json={"target": " ", "card": {}})
    assert resp.status_code == 400


def test_card_send_passes_payload_through(client: TestClient, enabled, monkeypatch):
    """卡片内容不校验、不加工：调用方给什么发什么。"""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr("app.protocols.feishu_send.send_card",
                        lambda t, c: (sent.append((t, c)), "om_x")[1])
    card = {"header": {"title": {"tag": "plain_text", "content": "hi"}}}
    body = client.post("/api/pm/cards", json={"target": "ou_x", "card": card}).json()
    assert body["message_id"] == "om_x"
    assert sent == [("ou_x", card)]


def test_patch_card_calls_update_not_resend(client: TestClient, enabled, monkeypatch):
    """就地更新：PATCH 掉原卡，不新发一条（重发会在聊天记录里堆废卡）。"""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("app.protocols.feishu_send.update_card",
                        lambda mid, c: (calls.append((mid, c)), True)[1])
    sent: list = []
    monkeypatch.setattr("app.protocols.feishu_send.send_card",
                        lambda t, c: sent.append(1) or "om_new")
    body = client.patch("/api/pm/cards/om_old", json={"elements": []}).json()
    assert body["updated"] is True
    assert calls == [("om_old", {"elements": []})]
    assert sent == []


def test_pm_config_defaults_are_off_and_empty():
    """默认全关、全空：读不到配置时不能假装能用。"""
    cfg = PmConfig()
    assert cfg.enabled is False
    assert cfg.identity.open_id == ""
    assert cfg.outputs.spreadsheet_token == ""
    assert cfg.rules.release.release_weekday == 4
    assert cfg.rules.release.ask_days_before == [8]


def test_pm_config_is_a_dataclass_field_on_app_config():
    """`AppConfig` 上必须有 pm 字段——缺了它 load_config 会静默走默认值。"""
    from app.config import AppConfig
    assert "pm" in {f.name for f in dataclasses.fields(AppConfig)}


def test_every_pm_route_is_registered(client: TestClient):
    """路由清单钉死：少一条就是「文件夹里有、接口上没有」，调用方只会看到 404。"""
    paths = {getattr(r, "path", "") for r in dashboard_mod.dashboard_app.routes}
    expected = {
        "/api/pm/events", "/api/pm/scan",
        "/api/pm/threads", "/api/pm/threads/{key}",
        "/api/pm/threads/{key}/settle", "/api/pm/backlog",
        "/api/pm/sheet", "/api/pm/records", "/api/pm/records/{record_id}",
        "/api/pm/radar", "/api/pm/people",
        "/api/pm/doc", "/api/pm/minutes", "/api/pm/minutes/{token}",
        "/api/pm/nudge-plan",
        "/api/pm/cards", "/api/pm/cards/{message_id}",
    }
    assert expected <= paths, f"缺路由：{sorted(expected - paths)}"


def test_radar_warnings_carry_workdays_left(monkeypatch):
    """`workdays_left` 必须是独立字段，不能只拼在 `msg` 句子里。

    下游要按天数排序/过滤时，对「…(剩3工作日)…」再正则一次就是在解析人话。
    """
    from datetime import date as _date

    from app.domains.pm.pm_hub_engine import StoryItem, evaluate_daily_radar

    stories = [StoryItem(
        version="11.26", line="私募基金", title="私募 SFC 26 号", dev_owner="王五",
        qa_owner="赵六", pm_owner="张三", target_date="2026-11-26",
        status="实现中", remark="",
    )]
    radar = evaluate_daily_radar(stories, today=_date(2026, 11, 19))
    highs = [w for w in radar["warnings"] if w["level"] == "HIGH"]
    assert highs, radar["warnings"]
    assert all("workdays_left" in w for w in radar["warnings"])
    assert highs[0]["workdays_left"] == 5


def test_settle_after_pm_switch_writes_to_config_open_id(monkeypatch):
    """`_ME_OPEN_ID` / `PM_OPEN_ID` 都是配置派生量：未配置时为空，不是某个真人的 ID。"""
    from app.domains.pm import pm_config as pmod
    from app.domains.pm.pm_config import pm_config as real

    assert pm_thread._ME_OPEN_ID == real().identity.open_id
    assert pmod.owner_name() == (real().identity.names or [""])[0]
