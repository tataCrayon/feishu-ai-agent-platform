"""easyprd 渲染单测 — 覆盖设计稿 §4.3 的三种边界与编号格式。

跑法：python -m pytest tests/domains/prd_watch/test_render.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL, ChangePoint
from app.domains.prd_watch.render import (
    CardItem,
    StoryMeta,
    build_decision_card,
    build_notice_card,
    card_text,
    comment_primary_link,
    make_cr_id,
    render_comment,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TZ = timezone(timedelta(hours=8))

BASE = dict(cr_id="CR-20261030-8821-001", story_title="高净值客户申购额度调整",
            story_id="8821", source="产品评审会", occurred_at="2026-10-30 14:00")


def _amount_point(band: str = BAND_NORMAL) -> ChangePoint:
    return ChangePoint(
        title="申购上限 5万→20万",
        current_text="单笔申购上限为人民币5万元，单日累计不超过10万元。",
        suggested_text="单笔申购上限为人民币20万元，单日累计不超过50万元。",
        change_type="参数调整", modules=["申购", "额度"], compliance="需合规确认",
        evidence_quote="之前5万的限额太紧了，\n高净值客户反馈不够用。",
        speaker="张三", scenario="产品评审会",
        rationale="客户侧反馈 5 万额度无法满足 VIP 客户",
        confidence=0.92, band=band, block_id="doxcnAAA",
    )


def test_cr_id_format() -> None:
    got = make_cr_id("8821", 1, datetime(2026, 10, 30, 14, 0, tzinfo=TZ))
    assert got == "CR-20261030-8821-001", got
    assert make_cr_id("8821", 12).endswith("-012")  # 序号补零


def test_normal_comment_has_all_sections() -> None:
    text = render_comment(_amount_point(), **BASE)
    for key in ("【变更依据】", "【改什么】", "【凭什么】", "【谁来定】",
                "当前PRD原文", "建议调整为", "采纳", "忽略", "调整："):
        assert key in text, key
    # 第一行就说清是谁写的：评论不显示机器人标记，不写就会被当成提需求的人写的
    assert text.splitlines()[0].startswith("🤖 Bot EasyPRD 自动评论 · CR-")
    assert "未经人工确认，不作为定论" in text
    # 原文逐字引用（一字不改）
    assert "  单笔申购上限为人民币5万元，单日累计不超过10万元。" in text
    # 依据引用压成一行（换行会打断「——说话人」的归属），前后半句都还在
    assert "之前5万的限额太紧了" in text and "高净值客户反馈不够用" in text
    quoted = [ln for ln in text.splitlines() if ln.strip().startswith("「")]
    assert len(quoted) == 1, quoted
    assert "置信度较低" not in text


def test_comment_body_has_no_markdown() -> None:
    """评论正文不渲染 markdown：`**x**` 与 `[字](url)` 会原样显示成星号和括号。"""
    text = render_comment(_amount_point(), **BASE,
                          discussion_url="https://x/1", story_url="https://y/2")
    assert "**" not in text
    assert "](http" not in text
    assert "<font" not in text


def test_new_clause_boundary() -> None:
    cp = ChangePoint(title="新增VIP风险提示条款", is_new_clause=True,
                     change_type="新增条款", suggested_text="面向VIP客户需追加风险揭示。",
                     confidence=0.9, band=BAND_NORMAL)
    text = render_comment(cp, **BASE)
    assert "（未在PRD中定位到对应段落，此为建议新增）" in text


def test_delete_boundary() -> None:
    cp = _amount_point()
    cp.change_type = "删除条款"
    cp.suggested_text = "随便写点什么，不该出现在删除条里"
    text = render_comment(cp, **BASE)
    assert "【建议删除本段】" in text
    assert "随便写点什么" not in text  # 删除条款固定占位，不用模型话术


def test_low_confidence_flagged() -> None:
    text = render_comment(_amount_point(band=BAND_FLAGGED), **BASE)
    assert "⚠️ 此条由 AI 自动识别" in text


def test_links_listed_as_plain_text_and_pickable() -> None:
    text = render_comment(_amount_point(), discussion_url="https://x/1",
                          story_url="https://y/2", **BASE)
    assert "https://x/1" in text
    # 只给一个可点链接：优先原始讨论（Story 链接其次）
    assert comment_primary_link("https://x/1", "https://y/2") == "https://x/1"
    assert comment_primary_link("", "https://y/2") == "https://y/2"


def _item(story_id: str = "8821", title: str = "高净值客户申购额度调整",
          point: ChangePoint | None = None, cr_id: str = "CR-20261030-8821-001",
          prd_url: str = "https://example.feishu.cn/docx/PRD8821",
          space: str = "星路产研需求管理（产研专用）") -> CardItem:
    return CardItem(story=StoryMeta(story_id=story_id, story_title=title,
                                    story_url=f"https://project.feishu.cn/pk/story/detail/{story_id}",
                                    space_name=space, status="4-待评审"),
                    prd_url=prd_url, cr_id=cr_id, point=point or _amount_point())


def test_card_three_sections_as_specified() -> None:
    """版式：① 变更依据（带链接）② 表格(Story/PRD/diff) ③ 待确认人。"""
    p2 = _amount_point()
    p2.title, p2.change_type, p2.compliance = "新增VIP风险提示条款", "新增条款", "无"
    items = [_item(), _item(story_id="9900", title="RWA赎回下单",
                            point=p2, cr_id="CR-20261030-9900-001",
                            prd_url="https://x.feishu.cn/docx/PRD9900")]
    card = build_notice_card(items, source="会议（智能纪要）· WebPortal RWA接口对齐",
                             source_url="https://applink.feishu.cn/client/chat/open?openChatId=oc_1",
                             at_open_ids=["ou_prod", "ou_dev"], actions=False)

    assert card["schema"] == "2.0"
    # header 里同时给条数与 Story 数（多 Story 是常态，只说"5 条"看不出涉及几篇文档）
    assert "📋 PRD 变更建议提醒（2 条 · 2 个 Story）" in card_text(card)

    # ① 变更依据必须带可点回原讨论的链接
    body = card_text(card)
    assert "**变更依据：** 会议（智能纪要）· WebPortal RWA接口对齐" in body
    assert "[查看原始讨论](https://applink.feishu.cn/client/chat/open?openChatId=oc_1)" in body

    # ② 变更表 = column_set 行网格（原生 table 单元格放不了按钮，实测 HTTP 400），
    #    最后一列「操作」跟随每行
    header = next(e for e in card["body"]["elements"]
                  if e.get("tag") == "column_set" and e.get("background_style") == "grey")
    names = [c["elements"][0]["content"].strip("*") for c in header["columns"]]
    assert names == ["Story", "PRD", "变更项"]          # diff 行自带标签，不占表头列
    rows = [e for e in card["body"]["elements"]
            if e.get("tag") == "column_set" and e is not header]
    # 一条变更 = 两行网格（信息行 + diff 行）+ 独立按钮行，操作不挤在信息行里
    assert len(rows) == 4, "两条变更 × 两行"
    cells = ["".join(el.get("content", "") for col in r["columns"]
                     for el in col["elements"] if el.get("tag") == "markdown")
             for r in rows]
    # 左右对照：diff 行两列并排，各带自己的灰色标签
    assert "变更前" in cells[1] and "变更后" in cells[1]
    # Story 列给**Story 名称**（可点），#ID 退到第二行灰字
    assert "[高净值客户申购额度调整](https://project.feishu.cn/pk/story/detail/8821)" in cells[0]
    assert "#8821" in cells[0] and "4-待评审" in cells[0]
    assert "[RWA赎回下单](https://project.feishu.cn/pk/story/detail/9900)" in cells[2]
    # PRD 列可点，文本用文档标题；没文档标题时退回 Story 标题
    assert "[RWA赎回下单](https://x.feishu.cn/docx/PRD9900)" in cells[2]
    assert "[高净值客户申购额度调整](https://example.feishu.cn/docx/PRD8821)" in cells[0]
    assert "单笔申购上限为人民币5万元" in cells[1]          # 变更前
    assert "单笔申购上限为人民币20万元" in cells[1]         # 变更后
    assert "CR-20261030-8821-001" in cells[0] and "合规" in cells[0]
    assert "新增条款" in cells[2]


def test_prd_column_prefers_document_title() -> None:
    """PRD 列优先用文档标题：三列都写 Story 标题时，看不出该点哪个。"""
    item = _item()
    item.story.prd_title = "WebPortal RWA 申购下单 PRD"
    flat = card_text(build_notice_card([item], source="群讨论"))
    assert "[WebPortal RWA 申购下单 PRD](" in flat


def test_card_points_to_comment_as_decision_entry() -> None:
    """卡片必须写明「去 PRD 评论里回复确认」——那是决策闭环的唯一入口。

    这里**没有** dry_run 分支：干跑阶段 watcher 根本不发卡片（_run 在 dry_run
    就 return 了），留一个永不为 True 的开关就是腐烂的起点。
    """
    card = build_notice_card([_item()], source="群讨论")
    body = card_text(card)
    assert "也可在 PRD 评论里回复「采纳 / 忽略 / 调整：…」确认" in body


def test_card_without_meegle_meta_degrades_not_fabricates() -> None:
    """没接 Meegle 时空间名/状态/链接都没有：只留 #ID，不编一个假空间名或假 PRD 链接。"""
    item = _item(space="", prd_url="")
    item.story.status = ""
    item.story.story_url = ""
    body = card_text(build_notice_card([item], source="群讨论"))
    assert "#8821" in body and "星路" not in body
    # 名称有、链接没有 → 名字照显（纯文本），绝不编一个 URL
    assert "高净值客户申购额度调整" in body
    assert "[高净值客户申购额度调整](" not in body
    assert "（无 PRD 链接）" in body                 # 明说没有，而不是留白让人以为漏看
    assert "4-待评审" not in body
    # Story 没链接时标题就是纯文本，不能编一个 URL
    assert "](https://" not in body.split("变更依据")[1].split("注：")[0]


def test_card_source_without_url_still_renders() -> None:
    """来源没有链接（妙记没权限、台账没记）时不能渲染出空括号链接。"""
    body = card_text(build_notice_card([_item()], source="群讨论", source_url=""))
    assert "**变更依据：** 群讨论" in body and "查看原始讨论" not in body


def test_card_rows_capped_with_note() -> None:
    """不分页：飞书 page_size 上限 10，行数也封顶 10，多出的写「另有 N 条」。

    不要分页 —— 一张注定翻页的表等于让人多点两下才能看全。
    """
    items = [_item(cr_id=f"CR-{i:03d}", point=_amount_point()) for i in range(14)]
    card = build_notice_card(items, source="群讨论")
    header = next(e for e in card["body"]["elements"]
                  if e.get("tag") == "column_set" and e.get("background_style") == "grey")
    data_rows = [e for e in card["body"]["elements"]
                 if e.get("tag") == "column_set" and e is not header]
    grids = [e for e in data_rows if e["columns"][0]["elements"][0].get("tag") == "markdown"]
    assert len(grids) == 20, "10 条 × 两行网格；条数本身封顶 10"
    assert "另有 4 条见 PRD 评论" in card_text(card)


def test_decision_card_adopt_says_body_not_changed() -> None:
    card = build_decision_card(decision="adopt", cr_id="CR-1", title="申购上限调整",
                               meta=StoryMeta(story_id="8821", story_title="额度调整",
                                              space_name="示例空间"),
                               confirmer="ou_prod")
    body = "\n".join(str(e.get("content", "")) for e in card["body"]["elements"])
    assert card["header"]["title"]["content"] == "✅ PRD 变更已采纳"
    assert "PRD 正文需人工按评论表述更新" in body
    assert "**确认人：** ou_prod" in body


def test_decision_card_timeout_has_no_confirmer() -> None:
    card = build_decision_card(decision="timeout", cr_id="CR-1", title="t",
                               meta=StoryMeta(story_id="8821"), confirmer="",
                               extra="已挂 3 天未确认")
    body = "\n".join(str(e.get("content", "")) for e in card["body"]["elements"])
    assert "已挂 3 天未确认" in body and "确认人" not in body
    assert "⏰" in card["header"]["title"]["content"]


def test_at_line_dedupes_multi_role_person() -> None:
    """同一人常同时挂「产品/需求提出人/项目」，按角色出名单会 @ 他三遍（实测）。"""
    card = build_notice_card([_item()], source="群讨论",
                             at_open_ids=["ou_a", "ou_a"],
                             at_names=["丁浩", "丁浩", "胡萍"])
    body = card_text(card)
    assert body.count("<at id=ou_a></at>") == 1
    assert body.count("@丁浩") == 1 and "@胡萍" in body


def test_ledger_row_has_decision_state() -> None:
    row = _amount_point().to_ledger_row("CR-20261030-8821-001", "8821", "oc_test")
    assert row["决策状态"] == "pending"
    assert row["CR编号"] == "CR-20261030-8821-001"
    assert "→" in row["变更前后"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✅ {name}")
    print("\n全部通过")


def test_card_has_bulk_action_buttons_by_default() -> None:
    """每条变更后面要能直接点采纳/修正/忽略。

    表格单元格放不下按钮（飞书 table 不支持），所以在表后按 CR 逐条挂按钮组。
    """
    card = build_notice_card([_item(), _item(cr_id="CR-2")], source="群讨论")
    # 按钮**单独一行**跟在各自那条变更下面（挤在信息行里会把该行撑得太长）
    ops = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"
           and e["columns"][0]["elements"][0].get("tag") == "button"]
    assert len(ops) == 2, "每条变更一组按钮"
    for btn_row in ops:
        acts = [c["elements"][0]["value"]["act"] for c in btn_row["columns"]]
        assert acts == ["adopt", "fix", "ignore"]
    # 点按提示要写明状态怎么落（不看文档也知道点完发生什么）
    assert "点右侧按钮即落状态" in card_text(card)


def test_change_cell_shows_section_and_jump_link() -> None:
    """变更项要写明原文所在章节；变更前列的原文要可点跳转到 PRD 对应块。"""
    item = _item()
    item.section = "二、产品规则 > 2.3 可见性"
    item.jump_url = "https://example.feishu.cn/docx/PRD8821#doxcnABC"
    card = build_notice_card([item], source="群讨论", actions=False)
    grids = [e for e in card["body"]["elements"]
             if e.get("tag") == "column_set" and e.get("background_style") != "grey"]
    change = grids[0]["columns"][2]["elements"][0]["content"]
    before = grids[1]["columns"][0]["elements"][0]["content"]
    assert "章节：二、产品规则 > 2.3 可见性" in change
    assert "[定位原文](https://example.feishu.cn/docx/PRD8821#doxcnABC)" in change
    assert "变更前" in before                                  # 行内自带灰标签
    assert "[单笔申购上限为人民币5万元" in before
    assert "(https://example.feishu.cn/docx/PRD8821#doxcnABC)" in before


def test_wider_card_config() -> None:
    """卡片要撑满会话宽度：默认宽度下五列表格每列只剩省略号。"""
    card = build_notice_card([_item()], source="群讨论", actions=False)
    assert card["config"]["width_mode"] == "fill"
