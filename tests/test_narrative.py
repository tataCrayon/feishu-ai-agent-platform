"""进度卡文案的契约测试 — 用户看到的是「在做什么」，不是工具名。

守的是反复出现的退化：`ToolCallEvent.title` 就是 dsh 的内部工具名
（`mcp__capabilities__gather_signals`），直接贴到进度卡上等于把调用栈给用户看。
这里钉住三件事：内部名被翻译、失败带可行动原因、同一动作不重复占行。
"""

from __future__ import annotations

from app.dsh.narrative import (
    _BY_TAIL,
    action_of,
    failure_hint,
)


def test_mcp_tool_names_are_translated_to_actions() -> None:
    assert action_of("mcp__capabilities__gather_signals") == "获取市场信号"
    assert action_of("mcp__capabilities__list_tasks") == "查看定时任务"
    # 前缀变了（换了 server 名）也按尾段命中，不要求逐个登记全名
    assert action_of("mcp__other__list_team_skills") == "读取团队能力目录"


def test_builtin_tools_fall_back_to_kind_then_name() -> None:
    assert action_of("read") == "读取资料"
    assert action_of("web_fetch") == "抓取网页/接口"
    # 没登记名字时按 kind 说人话
    assert action_of("unknown_tool", "execute") == "执行命令"
    # 连 kind 都没有就退回原名：宁可见到工具名，也不要编一个不存在的动作
    assert action_of("unknown_tool") == "unknown_tool"


def test_failure_hint_only_speaks_when_actionable() -> None:
    assert failure_hint("Error: Request timed out") == "超时未响应"
    assert failure_hint("YFRateLimitError: Too Many Requests") == "被数据源限流"
    # 认不出就不加噪音（不能把无关报错翻译成「超时」）
    assert failure_hint("some unexpected failure") == ""


def test_same_action_wording_across_states() -> None:
    """进行中与失败共用同一措辞：换句会让用户以为换了一件事。"""
    busy = action_of("mcp__capabilities__gather_signals")
    failed = action_of("mcp__capabilities__gather_signals")
    assert busy == failed == "获取市场信号"


def test_card_merges_repeated_tool_frames() -> None:
    """一次调用有 started/completed 两帧，卡片上只能占一行。"""
    import app.main as m

    events: list[str] = []
    m._upsert_tool_line(events, "获取市场信号", "🔎 获取市场信号")
    m._upsert_tool_line(events, "获取市场信号", "· 获取市场信号 ✓")
    assert events == ["· 获取市场信号 ✓"]


def test_card_keeps_error_line_when_retrying() -> None:
    """失败行不能被重试的 started 覆盖——用户要知道这一步曾经没成。"""
    import app.main as m

    events = ["🔎 获取市场信号"]
    m._upsert_tool_line(events, "获取市场信号", "❌ 获取市场信号（超时未响应）")
    m._upsert_tool_line(events, "获取市场信号", "🔎 获取市场信号")
    assert events == ["❌ 获取市场信号（超时未响应）", "🔎 获取市场信号"]


def test_card_does_not_duplicate_steps_beyond_window() -> None:
    """超出可见窗口的步骤不能再追加一行：卡片上会出现两个同名动作。

    时间线裁剪若只丢列表前端、第一帧却因为「已经看不见了」被当成新动作，
    「加载能力说明」就会在同一个窗口里出现两次——用户在重复步骤里找不出进展。
    """
    from app.dsh.timeline import Timeline

    timeline = Timeline()
    for i in range(20):
        timeline.observe("tool_start", "读取资料")
        timeline.observe("tool_done", "读取资料")
    assert all(
        block.action.count("读取资料") == 1
        for block in timeline.blocks
        if block.kind == "action"
    )
    assert len(timeline.blocks) == 1



def test_all_registered_tool_labels_are_non_empty() -> None:
    assert all(label.strip() for label in _BY_TAIL.values())


def test_common_tools_have_a_curated_label() -> None:
    """常用工具必须有专属文案：落到尾段/kind 兜底就等于把工具名还给用户。

    `mcp__capabilities__refresh_skill_catalog` 曾经没登记，卡片上显示的是工具名；
    `mcp__domains__daily_report` 更糟——尾段 `daily_report` 被当成一般名字透出。
    """
    from app.dsh.narrative import _TOOLS

    for name in (
        "mcp__capabilities__refresh_skill_catalog",
        "mcp__capabilities__get_skill_status",
        "mcp__capabilities__list_tasks",
        "mcp__domains__daily_report",
        "mcp__feishu__read_conversation",
        "skill",
    ):
        assert name in _TOOLS, f"{name} 缺进度文案"
        assert "_" not in _TOOLS[name], f"{name} 的文案像工具名：{_TOOLS[name]}"
