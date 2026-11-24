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
    for name in ("gather_signals", "list_team_skills", "refresh_skill_catalog",
                 "list_tasks", "get_skill_status_board"):
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


def test_identity_bound_tools_reject_without_caller(monkeypatch) -> None:
    """定时任务写操作要建卡回消息，没有会话身份就做不成——必须拒绝而不是空跑。

    身份只认会话 env（main 按 chat 注入），**不接受模型在参数里口述**：
    否则模型可以替任意用户建任务。
    """
    monkeypatch.delenv("PROMATE_CALLER_SENDER_ID", raising=False)
    result = asyncio.run(caps.dispatch("list_tasks", {}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] in (True, False)      # 只读工具不受身份限制
    result = asyncio.run(caps.dispatch("create_scheduled_task",
                                       {"title": "日报", "interval_minutes": 60,
                                        "target_id": "oc_test_chat"}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False


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
