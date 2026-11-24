"""卡片按钮单测（采纳/修正/忽略）— fake 台账 + fake 评论读写，零网络。

跑法：python -m pytest tests/domains/prd_watch/test_card_actions.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.card_actions import (
    CardActionHandler,
    build_review_buttons,
    extract_action,
)
from app.domains.prd_watch.ledger import COLUMNS, MemoryLedger, StoryBinding

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_CR = "CR-20261103-8821-001"
_CR2 = "CR-20261103-8821-002"


def _cfg() -> PrdWatchConfig:
    return PrdWatchConfig(enabled=True, dry_run=False, comment_identity="bot")


class _FakeLark:
    def __init__(self) -> None:
        self.replied: list[tuple[str, str, str]] = []
        self.resolved: list[str] = []

    async def reply_comment(self, doc: str, comment_id: str, text: str, **_kw: object) -> dict:
        self.replied.append((doc, comment_id, text))
        return {"ok": True}

    async def resolve_comment(self, doc: str, comment_id: str, **_kw: object) -> dict:
        self.resolved.append(comment_id)
        return {"ok": True}


def _ledger() -> MemoryLedger:
    led = MemoryLedger(_cfg())
    led.add_binding(StoryBinding(story_id="8821", story_title="RWA申购下单",
                                 prd_token="docA", chat_id="oc_x"))
    for cr, title in ((_CR, "申购上限调整"), (_CR2, "赎回改 T+1")):
        asyncio.run(led.record_change({
            COLUMNS["cr_id"]: cr, COLUMNS["decision"]: "pending",
            COLUMNS["comment_id"]: f"com_{cr[-3:]}", "PRD文档token": "docA",
            "关联群chat_id": "oc_x", "变更标题": title, COLUMNS["story_id"]: "8821",
        }))
    return led


def _event(value: dict[str, Any], chat_id: str = "oc_x") -> Any:
    return types.SimpleNamespace(
        action=types.SimpleNamespace(value=value), chat_id=chat_id,
        message_id="om_1", operator=types.SimpleNamespace(open_id="ou_prod", name="张三"))


def _poller(led: MemoryLedger | None = None, lark: _FakeLark | None = None):
    led = led or _ledger()
    lark = lark or _FakeLark()
    return CardActionHandler(_cfg(), led, lark=lark), led, lark  # type: ignore[arg-type]


def test_buttons_carry_cr_and_action() -> None:
    row = build_review_buttons(_CR)
    assert row["tag"] == "column_set"
    buttons = [c["elements"][0] for c in row["columns"]]
    acts = [b["value"]["act"] for b in buttons]
    assert acts == ["adopt", "fix", "ignore"]
    assert all(b["value"]["cr"] == _CR for b in buttons)
    # 2.0 卡片必须带 behaviors 才会触发回调（与 gateway 的垫片同口径）
    assert all(b["behaviors"][0]["type"] == "callback" for b in buttons)
    # 忽略是 resolve 动作，必须二次确认
    ignore = next(b for b in buttons if b["value"]["act"] == "ignore")
    assert ignore["confirm"]["title"]["content"]


def test_extract_action_reads_nested_value() -> None:
    assert extract_action(_event({"cr": _CR, "act": "adopt"})) == (_CR, "adopt")
    # 有的回调包装再套一层 {"value": {...}}
    assert extract_action(_event({"value": {"cr": _CR, "act": "ignore"}})) == (_CR, "ignore")
    assert extract_action(_event({"cap": "x"})) == ("", "")      # 别人的卡片不认领


def test_adopt_updates_ledger_and_comment() -> None:
    h, led, lark = _poller()
    text = asyncio.run(h.handle(_event({"cr": _CR, "act": "adopt"})))
    assert led._changes[_CR][COLUMNS["decision"]] == "已采纳"
    assert lark.replied[0][:2] == ("docA", "com_001")
    assert "已采纳" in lark.replied[0][2]
    assert lark.resolved == []                       # 采纳不 resolve（产品还要看）
    assert "已采纳" in text and "正文" in lark.replied[0][2]


def test_ignore_resolves_comment() -> None:
    h, led, lark = _poller()
    asyncio.run(h.handle(_event({"cr": _CR, "act": "ignore"})))
    assert led._changes[_CR][COLUMNS["decision"]] == "已忽略"
    assert lark.resolved == ["com_001"]
    # 另一条不受影响（按 CR 精确命中，不做模糊匹配）
    assert led._changes[_CR2][COLUMNS["decision"]] == "pending"


def test_fix_returns_prompt_without_touching_state() -> None:
    """修正要写新表述进 PRD，风险高于采纳 → 先要人给文案，不直接改状态。"""
    h, led, lark = _poller()
    text = asyncio.run(h.handle(_event({"cr": _CR, "act": "fix"})))
    assert _CR in text and "修改后表述" in text
    assert led._changes[_CR][COLUMNS["decision"]] == "pending"
    assert lark.replied == []


def test_unknown_or_foreign_click_is_silent() -> None:
    h, _led, _lark = _poller()
    assert asyncio.run(h.handle(_event({"cap": "abc", "act": "approve"}))) == ""
    assert asyncio.run(h.handle(_event({"cr": "CR-not-exist", "act": "adopt"}))) != ""


def test_comment_write_failure_still_keeps_ledger_state() -> None:
    """评论回写失败不能让状态停在 pending，否则下一轮又会重复处理。"""
    class _BoomLark(_FakeLark):
        async def reply_comment(self, *a: object, **_kw: object) -> dict:
            raise RuntimeError("comment scope missing")

    h, led, _ = _poller(lark=_BoomLark())
    text = asyncio.run(h.handle(_event({"cr": _CR, "act": "adopt"})))
    assert led._changes[_CR][COLUMNS["decision"]] == "已采纳"
    assert "状态已记在台账" in text


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")


def test_fix_comment_lands_on_same_block() -> None:
    """修正后的表述要挂在原评论那个块上，而不是落到整篇（会飘到文档顶部）。"""
    class _LarkWithBlocks(_FakeLark):
        def __init__(self) -> None:
            super().__init__()
            self.added: list[tuple[str, str, str]] = []

        async def list_comments(self, doc: str, **_kw: object) -> list[dict]:
            return [{"comment_id": "com_001", "block_id": "blkA", "body": "", "quote": "",
                     "is_solved": False, "is_whole": False, "reply_count": 1}]

        async def add_comment(self, doc: str, text: str, *,
                              block_id: str = "", **_kw: object) -> dict:
            self.added.append((doc, block_id, text))
            return {"data": {"comment_id": "cm_new"}}

    lark = _LarkWithBlocks()
    h, led, _ = _poller(lark=lark)  # type: ignore[arg-type]
    asyncio.run(h.handle(_event({"cr": _CR, "act": "fix"})))
    assert asyncio.run(h.apply_fix("oc_x", "ou_prod", "改为：上限 15 万")).startswith("🔄")
    assert lark.added and lark.added[0][1] == "blkA", "修正评论必须落在原块"
    assert led._changes[_CR][COLUMNS["decision"]] == "已调整"
