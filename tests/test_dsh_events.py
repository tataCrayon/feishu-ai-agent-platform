"""dsh 事件翻译的契约测试 — 用 Phase 0 实测录制的事件样本。

夹具是 ACP `session/update` 的真实形状（字段名/嵌套与 dsh 0.2.1-rc.2 一致），
经 `adapter` 的私有翻译函数转成内部事件后再断言 `events` 的纯函数输出；
这样协议改动会同时打红「协议解析」和「翻译」两处，而不是只红一边。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.dsh.adapter import _translate_update
from app.dsh.events import (
    MAX_PROGRESS_STEPS,
    CardStatus,
    ProgressCard,
    StopEvent,
    TextChunkKind,
    ToolCallEvent,
    ToolCallPhase,
    apply_event,
    new_card,
    to_span,
)

SESSION_ID = "5c1f5a1e-0000-4000-8000-000000000001"

# ── 录制样本（一次「列目录」任务的真实事件序列）──

NOTIFICATIONS: list[dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "messageId": "msg-1",
                "content": {"type": "text", "text": "先看看目录结构。"},
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-1",
                "title": "pwsh",
                "kind": "other",
                "status": "in_progress",
                "rawInput": {"command": "Get-ChildItem -Name"},
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-1",
                "status": "completed",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": "a.py\nb.py\n"},
                    }
                ],
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "msg-2",
                "content": {"type": "text", "text": "目录里有两个文件："},
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "msg-2",
                "content": {"type": "text", "text": "a.py 与 b.py。"},
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": {"sessionUpdate": "usage_update", "used": 12345, "size": 1000000},
        },
    },
]


def _events() -> list[Any]:
    out = []
    for notification in NOTIFICATIONS:
        event = _translate_update(SESSION_ID, notification["params"]["update"])
        assert event is not None
        out.append(event)
    return out


# ── 协议 → 内部事件 ──


def test_translate_thought_chunk_marks_thought_kind() -> None:
    event = _translate_update(
        SESSION_ID, NOTIFICATIONS[0]["params"]["update"]
    )
    assert event is not None
    assert event.kind is TextChunkKind.THOUGHT
    assert event.text == "先看看目录结构。"


def test_translate_tool_call_start_keeps_raw_input_as_json_text() -> None:
    event = _translate_update(SESSION_ID, NOTIFICATIONS[1]["params"]["update"])
    assert isinstance(event, ToolCallEvent)
    assert event.phase is ToolCallPhase.STARTED
    assert event.title == "pwsh"
    assert json.loads(event.raw_input) == {"command": "Get-ChildItem -Name"}


def test_translate_tool_result_extracts_nested_text() -> None:
    event = _translate_update(SESSION_ID, NOTIFICATIONS[2]["params"]["update"])
    assert isinstance(event, ToolCallEvent)
    assert event.phase is ToolCallPhase.UPDATED
    assert event.status == "completed"
    assert event.output_preview == "a.py\nb.py\n"


def test_translate_usage_update_reads_used_and_size() -> None:
    event = _translate_update(SESSION_ID, NOTIFICATIONS[5]["params"]["update"])
    assert event is not None
    assert event.used_tokens == 12345
    assert event.context_size == 1000000


def test_translate_unknown_update_returns_none() -> None:
    assert _translate_update(SESSION_ID, {"sessionUpdate": "config_option_update"}) is None


# ── 事件 → span ──


def test_tool_span_start_is_running_and_finish_is_success() -> None:
    started = to_span(_events()[1])
    finished = to_span(_events()[2])
    assert started is not None and finished is not None
    assert (started.span, started.status) == ("tool_call", "running")
    assert (finished.span, finished.status) == ("tool_call", "success")
    assert finished.tool_call_id == "call-1"


def test_tool_span_clips_long_raw_input() -> None:
    huge = {"command": "x" * 5000}
    event = _translate_update(
        SESSION_ID,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-9",
            "title": "pwsh",
            "status": "in_progress",
            "rawInput": huge,
        },
    )
    span = to_span(event)  # type: ignore[arg-type]
    assert span is not None
    assert len(span.attributes["raw_input"]) == 200
    assert span.attributes["raw_input"].endswith("…")


def test_text_chunk_produces_no_span() -> None:
    assert to_span(_events()[0]) is None
    assert to_span(_events()[3]) is None


def test_usage_span_carries_tokens_and_context_size() -> None:
    span = to_span(_events()[5])
    assert span is not None
    assert span.span == "usage"
    assert span.tokens == 12345
    assert span.attributes["context_size"] == "1000000"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("end_turn", "success"), ("cancelled", "cancelled"), ("max_tokens", "error")],
)
def test_stop_reason_maps_to_span_status(reason: str, expected: str) -> None:
    span = to_span(StopEvent(session_id=SESSION_ID, stop_reason=reason))
    assert span is not None
    assert (span.span, span.name, span.status) == ("turn", reason, expected)


# ── 事件 → 进度卡 ──


def test_full_turn_folds_into_card_with_answer_and_usage() -> None:
    card = new_card("task-1", "列出目录文件")
    for event in _events():
        card = apply_event(card, event)

    assert card.title == "列出目录文件"
    assert [step.title for step in card.steps] == ["pwsh"]
    assert card.steps[0].status == "success"
    assert card.answer_preview == "目录里有两个文件：a.py 与 b.py。"
    assert card.used_tokens == 12345
    assert card.status is CardStatus.RUNNING


def test_thought_chunks_never_reach_card_answer() -> None:
    card = apply_event(new_card("task-1", "t"), _events()[0])
    assert card.answer_preview == ""


def test_stop_turn_marks_card_done() -> None:
    card = apply_event(new_card("task-1", "t"), StopEvent(session_id=SESSION_ID))
    assert card.status is CardStatus.DONE
    assert card.stop_reason == "end_turn"


def test_cancelled_turn_marks_card_interrupted() -> None:
    card = apply_event(
        new_card("task-1", "t"),
        StopEvent(session_id=SESSION_ID, stop_reason="cancelled"),
    )
    assert card.status is CardStatus.INTERRUPTED


def test_update_before_start_still_creates_step() -> None:
    card = apply_event(
        new_card("task-1", "t"),
        ToolCallEvent(
            session_id=SESSION_ID,
            tool_call_id="call-x",
            title="skill",
            status="completed",
            phase=ToolCallPhase.UPDATED,
        ),
    )
    assert [step.tool_call_id for step in card.steps] == ["call-x"]
    assert card.steps[0].status == "success"


def test_more_than_max_steps_folds_oldest_into_counter() -> None:
    card = new_card("task-1", "t")
    total = MAX_PROGRESS_STEPS + 3
    for index in range(total):
        card = apply_event(
            card,
            ToolCallEvent(
                session_id=SESSION_ID,
                tool_call_id=f"call-{index}",
                title=f"tool-{index}",
                status="in_progress",
            ),
        )
    assert len(card.steps) == MAX_PROGRESS_STEPS
    assert card.omitted_steps == 3
    assert card.steps[-1].title == f"tool-{total - 1}"


def test_apply_event_is_pure_and_leaves_previous_card_untouched() -> None:
    before = new_card("task-1", "t")
    after = apply_event(
        before, StopEvent(session_id=SESSION_ID, stop_reason="cancelled")
    )
    assert before.status is CardStatus.RUNNING
    assert after is not before


def test_answer_preview_stops_growing_at_limit() -> None:
    card: ProgressCard = new_card("task-1", "t")
    chunk = {"type": "text", "text": "答" * 1000}
    for _ in range(10):
        event = _translate_update(
            SESSION_ID,
            {"sessionUpdate": "agent_message_chunk", "content": chunk},
        )
        card = apply_event(card, event)  # type: ignore[arg-type]
    assert len(card.answer_preview) <= 4000
