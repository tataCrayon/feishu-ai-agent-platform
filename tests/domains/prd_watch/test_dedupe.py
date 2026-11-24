"""去重/归一层单测 — 中文同义改写要合、不同主语要分开、跨轮重复要拦。

跑法：python -m pytest tests/domains/prd_watch/test_dedupe.py -q
"""

from __future__ import annotations

import sys

from app.domains.prd_watch.dedupe import (
    EmittedIndex,
    merge_points,
    similarity,
)
from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL, ChangePoint

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _cp(title: str, *, story: str = "8821", current: str = "",
        suggested: str = "", conf: float = 0.9,
        band: str = BAND_NORMAL) -> ChangePoint:
    return ChangePoint(title=title, story_id=story, current_text=current,
                       suggested_text=suggested, confidence=conf, band=band)


def test_similarity_separates_synonym_from_distinct() -> None:
    """同义改写 ≥0.5，异义 <0.5（阈值就是按实测分布标的）。"""
    assert similarity("取消 PI 专业投资者准入拦截", "取消 PI 准入拦截") >= 0.5
    assert similarity("申购上限 5万→20万", "赎回改 T+1") < 0.5
    assert similarity("", "任何") == 0.0


def test_merge_synonyms_same_subject() -> None:
    pts = [_cp("申购取消 KYT 校验", suggested="改由 Finmix 负责"),
           _cp("申购流程不再执行 KYT", suggested="改由 Finmix 负责", conf=0.8)]
    kept, merged = merge_points(pts)
    assert merged == 1 and len(kept) == 1
    assert kept[0].title == "申购取消 KYT 校验"      # 保留高置信那条
    assert kept[0].confidence == 0.9


def test_merge_keeps_different_subjects_apart() -> None:
    """「申购取消 KYT」和「赎回取消 KYT」字符集相似度 0.714，但不是同一件事。"""
    kept, merged = merge_points([_cp("申购取消 KYT", story="1"),
                                 _cp("赎回取消 KYT", story="2")])
    assert merged == 0 and len(kept) == 2


def test_merge_does_not_use_shared_current_text() -> None:
    """同一段原文里提了两处不同改动：原文相同不能作为合并依据（踩过的坑）。"""
    shared = "单笔申购上限为人民币5万元，单日累计不超过10万元。"
    kept, merged = merge_points([_cp("申购上限 5万→20万", current=shared),
                                 _cp("赎回改 T+1", current=shared)])
    assert merged == 0 and len(kept) == 2


def test_merge_flags_and_modules_union() -> None:
    """合并后低置信标记不能被洗掉，模块取并集。"""
    a = _cp("申购取消 KYT", band=BAND_NORMAL)
    a.modules = ["申购"]
    b = _cp("申购流程不再执行 KYT", conf=0.72, band=BAND_FLAGGED)
    b.modules = ["合规"]
    kept, merged = merge_points([a, b])
    assert merged == 1
    assert kept[0].band == BAND_FLAGGED and "合规" in kept[0].modules


def test_cross_round_dedupe_by_material() -> None:
    """同一素材重跑 → 同义条目判重复；换了素材（新讨论/PRD 已改）→ 照常出。"""
    idx = EmittedIndex()
    idx.record("8821", "src1", _cp("申购取消 KYT 校验"))
    assert idx.is_duplicate("8821", "src1", _cp("申购流程不再执行 KYT"))
    assert not idx.is_duplicate("8821", "src2", _cp("申购流程不再执行 KYT"))
    assert not idx.is_duplicate("9900", "src1", _cp("赎回取消 KYT"))


def test_drop_duplicates_counts_both_layers() -> None:
    idx = EmittedIndex()
    idx.record("8821", "src", _cp("申购取消 KYT 校验"))
    pts = [_cp("申购流程不再执行 KYT"),          # 跨轮重复
           _cp("申购仅保留预约计价", suggested="x"),
           _cp("申购仅保留预约计价模式", suggested="x")]   # 批内近义
    fresh, dropped = idx.drop_duplicates("8821", "src", pts)
    assert len(fresh) == 1 and dropped == 2


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")


# ── 文档侧已有评论检测（跨进程判重）───────────────────────────────

def _comment(body: str, *, block: str = "", quote: str = "", whole: bool = False,
             cid: str = "c1") -> dict:
    return {"comment_id": cid, "block_id": block, "quote": quote, "body": body,
            "is_solved": False, "is_whole": whole, "reply_count": 1}


def _bot_body(title: str, cr: str = "CR-20261104-8821-001") -> str:
    return (f"📋 PRD变更建议 · {cr}\n\n▎需求信息\nStory：X（#8821）\n\n"
            f"▎影响范围\n变更类型：参数调整\n\n▎变更项\n{title}\n\n▎当前PRD原文\n> 旧文\n")


def test_comment_title_parses_bot_comment_only() -> None:
    from app.domains.prd_watch.dedupe import comment_title

    assert comment_title(_bot_body("申购上限 5万→20万")) == "申购上限 5万→20万"
    # 人写的评论没有这套结构 → 空串（只按块判重，不猜语义）
    assert comment_title("这里是不是要改成 20 万？") == ""


def test_same_block_and_same_title_is_duplicate() -> None:
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("申购上限 5万→20万")
    cp.block_id = "blkA"
    hit = duplicate_of(cp, [_comment(_bot_body("申购上限 5万→20万"), block="blkA")])
    assert hit is not None and hit["comment_id"] == "c1"


def test_same_change_other_block_still_duplicate() -> None:
    """锚点本轮落到相邻块，但说的就是同一件事 —— 仍然算重复，不重复写。"""
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("申购上限 5万→20万")
    cp.block_id = "blkB"
    assert duplicate_of(cp, [_comment(_bot_body("申购上限调整到20万"), block="blkA")])


def test_same_block_different_change_not_duplicate() -> None:
    """同一段原文里两处不同改动，不能因为块相同就吞掉。"""
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("赎回改成 T+1 确认")
    cp.block_id = "blkA"
    assert duplicate_of(cp, [_comment(_bot_body("申购上限 5万→20万"), block="blkA")]) is None


def test_subject_guard_applies_to_document_side_too() -> None:
    """"申购取消 KYT" 与 "赎回取消 KYT" 相似度很高，但这是两件事。"""
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("赎回取消 KYT 校验")
    cp.block_id = "blkA"
    assert duplicate_of(cp, [_comment(_bot_body("申购取消 KYT 校验"), block="blkA")]) is None


def test_human_comment_same_block_with_title_inside_is_duplicate() -> None:
    """人写的评论里逐字抄了我们的标题 → 同一处已经提过，不再重复写。"""
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("申购上限 5万→20万")
    cp.block_id = "blkA"
    assert duplicate_of(cp, [_comment("申购上限 5万→20万 这条请产品确认下", block="blkA")])


def test_whole_document_comment_never_counts() -> None:
    from app.domains.prd_watch.dedupe import duplicate_of

    cp = _cp("申购上限 5万→20万")
    cp.block_id = "blkA"
    assert duplicate_of(cp, [_comment(_bot_body("申购上限 5万→20万"),
                                      block="", whole=True)]) is None


# ── 建议表述切关键词（建议表述不会逐字出现在文档里）─────────────────

def test_locator_terms_dedupes_offsets_and_drops_generic() -> None:
    from app.domains.prd_watch.dedupe import locator_terms

    terms = locator_terms("中台产品库未返回「下单价格确定方式」时，货架与详情页均不展示价格。")
    assert terms, "必须切出关键词"
    # 同一短语的三个偏移不能全留（否则一个词被算三遍，错块会赢）
    assert sum("中台产品库" in t for t in terms) <= 1
    assert all(len(t) >= 3 for t in terms)
    assert not any(t in ("的要求", "说明", "调整") for t in terms)


def test_locate_by_terms_prefers_specific_block() -> None:
    """2 个以上关键词共现才算命中；同分取更短的块。"""
    import asyncio

    from app.domains.prd_watch.anchor import BlockAnchor

    class _Reader:
        def __init__(self) -> None:
            self.blocks = [
                {"block_id": "b1", "tag": "p", "text": "申购与赎回两个入口独立判断，互不牵连。"},
                {"block_id": "b2", "tag": "p", "text": "中台产品库未返回下单价格确定方式时，不展示价格。"},
                {"block_id": "b3", "tag": "p", "text": "无关段落，只提到中台。"},
            ]

        async def fetch_block_ids(self, doc: str, *, keyword: str = "") -> list[dict]:
            return self.blocks

        async def fetch_blocks(self, doc: str) -> list[dict]:
            return self.blocks

    a = BlockAnchor(lark=_Reader())  # type: ignore[arg-type]
    assert asyncio.run(a.locate_by_terms("d", ["下单价格", "不展示价格"])) == "b2"
    # 关键词在文档里全部落空 → 不硬凑任何块
    assert asyncio.run(a.locate_by_terms("d", ["完全没有", "不相干的词"])) == ""
    # 一半以上的关键词都指向同一块（滑窗切词时常见）→ 可以定位
    assert asyncio.run(a.locate_by_terms("d", ["中台产品库", "不相干的词"])) == "b2"
