"""PM 催办卡片 —— 按钮点了能不能正确落到草稿上。

守五条：
1. 点「改这条」只进编辑态，不改正文；
2. 点「发这条」只发一条，其余留在批次里；
3. 点「不发这条」只去掉一条，草稿不丢；
4. 非草稿主人点 → 拒绝，什么都不改；
5. 旧卡片（batch_id 对不上）→ 明确说失效，不去改当前草稿。
"""

from __future__ import annotations


import pytest

from app.domains.pm import pm_nudge, pm_nudge_actions, pm_nudge_card as card


class _Story:
    item_id = 1
    title = "【私募】某需求"
    name = "【私募】某需求"
    status = "6-实现中"
    stage = "实现中"
    operator = "张三"
    version = "11.26"


def _nudge(audience: str) -> pm_nudge.Nudge:
    return pm_nudge.Nudge(story=_Story(), role="dev", audience=audience,
                          workdays_left=3, reason="距上线还剩 3 个工作日")


@pytest.fixture
def batch(monkeypatch):
    """造一批两条草稿，避开真实飞书项目取数。"""
    monkeypatch.setattr(pm_nudge, "draft", lambda n: f"哈喽 {n.audience}，进度如何？")
    pm_nudge._PENDING.clear()
    pm_nudge.stage("ou_pm", [_nudge("张三"), _nudge("李四")])
    b = pm_nudge.get_batch("ou_pm")
    assert b is not None and len(b.items) == 2
    return b


@pytest.fixture
def actions(monkeypatch):
    """注入假发信通道，记录调用而不真发。"""
    sent, cards, updated = [], [], []

    def send_text(target, text):
        sent.append((target, text))

    def send_card(target, payload):
        cards.append((target, payload))
        return "om_new"

    def update_card(message_id, payload):
        updated.append((message_id, payload))
        return True

    acts = pm_nudge_actions.make_actions(
        send_text, send_card, update_card,
        # 单条投递也要替掉：否则测试会真以 PM 名义给同事发消息
        send_one=lambda body: sent.append(("", body)) or True,
    )
    return acts, sent, cards, updated


def _event(action: str, batch_id: str, index: int = 0, operator: str = "ou_pm", **extra):
    value = {card.NUDGE_KEY: action, "bid": batch_id}
    if index:
        value["i"] = index
    value.update(extra)
    return {
        "action": {"value": value},
        "operator": {"open_id": operator},
        "context": {"open_chat_id": "oc_p2p"},
    }


def test_edit_enters_editing_without_changing_body(batch, actions):
    acts, sent, cards, updated = actions
    before = batch.items[0].body

    acts.handle(_event(card.ACTION_EDIT, batch.batch_id, 1))

    assert pm_nudge.get_batch("ou_pm").items[0].body == before
    assert pm_nudge.get_batch("ou_pm").editing == 1
    assert sent == []


def test_send_one_sends_exactly_one(batch, actions):
    acts, sent, cards, updated = actions
    batch.message_id = "om_old"

    acts.handle(_event(card.ACTION_ONE_SEND, batch.batch_id, 1))

    remaining = pm_nudge.get_batch("ou_pm")
    assert remaining is not None
    assert len(remaining.items) == 1
    assert remaining.items[0].nudge.audience == "李四"


def test_send_one_last_item_clears_batch(batch, actions, monkeypatch):
    """发完最后一条，批次要清空——否则「确认」会再发一遍空批次。"""
    acts, sent, cards, updated = actions
    monkeypatch.setattr(pm_nudge.pm_people, "open_id_of", lambda name: "ou_" + name)

    acts.handle(_event(card.ACTION_ONE_SEND, batch.batch_id, 1))
    acts.handle(_event(card.ACTION_ONE_SEND, batch.batch_id, 1))

    assert pm_nudge.get_batch("ou_pm") is None


def test_drop_one_keeps_other_items(batch, actions):
    acts, sent, cards, updated = actions
    batch.message_id = "om_old"

    acts.handle(_event(card.ACTION_ONE_DROP, batch.batch_id, 1))

    remaining = pm_nudge.get_batch("ou_pm")
    assert remaining is not None and len(remaining.items) == 1
    assert sent == [], "跳过的条目不发送任何消息"


def test_drop_one_on_last_clears_batch(batch, actions):
    acts, sent, cards, updated = actions
    batch.message_id = "om_old"

    acts.handle(_event(card.ACTION_ONE_DROP, batch.batch_id, 1))
    acts.handle(_event(card.ACTION_ONE_DROP, batch.batch_id, 1))

    assert pm_nudge.get_batch("ou_pm") is None


def test_other_operator_is_rejected(batch, actions):
    """别人点到按钮：必须拒绝，草稿与发送都不能变。"""
    acts, sent, cards, updated = actions

    toast = acts.handle(_event(card.ACTION_SEND, batch.batch_id, operator="ou_someone_else"))

    assert "不能代发" in toast
    assert pm_nudge.get_batch("ou_pm") is not None
    assert sent == []


def test_stale_batch_id_is_rejected(actions):
    """翻出几天前的旧卡片点：必须说失效，不能改当前草稿。"""
    acts, sent, cards, updated = actions
    pm_nudge._PENDING.clear()
    pm_nudge.stage("ou_pm", [_nudge("王五")])
    current = pm_nudge.get_batch("ou_pm")

    toast = acts.handle(_event(card.ACTION_DROP, "deadbeef", 1))

    assert "失效" in toast
    assert pm_nudge.get_batch("ou_pm") is current


def test_update_card_falls_back_to_new_card(batch, monkeypatch):
    """message_id 取不到时发新卡而不是静默——点按钮必须有反馈。"""
    sent, cards = [], []

    def send_card(target, payload):
        cards.append((target, payload))
        return "om_new"

    acts = pm_nudge_actions.make_actions(
        lambda t, x: sent.append(x),
        send_card,
        lambda mid, payload: False,   # 更新失败
    )
    batch.message_id = "om_gone"

    acts.handle(_event(card.ACTION_EDIT, batch.batch_id, 1))
    assert cards, "更新失败时应回落到发新卡"


def test_card_has_no_buttons_after_result():
    """终态卡不能再带按钮：一次确认即终态，重复点会二次发送。"""
    payload = card.build_result_card("发了 1/1 条", ok=True)
    text = str(payload)
    assert '"tag": "button"' not in text


def test_card_buttons_carry_behaviors(batch):
    """2.0 卡片的按钮必须声明 behaviors，否则点了没有回调。"""
    payload = card.build_nudge_card(batch.items, batch.batch_id)
    import json
    body = json.dumps(payload, ensure_ascii=False)
    assert '"behaviors"' in body
    assert card.NUDGE_KEY in body


def test_parse_action_ignores_foreign_cards():
    """反馈卡 / 审批卡的点击不能被催办 handler 认领。"""
    foreign = {"action": {"value": {"fb": "up", "fb_task": "t1"}},
               "operator": {"open_id": "ou_x"}}
    assert card.is_nudge_action(foreign) is False
    assert pm_nudge_actions._parse_event(foreign) is None


def test_nudges_in_card_show_body_verbatim(batch):
    """卡片里必须原样出现会发给对方的那段话——确认动作的对象就是它。"""
    payload = card.build_nudge_card(batch.items, batch.batch_id)
    import json
    body = json.dumps(payload, ensure_ascii=False)
    for item in batch.items:
        assert item.body.splitlines()[0] in body
