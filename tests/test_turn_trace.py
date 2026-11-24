"""会话轨迹的契约测试 — 「问什么 / 怎么处理 / 答了什么」必须留得下来。

守的是一个静默失效：工具步骤只活在内存的 `_Turn` 里，一轮结束就随会话丢弃。
一旦忘了在这条链路上取一次，事后就没有任何证据能回答「它当时为什么这么答」——
不报错，只是永远查不到。所以这里钉住三段：采集（gateway）、落盘（recorder）、
衔接（task_record 同时写档案与轨迹）。
"""

from __future__ import annotations

import json

from app.dsh.events import ToolCallEvent, ToolCallPhase
from app.dsh.gateway import _Turn
from app.observability.turn_trace import (
    TurnStep,
    TurnTrace,
    TurnTraceRecorder,
)


def _tool(call_id: str, title: str = "", status: str = "in_progress",
          raw_input: str = "", output: str = "") -> ToolCallEvent:
    return ToolCallEvent(
        session_id="s1", tool_call_id=call_id, title=title, status=status,
        phase=ToolCallPhase.UPDATED if output else ToolCallPhase.STARTED,
        raw_input=raw_input, output_preview=output,
    )


def test_turn_merges_started_and_updated_into_one_step() -> None:
    """同一次调用分 started（带入参）与 updated（带结果）两帧，必须合成一条。"""
    turn = _Turn()
    turn.track_tool(_tool("c1", "skill", raw_input='{"name":"aihot"}'))
    turn.track_tool(_tool("c1", "skill", status="completed", output="加载成功"))

    steps = turn.step_list()
    assert len(steps) == 1
    assert steps[0]["name"] == "skill"
    assert steps[0]["status"] == "completed"
    assert "aihot" in steps[0]["input"]
    assert steps[0]["output"] == "加载成功"


def test_turn_keeps_call_order_across_tools() -> None:
    """顺序即处理顺序——复盘要看「先查会话再读文档」这类序列。"""
    turn = _Turn()
    for i, name in enumerate(("read_conversation", "read_doc", "send_message")):
        turn.track_tool(_tool(f"c{i}", name))
    assert [s["name"] for s in turn.step_list()] == [
        "read_conversation", "read_doc", "send_message"
    ]


def test_recorder_writes_markdown_and_index(tmp_path) -> None:
    rec = TurnTraceRecorder(log_dir=tmp_path)
    trace = TurnTrace(
        task_id="t1", query="今天 AI 有什么新闻", chat_id="oc_x",
        sender_id="ou_1", sender_name="王", reply="答复正文",
        memory="用户偏好：简短",
        steps=[TurnStep(name="skill", status="completed",
                        input='{"name":"aihot"}', output="ok")],
        duration_ms=1200.0, status="success",
    )
    rec.write(trace)

    md_files = list(tmp_path.rglob("t1.md"))
    assert len(md_files) == 1
    text = md_files[0].read_text(encoding="utf-8")
    # 三段都要在：问什么 / 怎么处理 / 答什么
    assert "今天 AI 有什么新闻" in text
    assert "skill" in text and '"name":"aihot"' in text
    assert "答复正文" in text
    assert "用户偏好：简短" in text

    rows = [json.loads(line) for line in
            (tmp_path / f"{trace.ts[:7]}.index.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    assert rows[0]["tools"] == ["skill"]
    assert rows[0]["steps"] == 1
    assert rows[0]["status"] == "success"


def test_recorder_search_filters_by_tool_and_status(tmp_path) -> None:
    rec = TurnTraceRecorder(log_dir=tmp_path)
    rec.write(TurnTrace(task_id="a", query="查状态",
                        steps=[TurnStep(name="mcp__capabilities__process_order")],
                        status="success"))
    rec.write(TurnTrace(task_id="b", query="闲聊", steps=[], status="success"))

    hits = rec.search(days=1, tool="process_order")
    assert [h["task_id"] for h in hits] == ["a"]
    assert [h["task_id"] for h in rec.search(days=1)] == ["b", "a"]  # 新→旧


def test_recorder_never_raises_on_write_failure(tmp_path) -> None:
    """轨迹是观测面：目录不可写不能让用户少拿一条回复。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    TurnTraceRecorder(log_dir=blocker).write(TurnTrace(task_id="t", query="q"))


def test_long_tool_output_is_clipped_with_length_note() -> None:
    """单次工具输出留 4000 字上限，截断要写清原长，不然读的人以为就这么多。"""
    from app.observability.turn_trace import _render_markdown

    trace = TurnTrace(
        task_id="t2", query="q", reply="r",
        steps=[TurnStep(name="web_search", output="x" * 9000)],
    )
    text = _render_markdown(trace)
    assert "截断，原长 9000 字" in text
    assert len(text) < 9000


def test_prune_drops_only_days_outside_retention(tmp_path) -> None:
    """轨迹按天目录保留；过期目录整删，保留期内一个不动。"""
    rec = TurnTraceRecorder(log_dir=tmp_path)
    old_day = tmp_path / "2020-01-01"
    old_day.mkdir()
    (old_day / "x.md").write_text("old", encoding="utf-8")
    rec.write(TurnTrace(task_id="new", query="q"))

    removed = rec.prune(keep_days=30)
    assert removed == 1
    assert not old_day.exists()
    assert list(tmp_path.rglob("new.md")), "保留期内的轨迹被误删"
