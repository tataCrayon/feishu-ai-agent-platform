"""webportal_handler ↔ A2A 客户端单测（全 mock，无网络、不跑 LLM）。

覆盖 A2A 壳的三条契约：
1. `_a2a_send` 的协议解析（COMPLETED 取产物 / FAILED 抛错 / 非 JSON 产物 / 服务不可达）
2. `process_order` 的附件携带与阻断语义（附件读不到要降级成文本，不拖垮整单）
3. `fetch_approval` 的定位→表单→附件闭环（附件必须并进聚合缓冲，随工单出单）
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agents.handlers import webportal_handler as wph
from app.config import RouterConfig


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def handler():
    return wph.WebportalHandler(RouterConfig())


class _Resp:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload


def _patch_post(monkeypatch, resp_factory):
    """替换 `_a2a_send` 内部用的 httpx.AsyncClient.post。"""
    seen: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        seen.update({"url": url, "body": json, "headers": headers})
        out = resp_factory(seen)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return seen


def _completed(payload: dict) -> _Resp:
    return _Resp(200, {"task": {
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
    }})


# ── _a2a_send 协议层 ─────────────────────────────────────────────

def test_a2a_send_returns_artifact_payload(handler, monkeypatch):
    seen = _patch_post(monkeypatch, lambda _s: _completed({"advisory": False, "sql": "SELECT 1"}))
    payload = _run(handler._a2a_send([{"text": "处理工单"}]))
    assert payload["sql"] == "SELECT 1"
    assert seen["url"].endswith("/message:send")
    assert seen["body"]["message"]["parts"] == [{"text": "处理工单"}]
    assert seen["body"]["message"]["messageId"]     # 每次调用唯一，服务端据此去重


def test_a2a_send_raises_on_failed_task(handler, monkeypatch):
    _patch_post(monkeypatch, lambda _s: _Resp(200, {"task": {
        "status": {"state": "TASK_STATE_FAILED",
                   "message": {"parts": [{"text": "引擎阻断：缺案例"}]}},
    }}))
    with pytest.raises(RuntimeError) as exc:
        _run(handler._a2a_send([{"text": "x"}]))
    assert "TASK_STATE_FAILED" in str(exc.value) and "缺案例" in str(exc.value)


def test_a2a_send_rejects_non_json_artifact(handler, monkeypatch):
    _patch_post(monkeypatch, lambda _s: _Resp(200, {"task": {
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "not-json"}]}],
    }}))
    with pytest.raises(RuntimeError) as exc:
        _run(handler._a2a_send([{"text": "x"}]))
    assert "不是 JSON" in str(exc.value)


def test_a2a_send_reports_unreachable_service(handler, monkeypatch):
    _patch_post(monkeypatch, lambda _s: httpx.ConnectError("refused"))
    with pytest.raises(RuntimeError) as exc:
        _run(handler._a2a_send([{"text": "x"}]))
    assert "不可达" in str(exc.value) and "a2a_server" in str(exc.value)


def test_a2a_send_accepts_direct_message_response(handler, monkeypatch):
    """无 task（同步直答形态）时取 message.parts 文本。"""
    _patch_post(monkeypatch, lambda _s: _Resp(200, {"message": {
        "parts": [{"text": json.dumps({"advisory": True, "sql": "SELECT 2"})}]}}))
    assert _run(handler._a2a_send([{"text": "x"}]))["sql"] == "SELECT 2"


# ── process_order：附件携带 / 阻断 ───────────────────────────────

def test_process_order_carries_attachment_as_file_part(handler, monkeypatch):
    import tempfile

    src = Path(tempfile.mkdtemp()) / "客户申请.pdf"
    src.write_bytes(b"%PDF-1.4 demo")
    seen = _patch_post(monkeypatch, lambda _s: _completed(
        {"advisory": False, "case_id": "c1", "case_title": "销户", "sql": "SELECT 1",
         "risk": "medium", "review": []}))
    token = wph._pending_attachments.set(
        [{"filename": src.name, "file_path": str(src)}])
    try:
        r = _run(handler.execute_tool("process_order", {"ticket_text": "处理这个工单"}))
    finally:
        wph._pending_attachments.reset(token)
    assert r.success, r.error
    parts = seen["body"]["message"]["parts"]
    assert parts[0] == {"text": "处理这个工单"}
    assert parts[1]["filename"] == src.name
    assert parts[1]["mediaType"] == "application/pdf"
    assert parts[1]["raw"]           # base64 文件内容随消息走
    assert "命中案例" in r.data["reply"] and "SELECT 1" in r.data["reply"]


def test_process_order_unreadable_attachment_degrades_to_text(handler, monkeypatch):
    seen = _patch_post(monkeypatch, lambda _s: _completed({"sql": "SELECT 1", "advisory": True}))
    token = wph._pending_attachments.set(
        [{"filename": "gone.pdf", "file_path": "D:/no/such/file.pdf"}])
    try:
        r = _run(handler.execute_tool("process_order", {"ticket_text": "处理"}))
    finally:
        wph._pending_attachments.reset(token)
    assert r.success, r.error
    assert "读取失败" in seen["body"]["message"]["parts"][1]["text"]


def test_process_order_blocked_payload_is_error(handler, monkeypatch):
    _patch_post(monkeypatch, lambda _s: _completed({"blocked": True, "message": "缺工单号"}))
    r = _run(handler.execute_tool("process_order", {"ticket_text": "处理"}))
    assert not r.success and "处理被阻断" in r.error and "缺工单号" in r.error


def test_process_order_empty_ticket_is_rejected_locally(handler):
    """空文本 + 空附件：本地就拒，不白跑一次 A2A。"""
    r = _run(handler.execute_tool("process_order", {"ticket_text": "  "}))
    assert not r.success and "为空" in r.error


# ── fetch_approval：定位 → 表单 → 附件入缓冲 ──────────────────────

_FORM = {
    "meta": {"definition_name": "星路-数据提取/修改申请", "status": "已通过",
             "serial_number": "202610020012", "start_time": "", "instance_code": "U-1"},
    "text": "流程编号: DATA2026100201\n主题: 客户销户",
    "attachments": [{"name": "申请.pdf", "url": "https://x/1.pdf"}],
}


def _wire_approval(monkeypatch, *, loc_level="L1", cands=None, detail=_FORM,
                   sender="ou_SELF", fixed="ou_SELF"):
    from app.domains.webportal import approval_link as al
    from app.domains.webportal import user_auth as ua

    calls: dict = {"downloads": []}

    async def fake_probe(code):
        return {"code": code, "instance_id": "7691940269697322270",
                "user_id": "", "final_url": ""}

    async def fake_list(env_extra=None):
        return cands if cands is not None else [
            {"instance_code": "U-1", "task_id": "100", "topic": "2",
             "definition_name": "星路", "title": "", "initiator_name": "张三",
             "status": 2, "summaries": []}]

    async def fake_resolve_by_ids(ids, candidates, env_extra=None, **kw):
        return al.LocateResult("matched", candidates[0], [candidates[0]],
                               "短链实例 ID 创建毫秒对齐", loc_level)

    async def fake_fetch(code, env_extra=None):
        return detail

    async def fake_download(items):
        calls["downloads"].append(items)
        return [{"filename": "申请.pdf", "file_path": "D:/tmp/申请.pdf"}]

    def fake_session_ids(key):        # 同步函数：handler 里直接取元组
        return sender, ""

    async def fake_fixed():
        return fixed

    monkeypatch.setattr(wph, "probe_short_link", fake_probe)
    monkeypatch.setattr(wph, "list_my_approvals", fake_list)
    monkeypatch.setattr(wph, "resolve_by_instance_ids", fake_resolve_by_ids)
    monkeypatch.setattr(wph, "fetch_instance_form", fake_fetch)
    monkeypatch.setattr(wph, "download_form_attachments", fake_download)
    monkeypatch.setattr(ua, "session_ids", fake_session_ids)
    monkeypatch.setattr(ua, "fixed_open_id", fake_fixed)
    return calls


def test_fetch_approval_returns_form_and_carries_attachments(handler, monkeypatch):
    calls = _wire_approval(monkeypatch)
    buffer: list[dict] = []
    token = wph._pending_attachments.set(buffer)
    session_token = wph._current_session_key.set("feishu:oc_1:ou_SELF")
    try:
        r = _run(handler.execute_tool(
            "fetch_approval", {"url": "https://go.feishu.cn/approval/s/iXqq4KFo/"}))
    finally:
        wph._pending_attachments.reset(token)
        wph._current_session_key.reset(session_token)
    assert r.success, r.error
    assert "审批单已读取" in r.data["reply"]
    assert "DATA2026100201" in r.data["approval_form"]
    assert r.data["located"] == "短链实例 ID 创建毫秒对齐"
    # 附件必须并进聚合缓冲，否则下一步 process_order 拿不到表单附件
    assert buffer == [{"filename": "申请.pdf", "file_path": "D:/tmp/申请.pdf"}]
    assert calls["downloads"]


def test_fetch_approval_without_link_asks_for_clue(handler):
    r = _run(handler.execute_tool("fetch_approval", {"url": "https://example.com/x"}))
    assert r.success
    assert "未识别到" in r.data["reply"] and "流程编号" in r.data["reply"]


def test_fetch_approval_unlocated_gives_guidance(handler, monkeypatch):
    from app.domains.webportal import user_auth as ua

    async def fake_probe(code):
        return {"code": code, "instance_id": "", "user_id": "", "final_url": ""}

    async def fake_list(env_extra=None):
        return []

    def fake_session_ids(key):
        return "", ""

    monkeypatch.setattr(wph, "probe_short_link", fake_probe)
    monkeypatch.setattr(wph, "list_my_approvals", fake_list)
    monkeypatch.setattr(ua, "session_ids", fake_session_ids)
    r = _run(handler.execute_tool(
        "fetch_approval", {"url": "https://go.feishu.cn/approval/s/zzz999/"}))
    assert r.success and "没能定位" in r.data["reply"]


# ── 直达内核：capabilities MCP 必须投影出工单工具 ─────────────────

def test_capabilities_projects_webportal_tools():
    """dsh 会话里的 `mcp__capabilities__process_order` 等入口由这里派生。

    不投影 = 能力只剩描述，模型会说「本会话没有这个执行入口」（实测过的形态）。
    """
    from app.mcp import capabilities_server as cs

    names = {str((t.get("function") or {}).get("name"))
             for _, t in cs.tool_specs()}
    assert {"process_order", "fetch_approval", "list_cases"} <= names
    # schema 原样透传：模型看到的参数就是 Handler 自己期望的参数
    spec = next(t for _, t in cs.tool_specs()
                if (t.get("function") or {}).get("name") == "fetch_approval")
    assert spec["function"]["parameters"]["required"] == ["url"]
