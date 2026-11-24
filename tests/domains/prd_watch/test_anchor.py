"""锚点定位单测 + 一条真实只读验证（不写任何东西）。

跑法：python -m pytest tests/domains/prd_watch/test_anchor.py -q
"""

from __future__ import annotations

import asyncio
import sys

from app.domains.prd_watch.anchor import BlockAnchor, normalize
from app.protocols.lark_cli import LarkCliError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_BLOCKS = [
    {"block_id": "b1", "text": "单笔申购上限为人民币5万元，单日累计不超过10万元。"},
    {"block_id": "b2", "text": "赎回支持T+0到账，节假日顺延。"},
    {"block_id": "b3", "text": "本产品面向合格投资者，需签署风险揭示书。"},
]


class _FakeLark:
    """按真实 fetch_block_ids 契约打桩：keyword 命中则只回该块。"""

    def __init__(self, fail_keyword: bool = False) -> None:
        self.fail_keyword = fail_keyword
        self.calls: list[tuple[str, str]] = []

    async def fetch_block_ids(self, doc: str, *, keyword: str = "") -> list[dict[str, str]]:
        self.calls.append((doc, keyword))
        if keyword:
            if self.fail_keyword:
                raise LarkCliError("missing scope")
            return [b for b in _BLOCKS if keyword.replace(" ", "") in b["text"]]
        return list(_BLOCKS)

    async def fetch_blocks(self, doc: str) -> list[dict[str, str]]:
        """带元素类型的全文块（章节归属用）：h2 标题 + 段落各一块。"""
        return [
            {"block_id": "h1", "tag": "h1", "text": "RWA 申购下单 PRD"},
            {"block_id": "h2", "tag": "h2", "text": "二、产品规则"},
            {"block_id": "b1", "tag": "p",
             "text": "单笔申购上限为人民币5万元，单日累计不超过10万元。"},
            {"block_id": "h3", "tag": "h3", "text": "2.2 赎回规则"},
            {"block_id": "b2", "tag": "p", "text": "赎回支持T+0到账，节假日顺延。"},
        ]


def test_normalize_strips_punct_space() -> None:
    assert normalize("单笔申购上限 为人民币5万元，") == normalize("单笔申购上限为人民币5万元")


def test_locate_by_keyword_hit() -> None:
    lark = _FakeLark()
    bid = asyncio.run(BlockAnchor(lark).locate("doc1", "单笔申购上限为人民币5万元，单日累计不超过10万元。"))
    assert bid == "b1", bid
    assert lark.calls[0][1]  # 先走服务端关键词筛


def test_locate_falls_back_to_full_scan() -> None:
    """关键词筛不可用（缺 scope/超时）→ 全文本地兜底仍要定位到。"""
    lark = _FakeLark(fail_keyword=True)
    bid = asyncio.run(BlockAnchor(lark).locate("doc1", "赎回支持T+0到账"))
    assert bid == "b2", bid


def test_locate_quote_punct_mismatch() -> None:
    """讨论里引用常丢标点/多空格，归一化后仍要对上。"""
    bid = asyncio.run(BlockAnchor(_FakeLark()).locate("doc1", "合格投资者需签署风险揭示书"))
    assert bid == "b3", bid


def test_locate_miss_returns_empty() -> None:
    bid = asyncio.run(BlockAnchor(_FakeLark()).locate("doc1", "完全不存在的条款"))
    assert bid == "", bid


def test_empty_inputs() -> None:
    anchor = BlockAnchor(_FakeLark())
    assert asyncio.run(anchor.locate("", "任意")) == ""
    assert asyncio.run(anchor.locate("doc1", "")) == ""


def test_blocks_cached_per_doc() -> None:
    lark = _FakeLark(fail_keyword=True)
    anchor = BlockAnchor(lark)
    asyncio.run(anchor.locate("doc1", "赎回支持T+0到账"))
    asyncio.run(anchor.locate("doc1", "合格投资者"))
    full_calls = [c for c in lark.calls if c[1] == ""]
    assert len(full_calls) == 1, lark.calls  # 全文只取一次
    assert anchor.stats()["docs_cached"] == 1


def test_excerpt_concatenates_blocks() -> None:
    text = asyncio.run(BlockAnchor(_FakeLark()).excerpt("doc1", max_chars=20))
    assert text and len(text) <= 20


def test_readonly_against_real_doc() -> None:
    """真实文档只读校验（案例库索引文档，永久存在，不写不改）。"""
    doc = "doxcnExampleToken0000000000"
    anchor = BlockAnchor()
    blocks = asyncio.run(anchor._all_blocks(doc))
    if not blocks:
        print("⏭ 跳过：lark-cli 未登录或该文档不可读（非回归）")
        return
    target = blocks[0]["block_id"]
    got = asyncio.run(anchor.locate(doc, blocks[0]["text"][:20]))
    assert got == target, (got, target, blocks[0]["text"][:30])


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")


def test_section_of_returns_nearest_headings() -> None:
    """章节归属：取块上方最近的两级标题（卡片上要写明原文所在章节）。"""
    a = BlockAnchor(_FakeLark())
    assert asyncio.run(a.section_of("doc", "b1")) == "二、产品规则"
    # 第三个标题进来后只保留最近两级，不会退化成整条面包屑
    assert asyncio.run(a.section_of("doc", "b2")) == "二、产品规则 > 2.2 赎回规则"
    assert asyncio.run(a.section_of("doc", "not-exist")) == ""


def test_locate_in_doc_returns_block_text() -> None:
    a = BlockAnchor(_FakeLark())
    bid, text = asyncio.run(a.locate_in_doc("doc", "单笔申购上限为人民币5万元"))
    assert bid == "b1" and text.startswith("单笔申购上限")


def test_fuzzy_locate_handles_long_paragraph() -> None:
    """长段落型 PRD：口语 quote 与原文措辞不完全一致时，模糊匹配也要命中。

    实测某篇 PRD 全是几百字长段落，只做"包含"时同一场会里它 0/5 锚中。
    """
    long_block = {"block_id": "b9", "tag": "p",
                  "text": "产品是否可见、能否进入详情、能否操作，由白名单、投资者类型、"
                          "买卖开关、持仓四个条件共同决定，其中白名单来自中台产品库配置。"}
    class _Long(_FakeLark):
        async def fetch_block_ids(self, doc: str, *, keyword: str = "") -> list[dict[str, str]]:
            return [long_block]
    a = BlockAnchor(_Long())
    bid = asyncio.run(a.locate("doc", "可见性由白名单、投资者类型、买卖开关、持仓共同决定"))
    assert bid == "b9"


def test_fuzzy_locate_does_not_match_unrelated_block() -> None:
    """模糊匹配不能变成"总能匹配上"：无关内容必须仍然返回空。"""
    a = BlockAnchor(_FakeLark())
    assert asyncio.run(a.locate("doc", "完全无关的一段话内容")) == ""


def test_excerpt_for_picks_relevant_blocks() -> None:
    """按讨论挑块：相关块进节选，不相关的排后面/不进（稳定性的根因之一）。"""
    a = BlockAnchor(_FakeLark())
    text = asyncio.run(a.excerpt_for("doc", "赎回支持T+0到账吗？", max_chars=40))
    assert "赎回支持T+0" in text
    # 没有命中时退回全文前 N 字（原行为，不能变成空节选）
    plain = asyncio.run(a.excerpt_for("doc", "完全无关讨论", max_chars=30))
    assert plain
