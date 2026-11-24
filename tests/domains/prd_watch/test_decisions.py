"""easyprd 决策闭环单测（fake 评论读写 + fake 台账，零网络）。

跑法：python -m pytest tests/domains/prd_watch/test_decisions.py -q
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.decisions import (
    DecisionOutcome,
    DecisionPoller,
    classify_decision,
)
from app.domains.prd_watch.ledger import COLUMNS, MemoryLedger, StoryBinding
from app.domains.prd_watch.render import card_text
from app.protocols.lark_cli import LarkCliError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_CR = "CR-20261102-8821-001"


def _cfg(**kw: object) -> PrdWatchConfig:
    return PrdWatchConfig(enabled=True, watch_chats=["oc_x"],
                          decision_ttl_days=3, **kw)  # type: ignore[arg-type]


def _ledger_with_row(**over: object) -> MemoryLedger:
    cfg = _cfg()
    led = MemoryLedger(cfg)
    led.add_binding(StoryBinding(story_id="8821", prd_token="docPRD", chat_id="oc_x"))
    row: dict[str, object] = {COLUMNS["cr_id"]: _CR, COLUMNS["decision"]: "pending",
           COLUMNS["comment_id"]: "com_1", "PRD文档token": "docPRD",
           "StoryID": "8821", "关联群chat_id": "oc_x", "变更标题": "申购上限 5万→20万"}
    row.update(over)
    led._changes[_CR] = dict(row)
    return led


class _FakeLark:
    def __init__(self, replies: list[dict], *, fail: bool = False) -> None:
        self.replies = replies
        self.fail = fail
        self.replied: list[tuple[str, str]] = []
        self.resolved: list[str] = []

    async def list_comment_replies(self, doc: str, comment_id: str, **_kw: object) -> list[dict]:
        if self.fail:
            raise LarkCliError("missing scope: docs:document.comment:read")
        assert doc == "docPRD" and comment_id == "com_1", (doc, comment_id)
        return list(self.replies)

    async def reply_comment(self, doc: str, comment_id: str, text: str, **_kw: object) -> dict:
        self.replied.append((comment_id, text))
        return {"ok": True}

    async def resolve_comment(self, doc: str, comment_id: str, **_kw: object) -> dict:
        self.resolved.append(comment_id)
        return {"ok": True}


def _poller(replies: list[dict], **over: object):
    sent: list[tuple[str, Any]] = []   # payload: 卡片 dict 或纯文本

    async def _notify(chat_id: str, text: Any) -> None:
        sent.append((chat_id, text))

    led = _ledger_with_row(**over)
    lark = _FakeLark(replies)
    p = DecisionPoller(_cfg(), led, lark=lark, notify=_notify)  # type: ignore[arg-type]
    return p, led, lark, sent


def test_classify_decision_words() -> None:
    assert classify_decision("采纳") == "adopt"
    assert classify_decision("同意，就这么改") == "adopt"
    assert classify_decision("忽略") == "ignore"
    assert classify_decision("调整：上限写 15 万") == "adjust"
    assert classify_decision("这个我再想想") == ""      # 认不出不猜
    assert classify_decision("") == ""


def test_adopt_marks_comment_and_ledger_and_notifies() -> None:
    p, led, lark, sent = _poller([{"reply_id": "r1", "text": "采纳", "user_id": "ou_prod"}])
    out = asyncio.run(p.poll_once())
    assert out.applied == [_CR] and out.errors == [], out
    assert led._changes[_CR][COLUMNS["decision"]] == "已采纳"
    assert lark.replied[0][1].startswith("✅ 已采纳")
    # 采纳明确告知「正文没改」，避免研发以为已生效
    assert sent[0][0] == "oc_x" and isinstance(sent[0][1], dict), sent
    assert "PRD 正文需人工" in card_text(sent[0][1])
    assert lark.resolved == []  # 采纳不 resolve（产品还要看）


def test_ignore_resolves_comment() -> None:
    p, led, lark, sent = _poller([{"reply_id": "r1", "text": "忽略这条", "user_id": "ou_prod"}])
    out = asyncio.run(p.poll_once())
    assert out.applied == [_CR]
    assert led._changes[_CR][COLUMNS["decision"]] == "已忽略"
    assert lark.resolved == ["com_1"]
    assert "已忽略" in card_text(sent[0][1])


def test_adjust_records_note() -> None:
    p, led, _lark, sent = _poller([{"reply_id": "r1", "text": "调整：改成15万",
                                   "user_id": "ou_prod"}])
    asyncio.run(p.poll_once())
    assert led._changes[_CR][COLUMNS["decision"]] == "已调整"
    assert "改成15万" in str(led._changes[_CR].get("决策备注"))
    assert "已调整" in card_text(sent[0][1])


def test_no_reply_stays_pending() -> None:
    p, led, _lark, sent = _poller([{"reply_id": "r1", "text": "这个我还要问下合规",
                                   "user_id": "ou_prod"}])
    out = asyncio.run(p.poll_once())
    assert out.pending == 1 and out.applied == []
    assert led._changes[_CR][COLUMNS["decision"]] == "pending"
    assert sent == []


def test_second_poll_does_not_reprocess_same_reply() -> None:
    """同一回复只处理一次：否则每轮都会重复回写评论 + 重复发群通知。"""
    p, led, lark, sent = _poller([{"reply_id": "r1", "text": "采纳", "user_id": "ou_prod"}])
    asyncio.run(p.poll_once())
    led._changes[_CR][COLUMNS["decision"]] = "pending"  # 模拟台账被外部改回 pending
    out2 = asyncio.run(p.poll_once())
    assert out2.applied == [] and out2.pending == 1, out2
    assert len(lark.replied) == 1 and len(sent) == 1


def test_missing_comment_id_skipped_not_guessed() -> None:
    p, _led, lark, _sent = _poller([])
    p_led = p._ledger
    p_led._changes[_CR][COLUMNS["comment_id"]] = ""
    out = asyncio.run(p.poll_once())
    assert out.pending == 1 and lark.replied == []
    assert out.errors == []  # 缺坐标是常态（dry_run 不写评论），不算异常


def test_read_failure_recorded_not_raised() -> None:
    led = _ledger_with_row()
    lark = _FakeLark([], fail=True)
    p = DecisionPoller(_cfg(), led, lark=lark)  # type: ignore[arg-type]
    out = asyncio.run(p.poll_once())
    assert out.errors and "missing scope" in out.errors[0], out.errors
    assert led._changes[_CR][COLUMNS["decision"]] == "pending"


def test_remind_once_after_ttl() -> None:
    import time

    sent: list[tuple[str, Any]] = []   # payload: 卡片 dict 或纯文本

    async def _notify(chat_id: str, text: Any) -> None:
        sent.append((chat_id, text))

    led = _ledger_with_row(**{"创建时间": time.time() - 5 * 86400})
    p = DecisionPoller(_cfg(), led, lark=_FakeLark([]), notify=_notify)  # type: ignore[arg-type]
    out = asyncio.run(p.poll_once())
    assert out.reminded == [_CR] and "未确认" in card_text(sent[0][1])
    out2 = asyncio.run(p.poll_once())
    assert out2.reminded == [] and len(sent) == 1  # 只催一次


def test_summary_text() -> None:
    o = DecisionOutcome(checked=3, applied=["a"], pending=2)
    assert "待决 3" in o.summary() and "已定 1" in o.summary()


def test_comment_body_is_never_read_as_a_reply() -> None:
    """评论本体带着「采纳 → 回复『采纳』」的操作说明，当成回复读会让整批 pending
    在首轮全被判成已采纳。真机上就是这么误判的，必须跳过本体。"""
    p, led, lark, _sent = _poller([
        {"reply_id": "body", "is_body": True,
         "text": "▎决策状态\n⏳ 待产品确认\n  · 采纳 → 回复「采纳」", "user_id": "ou_me"},
        {"reply_id": "r1", "is_body": False, "text": "忽略", "user_id": "ou_prod"},
    ])
    out = asyncio.run(p.poll_once())
    assert led._changes[_CR][COLUMNS["decision"]] == "已忽略", "本体的说明文字被误读成采纳/忽略"
    assert out.applied == [_CR] and len(lark.replied) == 1


def test_only_comment_body_leaves_row_pending() -> None:
    p, led, lark, sent = _poller([
        {"reply_id": "body", "is_body": True,
         "text": "· 采纳 → 回复「采纳」", "user_id": "ou_me"}])
    out = asyncio.run(p.poll_once())
    assert out.pending == 1 and out.applied == []
    assert led._changes[_CR][COLUMNS["decision"]] == "pending"
    assert lark.replied == [] and sent == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")
