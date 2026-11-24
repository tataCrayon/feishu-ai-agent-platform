"""user_auth 状态机单测（全 mock，无网络）：发起/轮询/绑定/刷新/env 注入。

安全：测试只碰内存态（_sessions/_tokens），进出各清一次；无真实凭据。
"""
from __future__ import annotations

import time

import pytest

from app.domains.webportal import user_auth as ua


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_state():
    ua._sessions.clear()
    ua._tokens.clear()
    ua._fixed = (0.0, None)
    yield
    ua._sessions.clear()
    ua._tokens.clear()
    ua._fixed = (0.0, None)


_DEVICE_RESP = {
    "device_code": "dc-1",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://accounts.feishu.cn/device",
    "verification_uri_complete":
        "https://accounts.feishu.cn/device?user_code=ABCD-EFGH",
    "expires_in": 600,
    "interval": 5,
}


def _install_device(monkeypatch, calls: list | None = None):
    async def fake_post(url, payload):
        if calls is not None:
            calls.append(url)
        if "device_authorization" in url:
            return 200, dict(_DEVICE_RESP)
        return 400, {"error": "authorization_pending", "code": 20094}

    monkeypatch.setattr(ua, "_post", fake_post)


# ── session_key 解析 ─────────────────────────────────────────────

def test_session_ids_parse():
    assert ua.session_ids("feishu:oc_abc:ou_xyz") == ("ou_xyz", "oc_abc")
    assert ua.session_ids("oc_only") == ("", "")
    assert ua.session_ids("") == ("", "")
    assert ua.session_ids("feishu:oc_only") == ("", "")


# ── 发起 ─────────────────────────────────────────────────────────

def test_start_auth_success_and_reuse(monkeypatch):
    calls: list = []
    _install_device(monkeypatch, calls)
    url, code = _run(ua.start_auth("ou_a"))
    assert "user_code=ABCD-EFGH" in url and code == "ABCD-EFGH"
    url2, _ = _run(ua.start_auth("ou_a"))
    assert url2 == url
    assert len(calls) == 1  # 进行中会话复用，不重复发设备码


def test_start_auth_failure_returns_empty(monkeypatch):
    async def fake_post(url, payload):
        return 400, {"error": "invalid_client"}

    monkeypatch.setattr(ua, "_post", fake_post)
    assert _run(ua.start_auth("ou_a")) == ("", "")


# ── 轮询/完成 ────────────────────────────────────────────────────

def test_resolve_pending(monkeypatch):
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))
    env, state = _run(ua.resolve("ou_a"))
    assert env is None and state == "pending"


def test_resolve_authorized_env_injection(monkeypatch):
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))

    async def fake_post(url, payload):
        if "device_authorization" in url:
            return 200, dict(_DEVICE_RESP)
        assert payload["grant_type"].endswith("device_code")
        return 200, {"access_token": "AT", "refresh_token": "RT",
                     "expires_in": 7200}

    async def fake_who(access):
        assert access == "AT"
        return "ou_a"

    monkeypatch.setattr(ua, "_post", fake_post)
    monkeypatch.setattr(ua, "who_open_id", fake_who)
    env, state = _run(ua.resolve("ou_a"))
    assert state == "ok"
    assert env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "AT"
    assert env["LARKSUITE_CLI_APP_ID"]
    # 会话已清；token 有效期内二次 resolve 不打网络
    env2, state2 = _run(ua.resolve("ou_a"))
    assert state2 == "ok" and env2["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "AT"


def test_resolve_wrong_account_binds_real_owner(monkeypatch):
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))

    async def fake_post(url, payload):
        if "device_authorization" in url:
            return 200, dict(_DEVICE_RESP)
        return 200, {"access_token": "AT", "refresh_token": "RT",
                     "expires_in": 7200}

    async def fake_who(access):
        return "ou_other"  # 别人点了发送者的授权链接

    monkeypatch.setattr(ua, "_post", fake_post)
    monkeypatch.setattr(ua, "who_open_id", fake_who)
    env, state = _run(ua.resolve("ou_a"))
    assert env is None and state == "wrong_account"
    # token 按真实授权者入库：授权者本人可用，发送者仍无
    assert ua.token_env_sync("ou_other") is not None
    assert ua.token_env_sync("ou_a") is None


def test_resolve_session_expired(monkeypatch):
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))
    ua._sessions["ou_a"].expires_at = time.time() - 1
    env, state = _run(ua.resolve("ou_a"))
    assert env is None and state == "expired"
    assert "ou_a" not in ua._sessions  # 已清，重发消息会重新发起


def test_resolve_no_session(monkeypatch):
    async def boom(url, payload):
        raise AssertionError("无会话不应打 token 端点")

    monkeypatch.setattr(ua, "_post", boom)
    assert _run(ua.resolve("ou_a")) == (None, "no_session")
    assert _run(ua.resolve("")) == (None, "no_session")


def test_resolve_device_code_denied(monkeypatch):
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))

    async def fake_post(url, payload):
        if "device_authorization" in url:
            return 200, dict(_DEVICE_RESP)
        return 400, {"error": "access_denied"}

    monkeypatch.setattr(ua, "_post", fake_post)
    env, state = _run(ua.resolve("ou_a"))
    assert env is None and state == "expired"


# ── refresh ──────────────────────────────────────────────────────

def test_resolve_refreshes_expired_token(monkeypatch):
    ua._tokens["ou_a"] = ua._Token("AT-old", "RT", time.time() - 10)

    async def fake_post(url, payload):
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "RT"
        return 200, {"access_token": "AT-new", "refresh_token": "RT2",
                     "expires_in": 7200}

    monkeypatch.setattr(ua, "_post", fake_post)
    env, state = _run(ua.resolve("ou_a"))
    assert state == "ok"
    assert env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "AT-new"
    assert ua._tokens["ou_a"].refresh == "RT2"  # 轮转后的新 refresh


def test_resolve_refresh_failure_falls_to_no_session(monkeypatch):
    ua._tokens["ou_a"] = ua._Token("AT-old", "RT", time.time() - 10)

    async def fake_post(url, payload):
        return 400, {"error": "invalid_grant"}

    monkeypatch.setattr(ua, "_post", fake_post)
    env, state = _run(ua.resolve("ou_a"))
    assert env is None and state == "no_session"
    assert "ou_a" not in ua._tokens  # 坏 token 已清，可重新授权


# ── 同步读路径 ───────────────────────────────────────────────────

def test_token_env_sync_valid_and_expired():
    assert ua.token_env_sync("ou_a") is None
    ua._tokens["ou_a"] = ua._Token("AT", "RT", time.time() + 3600)
    env = ua.token_env_sync("ou_a")
    assert env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "AT"
    ua._tokens["ou_a"].expires_at = time.time() - 1
    assert ua.token_env_sync("ou_a") is None  # 过期 → None（refresh 归 resolve）


def test_session_url_returns_live_link_only(monkeypatch):
    assert ua.session_url("ou_a") == ""
    _install_device(monkeypatch)
    _run(ua.start_auth("ou_a"))
    assert "user_code=ABCD" in ua.session_url("ou_a")
    ua._sessions["ou_a"].expires_at = time.time() - 1
    assert ua.session_url("ou_a") == ""
