"""评审卡片单测：分级编号 / 判定行 / 渲染不吞字段 / 按钮与登记簿同源。

钉住的边界有三条（都是「看着能跑、用起来对不上」那类）：

1. **编号必须卡片与登记簿同源**：卡片写 B2、点「采纳」却写进 B3 的评论，
   群里和评论区就对不上是哪一条；
2. **级别只归一成三种**：模型偶尔会写 blocker / 待定 / P1，归不进去的降级成「建议」
   ——误标阻断会让整张卡失去可信度；
3. **渲染不产出结论**：字段没给就少一行，不替调用方编。

跑法：python -m pytest tests/domains/prd_watch/test_review_card.py -q
"""

from __future__ import annotations

import json
import sys

from app.domains.prd_watch.analysis_card import build_action_row
from app.domains.prd_watch.review_card import (
    ReviewBundle,
    ReviewItem,
    build_review_card,
    build_review_cards,
    item_id_for_review,
    order_items,
    to_analysis_items,
    verdict_text,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _bundle() -> ReviewBundle:
    return ReviewBundle(
        title="【Webportal】RWA 订单记录和详情",
        prd_url="https://x.feishu.cn/wiki/abc",
        prd_token="doxcnTOKEN",
        items=[
            ReviewItem(level="建议", kind="可执行性", title="导出列清单是占位",
                       quote="機構賬戶名 =（机构名称）", section="导出",
                       suggestion="给终版逐列清单"),
            ReviewItem(level="阻断", kind="一致性", title="进度条映射与状态表矛盾",
                       quote="待受理-> 待成交", section="状态流转",
                       issue="状态表写「已受理→待成交」", evidence="cBQEws"),
            ReviewItem(level="待确认", kind="证据", title="HKDAP 未定义",
                       quote="USD、HKD 或HKDAP", section="币种"),
        ],
    )


def test_order_items_groups_and_numbers() -> None:
    codes = [(c, it.title) for c, it in order_items(_bundle().items)]
    assert [c for c, _ in codes] == ["B1", "Q1", "S1"]
    assert codes[0][1] == "进度条映射与状态表矛盾"      # 阻断排最前


def test_level_alias_falls_back_to_advice() -> None:
    items = [ReviewItem(level="blocker", title="a"),
             ReviewItem(level="待定", title="b"),
             ReviewItem(level="随便写", title="c")]
    assert [c for c, _ in order_items(items)] == ["B1", "Q1", "S1"]


def test_verdict_requires_both_zero() -> None:
    only_advice = ReviewBundle(items=[ReviewItem(level="建议", title="x")])
    assert verdict_text(only_advice).startswith("✅")
    b = _bundle()
    line = verdict_text(b)
    assert line.startswith("⛔") and "阻断 1 条（B1）" in line and "待确认 1 项（Q1）" in line


def test_card_keeps_issue_quote_and_evidence() -> None:
    card = build_review_card(_bundle(), actions=False)
    text = str(card)
    for needle in ("进度条映射与状态表矛盾", "待受理-> 待成交", "证据：cBQEws",
                   "【阻断】", "【待确认】", "【建议】"):
        assert needle in text, needle
    assert "build_action_row" not in text


def test_code_sits_above_label_and_locate_link() -> None:
    """编号排左列**第一行**：压在枚举词与「定位原文」下面时扫读找不到它。"""
    from app.domains.prd_watch.analysis_card import grid_row
    from app.domains.prd_watch.render import card_text as render_text

    row = grid_row("阻断·一致性", 1, ["正文"],
                   jump_url="https://x.feishu.cn/docx/abc#blk", code="B1")
    text = render_text({"body": {"elements": [row]}})
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert lines[0].endswith("B1") or "B1" in lines[0], lines[:3]
    assert any("定位原文" in ln for ln in lines)
    # 顺序固定：编号 → 枚举词 → 链接
    assert text.index("B1") < text.index("阻断·一致性") < text.index("定位原文")


def test_no_locate_link_when_unanchored() -> None:
    """锚不上就不给链接（不猜块），左列也不留一行空灰字。"""
    from app.domains.prd_watch.analysis_card import grid_row
    from app.domains.prd_watch.render import card_text as render_text

    text = render_text({"body": {"elements": [grid_row("建议", 2, ["正文"], code="S2")]}})
    assert "定位原文" not in text
    assert "S2" in text


def test_long_issue_is_not_truncated_mid_argument() -> None:
    """「问题」是一段论证，截到 120 字会砍成半句话，产品看不懂就没法拍板。

    飞书的硬约束是元素数（见 `_PER_CARD`），正文长度不是瓶颈——按
    markdown 自然换行，别用省略号省空间。
    """
    issue = "四个对客状态只写了 2 条映射：" + "「待成交」在全文里既无定义也无映射，" * 8
    card = build_review_card(
        ReviewBundle(title="x", items=[ReviewItem(level="阻断", title="t", issue=issue)]),
        actions=False)
    text = str(card)
    assert issue[-20:] in text, "问题尾部被截掉了"
    assert "…" not in text.split("问题：")[1][: len(issue) + 10]


def test_item_ids_match_rendered_codes() -> None:
    """登记簿编号 === 卡片上显示的编号（含 Story 前缀）。"""
    b = _bundle()
    b.story_id = "7170562988"
    ids = [i.item_id for i in to_analysis_items(b)]
    assert ids == [item_id_for_review("7170562988", c) for c, _ in order_items(b.items)]
    assert ids[0] == "RV-7170562988-B1"
    assert all(i.prd_token == "doxcnTOKEN" for i in to_analysis_items(b))


def test_item_id_without_story_uses_token_prefix() -> None:
    ids = [i.item_id for i in to_analysis_items(_bundle())]
    assert ids[0] == "RV-doxcnTOK-B1"


def test_review_item_comment_carries_level_and_basis() -> None:
    item = to_analysis_items(_bundle())[0]
    body = item.comment_body()
    assert "阻断" in body and "【凭什么】" in body
    assert "PRD 评审（" in body


def test_many_items_are_paged_not_one_card() -> None:
    """超一页就切多张：飞书单卡元素数有硬上限，超了整卡被拒（实测 230099）。

    这条钉的是「发出去」而不是「渲染出来」——之前 8 条一份时工具回执写「已发」，
    群里却什么都没有，因为 HTTP 出口把 400 吞成了空串。

    条数**不是固定 6**：短结论按真实水位试放能多放一条（见
    `test_short_items_pack_more_per_page`），所以这里只钉「切成多张 + 编号连续」。
    """
    items = [ReviewItem(level="建议", title=f"结论 {i}") for i in range(14)]
    cards = build_review_cards(ReviewBundle(title="x", items=items), actions=True)
    assert len(cards) >= 2, "14 条不能挤在一张卡里"
    assert "第 1/" in str(cards[0]["header"])
    assert f"第 {len(cards)}/{len(cards)} 页" in str(cards[-1]["header"])
    # 编号连续，不因为切页重排
    assert "S1" in str(cards[0]) and "S14" in str(cards[-1])


def test_short_items_pack_more_per_page() -> None:
    """短结论按真实水位多放：6 是保守下限，不是硬上限。

    否则一份长结论会把这个值永久写死——同样的 8 条短结论被切成两张卡，
    而实测它们一张就发得出去。
    """
    items = [ReviewItem(level="建议", title=f"结论 {i}") for i in range(8)]
    cards = build_review_cards(ReviewBundle(title="x", items=items), actions=True)
    assert len(cards) == 1
    assert "页" not in str(cards[0]["header"])


def test_long_items_fall_back_to_conservative_page_size() -> None:
    """长字段（问题/建议各上千字）时退回保守切页：体积超了照样被整卡拒收。"""
    long_issue = "映射不完整：" + "客户端与中台状态对不上，" * 30
    items = [ReviewItem(level="建议", title=f"结论 {i}", issue=long_issue) for i in range(9)]
    cards = build_review_cards(ReviewBundle(title="x", items=items), actions=True)
    assert len(cards) >= 2
    for card in cards:
        assert len(card["body"]["elements"]) <= 23
        assert len(json.dumps(card, ensure_ascii=False).encode()) <= 28_000


def test_single_page_has_no_page_label() -> None:
    cards = build_review_cards(_bundle(), actions=False)
    assert len(cards) == 1
    assert "页" not in str(cards[0]["header"])


def test_compact_action_row_drops_fix_button() -> None:
    """评审卡少一列按钮：元素数预算优先，修正属于低频动作。"""
    full = str(build_action_row("RV-x-B1"))
    compact = str(build_action_row("RV-x-B1", compact=True))
    assert "修正" in full and "修正" not in compact
    for label in ("采纳", "忽略", "有帮助", "判错了"):
        assert label in compact
