"""后台续期策略单测 — 默认绝不登录 CMDB，零网络、零真实凭证。

跑法：python -m pytest tests/domains/cmdb_watch/test_session_policy.py -q

为什么单独测：CMDB 是单会话登录，用本人账号（`~/.cmdb.conf`）在后台登录一次，
就把用户浏览器里那条会话顶掉。这件事在日志上的表现只是「已重新登录」四个字，
**看不出有人被踢下线**，所以必须用测试把「默认不登录」钉死——否则哪天有人
觉得「自动续期更省心」把开关改成默认 true，问题会以「浏览器老是掉线」的形式
飘到用户那边，而不是在这里失败。

覆盖：三态开关、bot 专属凭证的优先级、登录后探活失败不落盘、
「没续上就返回空串（调用方据此降级 id 探测）」。
"""

from __future__ import annotations

import sys

import pytest

from app.domains.cmdb_watch import client as cmdb_client
from app.domains.cmdb_watch.client import (
    CmdbClient,
    relogin_mode,
    renew_session_if_stale,
    using_bot_account,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_USER_CONF = "CMDB_USER=devuser\nCMDB_PASS=user-pass\n"
_BOT_CONF = "CMDB_USER=feishu-agent-platform\nCMDB_PASS=bot-pass\n"


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    """把所有落盘路径与网络调用换成假的：本用例组绝不碰真实凭证/会话。"""
    session_file = tmp_path / ".cmdb-session"
    user_conf = tmp_path / ".cmdb.conf"
    bot_conf = tmp_path / ".cmdb-bot.conf"
    user_conf.write_text(_USER_CONF, encoding="utf-8")

    monkeypatch.setattr(cmdb_client, "SESSION_FILE", session_file)
    monkeypatch.setattr(cmdb_client, "CREDENTIALS_FILE", user_conf)
    monkeypatch.setattr(cmdb_client, "BOT_CREDENTIALS_FILE", bot_conf)
    for key in ("CMDB_ALLOW_RELOGIN", "CMDB_SESSION_ID", "CMDB_USER", "CMDB_PASS"):
        monkeypatch.delenv(key, raising=False)
    # 让位/自登录状态是进程级的，用例之间必须复位，否则前一条的痕迹会污染后一条
    monkeypatch.setattr(cmdb_client, "_self_login_sid", "")
    monkeypatch.setattr(cmdb_client, "_self_login_at", 0.0)
    monkeypatch.setattr(cmdb_client, "_yield_until", 0.0)

    calls: dict[str, list] = {"login": [], "valid": []}

    def fake_probe(session_id: str, base_url: str = "", timeout: int = 8) -> bool:
        calls["valid"].append(session_id)
        return session_id.startswith("fresh")

    def fake_post(url: str, payload: dict, timeout: int):
        calls["login"].append(payload["username"])
        return {"result": True}, "sessionid=fresh1234567890abcdef; Path=/"

    monkeypatch.setattr(cmdb_client, "session_valid", fake_probe)
    monkeypatch.setattr(cmdb_client, "_post_json", fake_post)

    return {"session": session_file, "bot_conf": bot_conf,
            "user_conf": user_conf, "calls": calls, "tmp": tmp_path}


def test_mode_defaults_to_confirm(monkeypatch):
    """不配开关时必须落在 confirm —— 这是「不顶掉浏览器」的唯一保障。"""
    monkeypatch.delenv("CMDB_ALLOW_RELOGIN", raising=False)
    assert relogin_mode() == "confirm"
    for raw, want in (("true", "auto"), ("false", "never"), ("CONFIRM", "confirm"),
                      ("yes", "confirm"), ("", "confirm")):
        monkeypatch.setenv("CMDB_ALLOW_RELOGIN", raw)
        assert relogin_mode() == want, raw


def test_confirm_mode_never_calls_login(fake_env):
    """默认模式下即使会话失效也一次都不调登录接口，且返回空串让调用方降级。"""
    fake_env["session"].write_text("stale-session-0000000", encoding="utf-8")
    renewed = renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    assert renewed == ""
    assert fake_env["calls"]["login"] == []
    # 也不该把旧的失效值当「续期成功」还回去
    assert fake_env["session"].read_text(encoding="utf-8").strip() == "stale-session-0000000"


def test_never_mode_is_silent(fake_env, monkeypatch, caplog):
    """false 模式同样不登录，但连告警都不打（纯后台托管场合不刷日志）。"""
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "false")
    with caplog.at_level("WARNING", logger="cmdb_watch.client"):
        renewed = renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    assert renewed == ""
    assert fake_env["calls"]["login"] == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_auto_mode_logs_in_and_persists(fake_env, monkeypatch):
    """true 模式才真的登录；探活通过后写盘并返回新会话。"""
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    renewed = renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    assert renewed == "fresh1234567890abcdef"
    assert fake_env["calls"]["login"] == ["devuser"]
    assert fake_env["session"].read_text(encoding="utf-8").strip() == renewed


def test_valid_session_is_reused_without_login(fake_env):
    """会话还有效时三种模式都不该登录（模式只决定「失效后」的行为）。"""
    fake_env["session"].write_text("fresh-existing-000000", encoding="utf-8")
    assert renew_session_if_stale("fresh-existing-000000", "http://cmdb.test") == "fresh-existing-000000"
    assert fake_env["calls"]["login"] == []


def test_login_that_probes_dead_is_not_persisted(fake_env, monkeypatch):
    """登录接口说成功、但新会话立刻判未登录 → 抛错且不覆盖文件。

    实测踩过：登录成功后被同账号在浏览器里的登录顶掉，文件里留下一条坏会话，
    下一个使用者（skill / MCP）拿着它一路报 UmUser，排查很久才发现源头在这。
    """
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    monkeypatch.setattr(cmdb_client, "session_valid", lambda *a, **k: False)
    fake_env["session"].write_text("keep-me-0000000000", encoding="utf-8")
    out = renew_session_if_stale("keep-me-0000000000", "http://cmdb.test")
    assert out == ""
    assert fake_env["session"].read_text(encoding="utf-8").strip() == "keep-me-0000000000"


def test_bot_credentials_take_precedence(fake_env):
    """配了独立服务账号就必须用它，绝不用本人的 ~/.cmdb.conf。"""
    assert not using_bot_account()
    fake_env["bot_conf"].write_text(_BOT_CONF, encoding="utf-8")
    assert using_bot_account()
    assert cmdb_client._read_credentials() == ("feishu-agent-platform", "bot-pass")


def test_missing_bot_conf_falls_back_to_user_conf(fake_env):
    assert cmdb_client._read_credentials() == ("devuser", "user-pass")


def test_client_renew_session_reports_false_when_not_renewed(fake_env):
    """客户端层的布尔值要能反映「没续上」，watcher 靠它决定是否重试列表接口。"""
    fake_env["session"].write_text("stale-session-0000000", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="stale-session-0000000")
    assert client.renew_session() is False
    assert client.session_id == "stale-session-0000000"     # 内存里的值不被清空


def test_client_renew_session_adopts_new_session(fake_env, monkeypatch):
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    fake_env["session"].write_text("stale-session-0000000", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="stale-session-0000000")
    assert client.renew_session() is True
    assert client.session_id == "fresh1234567890abcdef"


def test_client_adopts_manually_synced_session(fake_env):
    """人工把浏览器 sid 写进文件后，常驻进程必须采用它，而不是先判失效再干等。

    没有这条，「给最新 session 就不该重新登录」只在进程重启后成立；常驻进程里
    内存还是旧值，会把新会话当不存在。
    """
    fake_env["session"].write_text("fresh-manual-sync-0", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="stale-session-0000000")
    assert client.renew_session() is True
    assert client.session_id == "fresh-manual-sync-0"
    assert fake_env["calls"]["login"] == []


def test_client_ignores_unchanged_file(fake_env, monkeypatch):
    """文件没变（还是手里这条）时不能谎报「续期成功」，否则 watcher 会白重试一次。"""
    fake_env["session"].write_text("stale-session-0000000", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="stale-session-0000000")
    assert client.renew_session() is False
    assert fake_env["calls"]["login"] == []


def test_client_ignores_empty_file(fake_env):
    """文件被清空不等于「有新会话」，不该把内存里的值也清掉。"""
    fake_env["session"].write_text("", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="keep-me-0000000000")
    assert client.renew_session() is False
    assert client.session_id == "keep-me-0000000000"


def test_client_prefers_file_while_own_session_still_valid(fake_env):
    """手里这条还有效，但文件里已有更新的 → 先采用文件，绝不先登录。

    真实踩到的顺序：bot 启动身边会话已过期 → 探活失败 → 登录成功并写盘；几秒内
    用户又把浏览器 sid 同步进文件。若只在「失效后」才读文件，就会在两者都有效时
    优先用自己登的那条，把用户刚给的那条丢掉，接着一登录又把浏览器顶掉。
    """
    fake_env["session"].write_text("fresh-manual-sync-0", encoding="utf-8")
    client = CmdbClient(base_url="http://cmdb.test", session_id="fresh-old-but-valid-1")
    assert client.renew_session() is True
    assert client.session_id == "fresh-manual-sync-0"
    assert fake_env["calls"]["login"] == []


# ── auto 模式下的人机互踢自保 ────────────────────────────────


def test_auto_mode_yields_after_being_kicked(fake_env, monkeypatch):
    """后台刚登的那条被人顶掉 → 立刻让位，不再登录，等人工/服务账号接手。

    这条是「人机互踢」的唯一刹车：没有它，auto 模式就是每 120s 抢一次，
    用户侧的体感正是「浏览器老是掉线」。
    """
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    # 第一轮：失效 → 登录成功，记下「这是我登的」
    first = renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    assert first == "fresh1234567890abcdef"
    assert fake_env["calls"]["login"] == ["devuser"]

    # 人在浏览器里登录把 bot 顶掉：文件里仍是 bot 那条，但探活已失效
    monkeypatch.setattr(cmdb_client, "session_valid", lambda *a, **k: False)
    second = renew_session_if_stale(first, "http://cmdb.test")
    assert second == ""
    assert fake_env["calls"]["login"] == ["devuser"]      # 没有第二次登录

    # 让位期内再失效也不登
    third = renew_session_if_stale(first, "http://cmdb.test")
    assert third == ""
    assert fake_env["calls"]["login"] == ["devuser"]


def test_manual_sync_is_not_treated_as_kick(fake_env, monkeypatch):
    """人工同步了新 sid（文件被改写）后失效 → 是正常交接，不该进入让位期。"""
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    fake_env["session"].write_text("fresh-manual-sync-0", encoding="utf-8")
    # 人工同步的那条也失效了，但登录接口本身是好的（新登的能过探活）
    monkeypatch.setattr(cmdb_client, "session_valid", lambda sid, *a, **k: sid.startswith("fresh1"))

    assert renew_session_if_stale("fresh-manual-sync-0", "http://cmdb.test") == "fresh1234567890abcdef"
    assert cmdb_client._yield_until == 0.0


def test_yield_expires_and_login_resumes(fake_env, monkeypatch):
    """让位是临时的：过期后 auto 模式仍会登录（否则等于把开关偷改成 confirm）。"""
    monkeypatch.setenv("CMDB_ALLOW_RELOGIN", "true")
    renew_session_if_stale("stale-session-0000000", "http://cmdb.test")
    monkeypatch.setattr(cmdb_client, "session_valid", lambda *a, **k: False)
    assert renew_session_if_stale("fresh1234567890abcdef", "http://cmdb.test") == ""
    assert cmdb_client._yield_until > 0

    monkeypatch.setattr(cmdb_client, "_yield_until", 0.0)      # 模拟让位期已过
    monkeypatch.setattr(cmdb_client, "_self_login_at", 0.0)    # 且已超出被顶判定窗口
    monkeypatch.setattr(cmdb_client, "session_valid", lambda sid, *a, **k: sid.startswith("fresh1"))
    assert renew_session_if_stale("fresh1234567890abcdef", "http://cmdb.test") == "fresh1234567890abcdef"
