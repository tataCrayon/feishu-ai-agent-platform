"""webportal handler per-sender 授权触发单测（全 mock，无网络，不跑 LLM）。

覆盖 `_prefetch_approval` 的授权触发矩阵：外人无 token + 链接真实存在 →
直返授权块；固定身份/精确命中 → 维持现状；pending → 直返等待块。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.handlers import webportal_handler as wph
from app.config import RouterConfig
from app.domains.webportal import user_auth as ua

_LINK = "https://go.feishu.cn/approval/s/xxx1/"


def _run(coro):
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


def _entry(text: str) -> dict:
    return {"combined": text, "attachments": [], "ts": 0}


def _wire(monkeypatch, *, probe_id="7691216079699905484",
          cands=None, resolve_state=(None, "no_session"),
          fixed="ou_FIXED", detail=None, start_url="https://accounts.feishu.cn/device?user_code=X"):
    """装好 prefetch 依赖的 mock，返回调用记录。"""
    seen: dict = {"list_env": "unset", "detail_env": "unset", "start_auth": []}

    async def fake_probe(code):
        return {"code": code, "instance_id": probe_id,
                "user_id": "", "final_url": ""}

    async def fake_list(env_extra=None):
        seen["list_env"] = env_extra
        return cands or []

    async def fake_resolve(oid):
        return resolve_state

    async def fake_fixed():
        return fixed

    async def fake_start(oid):
        seen["start_auth"].append(oid)
        return start_url, "X"

    async def fake_fetch(code, env_extra=None):
        seen["detail_env"] = env_extra
        return detail

    async def fake_download(items):
        return []

    monkeypatch.setattr(wph, "probe_short_link", fake_probe)
    monkeypatch.setattr(wph, "list_my_approvals", fake_list)
    monkeypatch.setattr(wph, "fetch_instance_form", fake_fetch)
    monkeypatch.setattr(wph, "download_form_attachments", fake_download)
    monkeypatch.setattr(ua, "resolve", fake_resolve)
    monkeypatch.setattr(ua, "fixed_open_id", fake_fixed)
    monkeypatch.setattr(ua, "start_auth", fake_start)
    monkeypatch.setattr(ua, "session_url",
                        lambda oid: "https://accounts.feishu.cn/device?user_code=X")
    return seen


def test_foreign_sender_no_token_requests_auth(monkeypatch):
    """外人 + 无 token + 链接真实存在 + 池里定位不到 → 直返授权块。"""
    seen = _wire(monkeypatch, cands=[])
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"帮我处理这个审批 {_LINK} 流程编号 DATA2026093002"),
        "feishu:oc_1:ou_SENDER"))
    assert direct is True
    assert "accounts.feishu.cn/device" in text
    assert "🔐" in text
    assert seen["start_auth"] == ["ou_SENDER"]


def test_foreign_sender_l2_match_is_not_trusted(monkeypatch):
    """外人无 token 时 L2 唯一命中不采用（可能是固定身份池里别人的单）→ 授权。"""
    cands = [{"instance_code": "U-1", "task_id": "100", "topic": "1",
              "definition_name": "数据修改", "title": "", "initiator_name": "张三",
              "status": 1,
              "summaries": [{"key": "流程编号", "value": "DATA999"}]}]
    seen = _wire(monkeypatch, cands=cands)
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"看看 {_LINK}"), "feishu:oc_1:ou_SENDER"))
    assert direct is True and "🔐" in text
    assert seen["detail_env"] == "unset"  # 没去拉详情


def test_fixed_sender_keeps_old_behavior(monkeypatch):
    """固定登录身份自己发 → 一切照旧（空池引导，不请求授权）。"""
    _wire(monkeypatch, cands=[], fixed="ou_FIXED")
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"看看 {_LINK}"), "feishu:oc_1:ou_FIXED"))
    assert direct is False
    assert "未定位到" in text


def test_precise_l0_match_no_auth_needed(monkeypatch):
    """流程编号 L0 精确命中（外人无 token 也一样）→ 以固定身份直读，不请求授权。"""
    cands = [{"instance_code": "U-1", "task_id": "100", "topic": "2",
              "definition_name": "数据修改", "title": "", "initiator_name": "张三",
              "status": 1,
              "summaries": [{"key": "流程编号", "value": "DATA2026093002"}]}]
    detail = {"meta": {"definition_name": "数据修改", "status": "审批中",
                       "serial_number": "DATA2026093002", "start_time": "",
                       "instance_code": "U-1"},
              "text": "流程编号: DATA2026093002", "attachments": []}
    seen = _wire(monkeypatch, cands=cands, detail=detail)
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"处理 {_LINK} 流程编号 DATA2026093002"),
        "feishu:oc_1:ou_SENDER"))
    assert direct is False
    assert "DATA2026093002" in text
    assert seen["start_auth"] == []          # 未请求授权
    assert seen["detail_env"] is None        # 固定身份读详情


def test_sender_token_env_threaded_through(monkeypatch):
    """发送者已有 token → list/detail 都以发送者身份（env 注入）读。"""
    cands = [{"instance_code": "U-1", "task_id": "100", "topic": "2",
              "definition_name": "数据修改", "title": "", "initiator_name": "张三",
              "status": 1,
              "summaries": [{"key": "流程编号", "value": "DATA2026093002"}]}]
    detail = {"meta": {"definition_name": "数据修改", "status": "审批中",
                       "serial_number": "DATA2026093002", "start_time": "",
                       "instance_code": "U-1"},
              "text": "流程编号: DATA2026093002", "attachments": []}
    env = {"LARKSUITE_CLI_USER_ACCESS_TOKEN": "T", "LARKSUITE_CLI_APP_ID": "cli"}
    seen = _wire(monkeypatch, cands=cands, detail=detail,
                 resolve_state=(env, "ok"))
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"处理 {_LINK} 流程编号 DATA2026093002"),
        "feishu:oc_1:ou_SENDER"))
    assert direct is False and "DATA2026093002" in text
    assert seen["list_env"] == env
    assert seen["detail_env"] == env
    assert seen["start_auth"] == []


def test_pending_auth_direct_reply(monkeypatch):
    """授权进行中（用户重发消息）→ 直返等待提示，不进 LLM。"""
    _wire(monkeypatch, resolve_state=(None, "pending"))
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"帮我处理这个审批 {_LINK}"), "feishu:oc_1:ou_SENDER"))
    assert direct is True
    assert "⏳" in text and "accounts.feishu.cn/device" in text


def test_wrong_account_direct_reply(monkeypatch):
    """别人点了发送者的授权链接 → 直返换账号提示 + 新链接。"""
    _wire(monkeypatch, resolve_state=(None, "wrong_account"))
    h = wph.WebportalHandler(RouterConfig())
    text, direct = _run(h._prefetch_approval(
        _entry(f"帮我处理这个审批 {_LINK}"), "feishu:oc_1:ou_SENDER"))
    assert direct is True
    assert "⚠️" in text and "重新授权" in text


def test_run_with_message_direct_bypasses_llm(monkeypatch):
    """direct=True 的授权块原样返回，LLM 完全不跑。"""
    h = wph.WebportalHandler(RouterConfig())

    async def fake_pre(entry, session_key):
        return "AUTH-BLOCK", True

    async def boom(*args, **kwargs):
        raise AssertionError("LLM 不应被调用")

    monkeypatch.setattr(h, "_prefetch_approval", fake_pre)
    h.run = boom
    out = _run(h.run_with_message("看看 https://go.feishu.cn/approval/s/x/",
                                  "feishu:oc_1:ou_SENDER"))
    assert out == "AUTH-BLOCK"
