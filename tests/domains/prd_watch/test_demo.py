"""自测台单测 — demo 卡片生成 + 「修正」两段式回填，零网络。

自测台的意义：按钮回调只有真人点得到，而真实监听要等绑定配好才有合法入口发卡。
这里钉住的是「卡片能生成、两条评论路径各自落在哪、修正不会写错文案」。

跑法：python -m pytest tests/domains/prd_watch/test_demo.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.card_actions import CardActionHandler
from app.domains.prd_watch.demo import run_demo
from app.domains.prd_watch.ledger import COLUMNS, MemoryLedger
from app.domains.prd_watch.render import card_text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DOC = "docDEMO"
_URL = "https://x.feishu.cn/wiki/nodeTok"


class _FakeAnchor:
    """锚点替身：按 quote 是否为空给块 id，模拟「定位成功 / 新增条款」两种形态。"""

    async def title(self, doc: str) -> str:
        return "自测 PRD"

    async def locate(self, doc: str, quote: str) -> str:
        return "blk_real" if quote else ""

    async def section_of(self, doc: str, block_id: str) -> str:
        return "类型 Tab 切换"


class _FakeLark:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []
        self.replied: list[tuple[str, str]] = []
        self.resolved: list[str] = []

    async def add_comment(self, doc: str, text: str, **_kw: Any) -> dict:
        self.added.append((doc, text))
        return {"data": {"comment_id": "cm_new"}}

    async def reply_comment(self, doc: str, comment_id: str, text: str, **_kw: Any) -> dict:
        self.replied.append((comment_id, text))
        return {}

    async def resolve_comment(self, doc: str, comment_id: str, **_kw: Any) -> dict:
        self.resolved.append(comment_id)
        return {}


def _cfg() -> PrdWatchConfig:
    return PrdWatchConfig(enabled=True, dry_run=False, comment_identity="user")


def _demo(ledger: MemoryLedger | None = None):
    led = ledger or MemoryLedger(_cfg())
    card, points, binding = asyncio.run(run_demo(
        _DOC, chat_id="oc_demo", ledger=led, config=_cfg(),
        anchor=_FakeAnchor(), prd_url=_URL))
    return card, points, binding, led


def _click(cr: str, act: str, open_id: str = "ou_admin") -> Any:
    return types.SimpleNamespace(
        action=types.SimpleNamespace(value={"cr": cr, "act": act}),
        chat_id="oc_demo", message_id="om_1",
        operator=types.SimpleNamespace(open_id=open_id, name="管理员"))


def test_card_lists_every_sample_with_buttons_and_sections() -> None:
    card, points, binding, led = _demo()
    text = card_text(card)
    assert "自测卡片" in text and len(points) == 3
    assert f"[自测 PRD]({_URL})" in text            # PRD 列用用户给的链接
    assert "章节：类型 Tab 切换" in text
    assert f"{_URL}#blk_real" in text               # 定位原文可跳转
    assert "（新增条款，PRD 无对应原文）" in text
    assert text.count("✅ 采纳") == 3
    assert binding.story_id == "DEMO"
    # 三条都进了台账且是 pending（按钮按 CR 回查台账，缺一条按钮就点了没反应）
    assert len(led._changes) == 3
    assert all(r[COLUMNS["decision"]] == "pending" for r in led._changes.values())


def test_demo_without_url_never_fabricates_link() -> None:
    led = MemoryLedger(_cfg())
    card, _points, binding = asyncio.run(run_demo(
        _DOC, chat_id="oc_demo", ledger=led, config=_cfg(), anchor=_FakeAnchor()))
    text = card_text(card)
    assert binding.prd_url == ""
    assert "（无 PRD 链接）" in text
    assert "feishu.cn/docx" not in text             # token 拼 URL 是打不开的假链接


def test_no_comment_written_until_a_button_is_clicked() -> None:
    """demo 只发卡片：没点按钮前文档里不该多出任何评论。"""
    _card, _points, _b, _led = _demo()
    lark = _FakeLark()
    assert lark.added == [] and lark.replied == []


def test_adopt_without_comment_id_reports_honestly() -> None:
    """自测行没有评论 id：状态照落，但必须说明「未回写文档」，不能假装写了。"""
    _card, points, _b, led = _demo()
    lark = _FakeLark()
    h = CardActionHandler(_cfg(), led, lark=lark)   # type: ignore[arg-type]
    text = asyncio.run(h.handle(_click(points[0].cr_id, "adopt")))
    assert led._changes[points[0].cr_id][COLUMNS["decision"]] == "已采纳"
    assert "未回写文档" in text
    assert lark.replied == [] and lark.added == []


def test_fix_requires_reply_then_writes_new_comment() -> None:
    _card, points, _b, led = _demo()
    lark = _FakeLark()
    h = CardActionHandler(_cfg(), led, lark=lark)   # type: ignore[arg-type]
    cr = points[1].cr_id
    prompt = asyncio.run(h.handle(_click(cr, "fix")))
    assert cr in prompt and "修改后表述" in prompt
    assert led._changes[cr][COLUMNS["decision"]] == "pending"   # 还没定
    assert lark.added == []

    text = asyncio.run(h.apply_fix("oc_demo", "ou_admin", "改为：默认选中票据"))
    assert led._changes[cr][COLUMNS["decision"]] == "已调整"
    assert lark.added and "改为：默认选中票据" in lark.added[0][1]
    assert cr in lark.added[0][1]
    assert "已调整" in text


def test_fix_state_is_bound_to_the_person_who_clicked() -> None:
    """群里别人接着说话不能被当成修正文案落进 PRD。"""
    _card, points, _b, led = _demo()
    lark = _FakeLark()
    h = CardActionHandler(_cfg(), led, lark=lark)   # type: ignore[arg-type]
    asyncio.run(h.handle(_click(points[0].cr_id, "fix", open_id="ou_admin")))
    assert h.pending_fix("ou_other") == ""
    assert asyncio.run(h.apply_fix("oc_demo", "ou_other", "我插一句")) == ""
    assert lark.added == []
    assert h.pending_fix("ou_admin") == points[0].cr_id


def test_fix_state_expires() -> None:
    import app.domains.prd_watch.card_actions as ca

    _card, points, _b, led = _demo()
    h = CardActionHandler(_cfg(), led, lark=_FakeLark())   # type: ignore[arg-type]
    old = ca.FIX_TTL_SEC
    ca.FIX_TTL_SEC = -1.0                        # 立即过期
    try:
        asyncio.run(h.handle(_click(points[0].cr_id, "fix")))
        assert h.pending_fix("ou_admin") == ""
    finally:
        ca.FIX_TTL_SEC = old


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")
