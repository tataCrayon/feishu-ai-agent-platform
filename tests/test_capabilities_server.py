"""能力投影 MCP server 的契约测试 — 工具面与身份边界。

守两件事：

1. **能力必须有手**：Handler 的工具定义要原样导出成 MCP 工具，schema 不能被
   重新推导（参数名/枚举/自由字典一变，模型就会传错参数）。这层漏了不会报错，
   只会让模型说「本会话没有这个执行入口」——dsh 迁移后第一轮实测就是这个表现。
2. **身份不能由模型填**：订阅/关注类工具只认会话 env 里的发送者；没有身份时
   必须拒绝，而不是按模型口述的用户名去绑。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.mcp import capabilities_server as caps


def _tools() -> dict[str, object]:
    return {t.name: t for t in asyncio.run(caps.list_tools())}


def test_handler_tools_are_projected_with_original_schema() -> None:
    tools = _tools()
    # 每个能力至少一个执行入口（名字取 Handler 的 tools 声明，不在这里重复维护）
    for name in ("gather_signals", "cmdb_watch_status", "list_team_skills",
                 "process_order", "list_tasks"):
        assert name in tools, f"{name} 没有投影成 MCP 工具"


def test_free_form_dict_parameter_survives_projection() -> None:
    """digital-oracle 的 signals 是自由字典（additionalProperties）。

    FastMCP 的签名推导会把它压成「未知参数」或丢掉 additionalProperties，
    模型于是无法传任意信号名——所以这里走低层 Server 原样透传。
    """
    schema = _tools()["gather_signals"].inputSchema
    signals = schema["properties"]["signals"]
    assert signals.get("type") == "object"
    assert "additionalProperties" in signals


def test_schedule_write_tools_are_projected_with_approval_gate() -> None:
    """定时任务写操作要投影：执行走审批卡，权限门在服务侧，不在「有没有消息对象」。"""
    tools = _tools()
    for name in ("create_scheduled_task", "pause_task", "resume_task", "cancel_task"):
        assert name in tools


def test_card_button_bound_tool_is_not_projected() -> None:
    """PRD 整理要等卡片按钮回调，脱离飞书链路无法完成确认，所以不投影。"""
    assert "flush_prd_watch" not in _tools()


def test_refused_tool_explains_where_to_go() -> None:
    result = asyncio.run(caps.dispatch("flush_prd_watch", {}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    # 只说「不支持」没用，要告诉用户下一步在哪做
    assert "飞书" in payload["error"]


def test_identity_bound_tool_requires_caller(monkeypatch) -> None:
    """没有注入身份时，订阅类工具必须拒绝——绝不按模型口述的名字去绑。"""
    monkeypatch.delenv("PROMATE_CALLER_SENDER_ID", raising=False)
    result = asyncio.run(caps.dispatch("subscribe_cmdb_watch", {"cmdb_user": "someone"}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert "身份" in payload["error"]


def test_identity_bound_tool_passes_with_caller(monkeypatch, tmp_path) -> None:
    """有身份时走通：订阅落到 state 文件（用临时路径，不碰真实档案）。"""
    from app.config import load_config

    cfg = load_config()
    cfg.cmdb_watch.state_path = str(tmp_path / "state.json")
    monkeypatch.setattr(caps, "_config", cfg)
    monkeypatch.setattr(caps, "_registry", None)
    monkeypatch.setattr(caps, "_load_error", "")
    monkeypatch.setenv("PROMATE_CALLER_SENDER_ID", "ou_test_caller")
    monkeypatch.setenv("PROMATE_CALLER_CHAT_ID", "oc_test_chat")
    monkeypatch.setenv("PROMATE_CALLER_SENDER_NAME", "测试用户")

    result = asyncio.run(caps.dispatch("subscribe_cmdb_watch", {"cmdb_user": "devuser"}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert "devuser" in payload.get("reply", "")


def test_unknown_tool_is_reported_not_silently_empty() -> None:
    result = asyncio.run(caps.dispatch("no_such_tool", {}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False and "no_such_tool" in payload["error"]


def test_registry_failure_does_not_raise(monkeypatch) -> None:
    """装配失败只能降级，不能抛：MCP 起不来会让整个 session/new 被拒。"""
    monkeypatch.setattr(caps, "_registry", None)
    monkeypatch.setattr(caps, "_load_error", "boom")

    def _explode(_cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(caps, "load_config", _explode)
    try:
        assert caps.registry() is None
    finally:
        caps._load_error = ""


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个用例后重建 registry：用例间不能串配置与 watcher。"""
    yield
    caps._registry = None
    caps._load_error = ""
    caps._config = None
