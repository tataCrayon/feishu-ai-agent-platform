"""过程卡契约测试 — 思考与动作按到达顺序缝成一条，答案进同一张卡。

守的是这次的三个需求点（2026-11-08）：思考过程要真的来自 dsh 的 thought 帧、
过程与能力加载合成一段、整轮只发一条消息（答案不再单独发文本）。
"""

from __future__ import annotations

import json

from app.dsh.timeline import _MAX_BLOCKS, Timeline


def test_thought_and_actions_interleave_in_arrival_order():
    """时间序：调一次能力，跟一段当时的判断——不是两张清单各排一遍。"""
    t = Timeline()
    t.observe("thought", "先确认要回答的是两件事")
    t.observe("tool_start", "加载能力说明")
    t.observe("tool_done", "加载能力说明")
    t.observe("thought", "工单状态是卡住的那一步，先取它")
    kinds = [b.kind for b in t.blocks]
    assert kinds == ["thought", "action", "thought"]


def test_consecutive_thought_frames_merge_into_one_block():
    """同一段推理被切成几十帧；逐帧成块会让卡片变成碎句墙。"""
    t = Timeline()
    for chunk in ("先取工单，", "它的数据源不同，", "再拉资讯。"):
        t.observe("thought", chunk)
    assert len(t.blocks) == 1
    assert t.blocks[0].text == "先取工单，它的数据源不同，再拉资讯。"


def test_tool_started_and_done_occupy_one_line():
    """一次调用有 started/done 两帧，卡片上只该占一行。"""
    t = Timeline()
    t.observe("tool_start", "查看关注的工单")
    t.observe("tool_done", "查看关注的工单")
    assert sum(1 for b in t.blocks if b.kind == "action") == 1
    assert t.blocks[0].status == "done"


def test_failed_action_keeps_its_line_when_retried():
    """失败行不能被重试覆盖：用户要知道这一步曾经没成。"""
    t = Timeline()
    t.observe("agent_error", "获取市场信号（超时未响应）")
    t.observe("tool_start", "获取市场信号")
    assert [b.status for b in t.blocks] == ["failed", "running"]
    text = t.to_markdown()
    assert "❌ 获取市场信号（超时未响应）" in text


def test_timeline_keeps_only_the_recent_window():
    """块数超窗口丢最早：卡片放不下，且最近的推理才是用户在等的。"""
    t = Timeline()
    for i in range(_MAX_BLOCKS + 4):
        t.observe("tool_start", f"动作{i}")
        t.observe("tool_done", f"动作{i}")
    assert len(t.blocks) <= _MAX_BLOCKS
    assert t.blocks[-1].action == f"动作{_MAX_BLOCKS + 3}"


def test_thought_block_keeps_tail_when_too_long():
    """超长思考保留尾部：正在想的几句才是重点。"""
    t = Timeline()
    t.observe("thought", "开头" + "x" * 3000 + "结尾")
    assert t.to_markdown().endswith("结尾")


def test_answer_with_table_becomes_native_component():
    """飞书卡片 markdown 不吃 GFM 表格语法，管道表格要拆成原始 table 组件。"""
    from app.dsh.progress_card import build_done

    answer = (
        "**结论**\n\n"
        "| 任务 | 状态 |\n"
        "| --- | --- |\n"
        "| 建表 | 成功 |\n"
        "| 加索引 | 执行中 |\n\n"
        "尾巴说明"
    )
    timeline = Timeline()
    timeline.observe("thought", "先看两个任务的状态")
    timeline.observe("tool_start", "查看执行的工单")
    timeline.observe("tool_done", "查看执行的工单")
    timeline.observe("tool_start", "查看建表任务")
    timeline.observe("tool_done", "查看建表任务")
    card = build_done("看工单 5784", timeline, answer, task_id="t1")
    tags = _tags(card)
    assert "table" in tags
    assert "collapsible_panel" in tags
    # 表格不再以管道文本形式出现在 markdown 里
    assert all("| --- |" not in el.get("content", "") for el in _walk(card) if el.get("tag") == "markdown")
    assert "| --- |" not in str([el for el in _walk(card) if el.get("tag") == "markdown"])


def test_done_card_carries_feedback_buttons_not_a_second_message():
    """反馈按钮内嵌完成卡：不再额外发一张极简反馈卡（否则又变两条消息）。"""
    from app.dsh.progress_card import build_done

    timeline = Timeline()
    timeline.observe("thought", "想想")
    card = build_done("问题", timeline, "答案", task_id="t1")
    values = [el.get("value") for el in _walk(card) if el.get("tag") == "button"]
    assert [v.get("fb") for v in values] == ["up", "down"]


def test_header_shows_the_answer_not_the_brand():
    """头部是唯一必然被看到的位置，写结论比写 AgentMate 有用。"""
    from app.dsh.progress_card import build_done

    card = build_done("给我讲讲 AI 热点", Timeline(), "**工单 5784** 正在执行，1 个任务已完成。")
    assert "AgentMate" not in card["header"]["title"]["content"]
    assert "工单 5784" in card["header"]["title"]["content"]


def test_oversized_answer_is_flagged_for_text_fallback():
    from app.dsh.progress_card import _MAX_ANSWER_CHARS, answer_fits_card

    assert answer_fits_card("短答案")
    assert not answer_fits_card("x" * (_MAX_ANSWER_CHARS + 1))


def test_running_card_shows_only_the_current_step():
    """运行态只值「此刻」：整轮积累的过程铺开就是一屏读不完的推理。"""
    from app.dsh.progress_card import build_running

    timeline = Timeline()
    for i in range(20):
        timeline.observe("thought", f"第{i}段推理" + "细节" * 200)
        timeline.observe("tool_start", f"动作{i}")
        timeline.observe("tool_done", f"动作{i}")
    body = _panel_body(build_running("问题", timeline, 40.0))
    assert "动作19" in body
    assert "动作0 " not in body and "动作0\n" not in body
    assert len(body) < 900


def test_done_card_hides_a_one_step_process_panel():
    """1 步的「处理过程」既不解释任何东西，又占掉答案前面的位置。"""
    from app.dsh.progress_card import build_done

    t = Timeline()
    t.observe("tool_start", "发消息")
    t.observe("tool_done", "发消息")
    assert "collapsible_panel" not in _tags(build_done("发消息", t, "已发出"))

    t.observe("tool_start", "查成员")
    t.observe("tool_done", "查成员")
    assert "collapsible_panel" in _tags(build_done("发消息", t, "已发出"))


def test_gateway_forwards_thought_chunks():
    """dsh 的 thought 帧必须进进度回调——这是「真思考」的唯一来源。"""
    import inspect

    from app.dsh import gateway

    source = inspect.getsource(gateway.DshGateway.on_event)
    assert "TextChunkKind.THOUGHT" in source
    assert '"thought"' in source


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _tags(card) -> list[str]:
    return [el.get("tag", "") for el in _walk(card)]


def _panel_body(card) -> str:
    """拼出所有折叠面板里的正文（面板标题也算，action 行渲染在正文里）。"""
    parts: list[str] = []
    for el in _walk(card):
        if el.get("tag") == "collapsible_panel":
            for block in el.get("elements", []):
                if block.get("tag") == "markdown":
                    parts.append(block.get("content", ""))
    return "\n".join(parts)


def test_running_card_shows_eta_alongside_elapsed():
    """耗时行必须带预估，且预估只升不降——报准了用户会当承诺，报降了像倒退。"""
    from app.dsh.progress_card import build_running, _eta_text

    timeline = Timeline()
    timeline.observe("tool_start", "读文档")
    timeline.observe("tool_done", "读文档")
    card = json.dumps(build_running("问题", timeline, 12.0), ensure_ascii=False)
    assert "已处理 12s" in card
    assert "预估约 15s（还剩约 3s）" in card
    # 步骤越多，档位越高，且不低于已耗时
    assert "预估约 180s" in _eta_text(100.0, 3)
    assert _eta_text(400.0, 3).startswith("预估 ≥ 180s")
