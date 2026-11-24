"""日报卡片 —— 结构固定、每条可点。

守的是用户看到的东西：
1. 要点头写日期（`技术日报要点 · 2026-11-20`），不是一句「今日已获取」；
2. 条目按 API 板块分区，顺序不重排（API 顺序就是编辑的意思，不是排行榜）；
3. 「动态」与「核心事项」每一条标题都带链接，缺 `links.aihot` 时回落 `links.original`，
   两个都空才退成纯文本——链接是这张卡的主要用途，不是可选装饰；
4. 超量显式说「另有 N 条」，不静默截断。
"""

from __future__ import annotations

import json

from app.domains.daily.card import (
    MAX_FLASHES,
    MAX_ITEMS_PER_SECTION,
    build_daily_card,
    collect_links,
    render_card_markdown,
)


def _report(**over) -> dict:
    base = {
        "date": "2026-11-20",
        "links": {"aihot": "https://aihot.news/daily/2026-11-20"},
        "lead": {"title": "Anthropic 登陆纳斯达克", "leadParagraph": "开盘价高出发行价 33%。"},
        "sections": [
            {
                "label": "产品发布/更新",
                "items": [
                    {"title": "Mac 版 Gemini 上线计算机操作",
                     "summary": "可跨应用代用户执行任务。",
                     "source": {"name": "X：Josh Woodward"},
                     "links": {"aihot": "https://aihot.news/items/a"}},
                ],
            },
            {
                "label": "行业动态",
                "items": [
                    {"title": "Anthropic 登陆纳斯达克",
                     "source": {"name": "TechCrunch"},
                     "links": {"aihot": "https://aihot.news/items/b"}},
                    {"title": "芯片出口管制收紧",
                     "links": {"original": "https://the-decoder.com/x"}},
                    {"title": "无链接条目"},
                ],
            },
        ],
        "flashes": [
            {"title": "Gemini 4 Neon 实测",
             "links": {"aihot": "https://aihot.news/items/c"}},
        ],
    }
    base.update(over)
    return base


def _all_markdown(card: dict) -> str:
    return "\n".join(
        e.get("content", "") for e in card["body"]["elements"] if e.get("tag") == "markdown"
    )


def test_header_shows_date_not_a_meta_sentence():
    card = build_daily_card(_report())
    header = card["header"]
    assert header["title"]["content"] == "技术日报要点 · 2026-11-20"
    assert "共 4 条" in header["subtitle"]["content"]


def test_every_item_title_is_a_link():
    card = build_daily_card(_report())
    body = _all_markdown(card)
    assert "[Mac 版 Gemini 上线计算机操作](https://aihot.news/items/a)" in body
    assert "[Anthropic 登陆纳斯达克](https://aihot.news/items/b)" in body
    # 站内链接缺失时回落原文链接，而不是留一条死链或直接丢链接
    assert "[芯片出口管制收紧](https://the-decoder.com/x)" in body
    # 两个链接都空才退纯文本
    assert "**无链接条目**" in body
    assert "http" not in body.split("4. **无链接条目**")[1].split("\n")[0]


def test_sections_keep_api_order_and_flashes_separate():
    blocks = render_card_markdown(_report())
    joined = "\n".join(blocks)
    assert joined.index("产品发布/更新") < joined.index("行业动态") < joined.index("快讯")
    # 序号跨板块连续：用户能说「第 4 条」而不必先说是哪个板块的
    assert "1. " in joined and "4. **无链接条目**" in joined


def test_lead_is_clipped_to_one_sentence():
    long_lead = "开盘价高出发行价 33%。" * 30
    card = build_daily_card(_report(lead={"leadParagraph": long_lead}))
    first = card["body"]["elements"][0]["content"]
    assert first == "**开盘价高出发行价 33%。**"


def test_overflow_says_how_many_left_instead_of_truncating():
    many = {"label": "行业动态",
            "items": [{"title": f"条目{i}"} for i in range(MAX_ITEMS_PER_SECTION + 3)]}
    body = _all_markdown(build_daily_card(_report(sections=[many], flashes=[])))
    assert "另有 3 条未在卡片展开" in body
    assert f"{MAX_ITEMS_PER_SECTION}. **条目{MAX_ITEMS_PER_SECTION - 1}**" in body
    assert f"条目{MAX_ITEMS_PER_SECTION}**" not in body


def test_flashes_capped_and_counted():
    flashes = [{"title": f"快讯{i}"} for i in range(MAX_FLASHES + 2)]
    body = _all_markdown(build_daily_card(_report(flashes=flashes)))
    assert "另有 2 条快讯" in body


def test_card_has_daily_page_button_only_when_link_exists():
    card = build_daily_card(_report())
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    assert buttons and buttons[0]["url"] == "https://aihot.news/daily/2026-11-20"

    card = build_daily_card(_report(links={}))
    assert not [e for e in card["body"]["elements"] if e.get("tag") == "button"]


def test_card_stays_under_volume_limit_and_links_are_collectable():
    card = build_daily_card(_report())
    size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    assert size < 28000
    assert collect_links(_report()) == [
        "https://aihot.news/items/a",
        "https://aihot.news/items/b",
        "https://the-decoder.com/x",
        "https://aihot.news/items/c",
    ]
