"""反馈卡与负反馈归类的契约测试。

两块守的东西不同：
1. 卡片层——按钮必须能被我们自己的 handler 认领（卡片回调广播给所有 handler，
   认错就是点一下触发别人家的逻辑），且补 behaviors 后仍可点。
2. 归类层——"哪类问题效果不好"要能从档案聚合出来；按钮与文字两条来源
   合到同一字段，任一来源判负即负（否则点过 🙃 后被别的路径覆盖就丢了信号）。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from app.observability.task_record import TZ, TaskRecord, TaskRecorder
from app.protocols.feedback_card import (
    ACTION_DOWN,
    ACTION_UP,
    FB_KEY,
    TASK_KEY,
    build_done_card,
    build_feedback_card,
    is_feedback_action,
    parse_feedback_value,
)


def _event(value: object) -> SimpleNamespace:
    return SimpleNamespace(action=SimpleNamespace(value=value),
                           message_id="om_1", chat_id="oc_x")


# ── 卡片层 ────────────────────────────────────────────────────────────


def test_feedback_card_buttons_carry_callback_behaviors() -> None:
    """2.0 卡片缺 behaviors = 按钮点不动（飞书报「回调服务未在线」）。"""
    card = build_feedback_card("t1")
    buttons = [el for col in card["body"]["elements"][1]["columns"]
               for el in col["elements"]]
    assert len(buttons) == 2
    for btn in buttons:
        assert btn["behaviors"] == [{"type": "callback", "value": btn["value"]}]
        assert btn["value"][TASK_KEY] == "t1"


def test_is_feedback_action_recognizes_own_card_only() -> None:
    """别的卡片（能力审批 / PRD / 定时任务）点了不该被这里认领。"""
    assert is_feedback_action(_event({FB_KEY: ACTION_UP, TASK_KEY: "t1"}))
    assert is_feedback_action(_event({FB_KEY: ACTION_DOWN, TASK_KEY: "t1"}))
    assert not is_feedback_action(_event({"task": "task-123", "act": "approve"}))
    assert not is_feedback_action(_event(None))
    assert not is_feedback_action(SimpleNamespace(action=None))


def test_feedback_payload_does_not_reuse_scheduler_task_key() -> None:
    """反馈卡与定时任务审批卡不能共用 `task` 键。

    卡片回调广播给所有 handler：反馈卡若也带 `{task: ...}`，审批 handler 会把
    这次点击当成「一张找不到档案的审批卡」，在群里回一句「未找到定时任务档案」。
    """
    from app.ops.capability.approval_card import _extract_action_value

    card = build_feedback_card("t1")
    value = card["body"]["elements"][1]["columns"][0]["elements"][0]["value"]
    assert "task" not in value, "反馈卡复用了审批卡的 task 键"
    assert value[TASK_KEY] == "t1"

    # 反向：审批 handler 在反馈卡点击上取不到它要的 task 值
    event = _event(value)
    assert _extract_action_value(event, key="task") is None


def test_parse_feedback_value_maps_direction() -> None:
    assert parse_feedback_value(_event({FB_KEY: ACTION_UP, TASK_KEY: "t1"})) == ("t1", True)
    assert parse_feedback_value(_event({FB_KEY: ACTION_DOWN, TASK_KEY: "t1"})) == ("t1", False)
    assert parse_feedback_value(_event({"x": 1})) == ("", False)


def test_done_card_has_no_buttons() -> None:
    """一次反馈即终态：卡片里不能再留按钮，否则会被反复点改判。"""
    card = build_done_card("t1", positive=False, operator="ww")
    assert "button" not in json.dumps(card)
    assert "🙃" in json.dumps(card, ensure_ascii=False)


# ── 回填与归类 ────────────────────────────────────────────────────────


def _recorder(tmp_path) -> TaskRecorder:
    return TaskRecorder(log_dir=tmp_path)


def _write(rec: TaskRecorder, **kw) -> None:
    defaults = dict(task_id="t1", chat_id="oc_x", sender_id="ou_1",
                    query="今天 AI 新闻", final_agent="aihot", status="success")
    defaults.update(kw)
    rec.write(TaskRecord(**defaults))


def test_attach_by_task_id_lands_on_exact_turn(tmp_path) -> None:
    """按 task_id 精确回填：同一会话连问两轮，点第二轮的按钮不能落到第一轮。"""
    rec = _recorder(tmp_path)
    _write(rec, task_id="t1")
    _write(rec, task_id="t2")

    assert rec.attach_feedback_by_task("t2", {"negative": True,
                                              "text": "按钮：🙃 不太行",
                                              "source": "button"})
    path = tmp_path / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    rows = {json.loads(x)["task_id"]: json.loads(x)
            for x in path.read_text(encoding="utf-8").splitlines() if x}
    assert rows["t2"]["feedback"]["negative"] is True
    # t1 落盘时 feedback 是 None（字段恒存在），没被点过就是 None
    assert rows["t1"]["feedback"] is None


def test_merge_keeps_negative_when_text_feedback_is_neutral(tmp_path) -> None:
    """按钮点了 🙃，后续文字反馈不能把它翻回正向。"""
    rec = _recorder(tmp_path)
    _write(rec, task_id="t1")
    rec.attach_feedback_by_task("t1", {"negative": True, "text": "🙃", "source": "button"})
    rec.attach_feedback_by_task("t1", {"negative": False, "text": "补充一句",
                                       "source": "text"})

    path = tmp_path / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[0])
    fb = row["feedback"]
    assert fb["negative"] is True
    # 首条（按钮）的 source 也要进 sources：合并时以 existing 为底，
    # 首条自己没带 sources 就得在这里补上，否则「谁判负的」无从回溯
    assert set(fb["sources"]) == {"button", "text"}


def test_summarize_reports_negative_rate_per_agent(tmp_path) -> None:
    """聚合出「哪个能力被点 🙃 的比例高」——优化的直接依据。"""
    rec = _recorder(tmp_path)
    _write(rec, task_id="a1", final_agent="aihot")
    _write(rec, task_id="a2", final_agent="aihot")
    _write(rec, task_id="b1", final_agent="webportal-agent")
    rec.attach_feedback_by_task("a1", {"negative": True, "text": "🙃", "source": "button"})

    summary = rec.summarize(days=1)
    agents = summary["agents"]
    assert agents["aihot"]["count"] == 2
    assert agents["aihot"]["negative"] == 1
    assert agents["aihot"]["negative_rate"] == 0.5
    assert agents["webportal-agent"]["negative_rate"] == 0.0
    assert summary["negative_count"] == 1
