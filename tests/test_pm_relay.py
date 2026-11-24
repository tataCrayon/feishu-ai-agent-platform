"""PM 私聊转主链 —— 「不归 PM 的问题」怎么交出去。

守四条：
1. RELAY 哨兵只在判定域外时出现，普通答复原样返回；
2. 转发成功就用主链答复，pm_run 不再自己编一句；
3. 主链没起 / 转发失败要给**明确边界说明**，不能静默丢消息；
4. relay 端点只认本机来源。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domains.pm import pm_agent, pm_daemon, pm_relay


@pytest.fixture
def pm_chat():
    return "oc_p2p", "ou_pm_boss"


def test_normal_answer_passes_through(monkeypatch, pm_chat):
    """PM 域内问题：本地答复，不碰转发。"""
    chat_id, sender = pm_chat
    monkeypatch.setattr(pm_daemon, "process_user_query", lambda t, s: "11.19 还剩 3 个工作日")

    called = []
    monkeypatch.setattr(pm_relay, "forward", lambda *a, **k: called.append(1) or "不该走到这")

    assert pm_daemon._answer_private("11.19 还剩几天", sender, chat_id) == "11.19 还剩 3 个工作日"
    assert called == []


def test_relay_marker_triggers_forward(monkeypatch, pm_chat):
    """模型回 `RELAY:` 前缀 → 剥掉哨兵后原样投给主链，用主链答复。"""
    chat_id, sender = pm_chat
    monkeypatch.setattr(
        pm_daemon, "process_user_query",
        lambda t, s: pm_agent.RELAY_PREFIX + "帮 rwa-agent 建个上线工单",
    )
    seen = {}

    def fake_forward(text, sender_id="", chat_id="", timeout=120.0):
        seen["text"] = text
        seen["sender"] = sender_id
        return "工单已建：#123"

    monkeypatch.setattr(pm_relay, "forward", fake_forward)

    out = pm_daemon._answer_private("帮 rwa-agent 建个上线工单", sender, chat_id)
    assert out == "工单已建：#123"
    assert seen["text"] == "帮 rwa-agent 建个上线工单"
    assert seen["sender"] == sender


def test_relay_failure_when_daemon_down_says_so(monkeypatch, pm_chat):
    """主链没跑：必须明说「转发不出去」，不能返回空串让用户以为消息丢了。"""
    chat_id, sender = pm_chat
    monkeypatch.setattr(pm_daemon, "process_user_query",
                        lambda t, s: pm_agent.RELAY_PREFIX + "查下 SIT 日志")
    monkeypatch.setattr(pm_relay, "forward", lambda *a, **k: None)
    monkeypatch.setattr(pm_relay, "available", lambda *a, **k: False)

    out = pm_daemon._answer_private("查下 SIT 日志", sender, chat_id)
    assert "没在跑" in out
    assert out.startswith("这件事不归 PM 管")


def test_relay_failure_when_timeout_says_so(monkeypatch, pm_chat):
    """主链活着但没答（超时）：区分于「没在跑」，给不同提示。"""
    chat_id, sender = pm_chat
    monkeypatch.setattr(pm_daemon, "process_user_query",
                        lambda t, s: pm_agent.RELAY_PREFIX + "查下 SIT 日志")
    monkeypatch.setattr(pm_relay, "forward", lambda *a, **k: None)
    monkeypatch.setattr(pm_relay, "available", lambda *a, **k: True)

    out = pm_daemon._answer_private("查下 SIT 日志", sender, chat_id)
    assert "没拿到答复" in out


def test_relay_endpoint_requires_loopback():
    """转发端点是完整主链权限，不能对本机之外开放。"""
    from app.api.pm_relay import relay_router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(relay_router)
    client = TestClient(app)

    # TestClient 默认来源是 testclient（非回环），应被拒
    assert client.get("/api/pm/relay/ping").status_code == 403


def test_relay_endpoint_rejects_when_dsh_disabled(monkeypatch):
    """dsh 没起来时返 503，而不是假装成功返回空答复。"""
    from app.api.pm_relay import relay_router
    from fastapi import FastAPI
    import app.runtime as runtime

    app = FastAPI()
    app.include_router(relay_router)
    client = TestClient(app)

    monkeypatch.setattr(runtime, "get_dsh_gateway", lambda: None)
    # 让来源看起来是回环
    monkeypatch.setattr("app.api.pm_relay._require_loopback", lambda r: None)

    resp = client.post("/api/pm/relay", json={"text": "hi", "sender_id": "ou_x"})
    assert resp.status_code == 503


def test_relay_endpoint_returns_gateway_reply(monkeypatch):
    """正常路径：网关答复原样进 `reply` 字段，pm_run 拿到就能发出去。"""
    from app.api.pm_relay import relay_router
    from fastapi import FastAPI
    import app.runtime as runtime

    class FakeGw:
        async def handle(self, message, chat_id="", on_event=None, media=None,
                         caller=None, memory=""):
            return f"答复：{message}"

    app = FastAPI()
    app.include_router(relay_router)
    client = TestClient(app)

    monkeypatch.setattr(runtime, "get_dsh_gateway", lambda: FakeGw())
    monkeypatch.setattr("app.api.pm_relay._require_loopback", lambda r: None)

    resp = client.post("/api/pm/relay", json={"text": "查下日志", "sender_id": "ou_x"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "答复：查下日志"
