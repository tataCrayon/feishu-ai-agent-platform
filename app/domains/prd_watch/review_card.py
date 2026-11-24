"""PRD 评审卡片 — 把 Skill 判出来的结论渲染成与「变更提醒卡」同一套表格。

为什么单独一张卡，而不是复用 `analysis_card.build_story_card`：

| | 需求分析卡 | 评审卡 |
|---|---|---|
| 结论从哪来 | `analysis.py` 的机械自检（数量/删除线/术语…） | 执行面按 `prd-review` 系列 Skill 判出来的四维结论 |
| 需要什么 | Story 链接（要 Story 才出得来） | 一篇 PRD 就够；Story 可有可无 |
| 分级 | 无（都是自检项） | 阻断 / 待确认 / 建议 |

把判断塞进 Python 会让「怎么评审」这件事跟着代码走，改一次口径要发一次版；
所以这里只做**渲染 + 登记**：级别、标题、原文、建议全由调用方（执行面）给，
本模块不产出任何结论，缺字段就少渲染一行，不替它补。

登记进 `AnalysisFeedback` 后，卡片上的「采纳 / 修正 / 忽略 / 🙂🙃」与需求分析卡
完全同一条链路：采纳 = 在原文所在块写一条 PRD 评论，不改正文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.domains.prd_watch.analysis_card import (
    _LEFT_COL_WIDTH_LOCATE,
    clip,
    grid_header,
    grid_row,
    plain,
)

# 级别 → 行首编号字母。同一份 PRD 的结论按 阻断 → 待确认 → 建议 排列：
# 挡住开发的排最前，产品扫一眼就知道先改哪几条。
_LEVELS: tuple[tuple[str, str], ...] = (
    ("阻断", "B"),
    ("待确认", "Q"),
    ("建议", "S"),
)
_LEVEL_HINT = {
    "阻断": "不改就没法开发",
    "待确认": "PRD 没写清、也没取证到，开发会来回问人",
    "建议": "能开发，但会返工",
}
_MAX_ITEMS = 24
# 单条结论各字段的字数上限。飞书的硬约束是**元素数**（见 `_PER_CARD`），
# 正文长度只受卡片体积（约 30KB）限制，所以这里给得比分析卡宽得多：
# 评审结论的「问题」本身就是一段论证（哪条规则和哪条冲突、冲突后果是什么），
# 截到 120 字会把结论砍成半句话，产品看不懂就没法拍板（实测截图：问题列
# 结尾是「…」，读者要点开原文才知道在说什么）。字段按「够读完一条论证」取值，
# 由 markdown 自然换行，不靠省略号省空间。
_QUOTE_MAX = 200
_SUGGEST_MAX = 300
_ISSUE_MAX = 320
_TITLE_MAX = 60

# 单张卡片的条数下限。飞书卡片有**元素数硬上限**（实测：顶层元素超过 25 个、
# 或带按钮的条目堆到 10 条就以 `230099 / ErrCode 11310 element exceeds the limit`
# 整卡拒收——拒收时整张卡什么都不留，用户在群里只看到一段正文，看不出是被拒了）。
# 一条结论 = 行网格 + 一排四个按钮，所以按 6 条切页，留足余量。
#
# 但 6 是**保守下限**，不是硬上限：实际能不能多放一条取决于这一份结论的字段有多长
# （短结论 8 条也发得出去，实测 28KB 仍 OK）。所以切页时按真实渲染结果逐条试放
# （`_fits_one_card`），放得下就多放——不然一份长结论会把这个保守值永久写死，
# 短结论也跟着被切成两张。
_PER_CARD = 6
_PAGE_SIZE_MAX = 9
# 实测上限留一成余量：顶层元素 25 个、卡片 JSON 约 30KB 起被拒。
# 23 是实测过线：8 条短结论（22 元素）一张就发得出去，加上新的指令脚注仍在上限内。
_MAX_TOP_ELEMENTS = 23
_MAX_CARD_BYTES = 28_000

# 级别字段的口径不统一（模型可能写 blocker / 待定 / 建议改），归一到这里三种，
# 归不进去的一律按「建议」——低估严重度好过高估，误标阻断会让卡片失去可信度。
_LEVEL_ALIASES = {
    "阻断": "阻断", "阻塞": "阻断", "blocker": "阻断", "blocking": "阻断",
    "must": "阻断", "p0": "阻断", "高": "阻断",
    "待确认": "待确认", "待定": "待确认", "pending": "待确认", "tbd": "待确认",
    "open": "待确认", "question": "待确认",
    "建议": "建议", "suggestion": "建议", "should": "建议", "minor": "建议",
    "优化": "建议", "低": "建议",
}


@dataclass
class ReviewItem:
    """一条评审结论。字段全部由执行面给，本模块只负责排版。"""

    level: str = "建议"
    kind: str = ""            # 判定维度：一致性 / 可执行性 / 验收 / 证据
    title: str = ""
    quote: str = ""           # PRD 原文逐字引用
    section: str = ""         # 原文所在章节
    issue: str = ""           # 冲突点/缺口（含代码出处）
    suggestion: str = ""      # 该怎么改
    evidence: str = ""        # 文件:行号 / 另一篇文档
    block_id: str = ""        # 锚到的块（采纳时评论落在这里）
    jump_url: str = ""


@dataclass
class ReviewBundle:
    """一张评审卡片的全部素材。"""

    title: str = ""
    prd_url: str = ""
    prd_token: str = ""
    story_id: str = ""
    items: list[ReviewItem] = field(default_factory=list)
    note: str = ""            # 脚注：哪几项没取证 / 读不到什么

    @property
    def blocking(self) -> list[tuple[str, ReviewItem]]:
        return [(code, it) for code, it in order_items(self.items) if _level(it) == "阻断"]

    @property
    def pending(self) -> list[tuple[str, ReviewItem]]:
        return [(code, it) for code, it in order_items(self.items) if _level(it) == "待确认"]

    @property
    def advices(self) -> list[tuple[str, ReviewItem]]:
        return [(code, it) for code, it in order_items(self.items) if _level(it) == "建议"]


def _level(item: ReviewItem) -> str:
    raw = str(item.level or "").strip()
    hit = _LEVEL_ALIASES.get(raw) or _LEVEL_ALIASES.get(raw.lower())
    return hit or "建议"


def order_items(items: list[ReviewItem]) -> list[tuple[str, ReviewItem]]:
    """编号与排序（阻断 B1… / 待确认 Q1… / 建议 S1…），**卡片与登记簿共用**。

    两边各算一次编号会漂移：卡片上写 B2、点「采纳」写进评论的却是 B3，
    评论区和群里就对不上是哪条了。
    """
    out: list[tuple[str, ReviewItem]] = []
    for level, letter in _LEVELS:
        seq = 0
        for item in items[:_MAX_ITEMS]:
            if _level(item) != level:
                continue
            seq += 1
            out.append((f"{letter}{seq}", item))
    return out


def verdict_text(bundle: ReviewBundle) -> str:
    """结尾判定行。判据与 `prd-review` Skill 一致：阻断 0 且待确认 0 才算过关。"""
    blocking = bundle.blocking
    pending = bundle.pending
    if not blocking and not pending:
        return "✅ 可进入开发：阻断项 0、待确认 0"
    bits: list[str] = []
    if blocking:
        bits.append(f"阻断 {len(blocking)} 条（{'、'.join(c for c, _ in blocking)}）")
    if pending:
        bits.append(f"待确认 {len(pending)} 项（{'、'.join(c for c, _ in pending)}）")
    return "⛔ 未就绪：" + "；".join(bits)


def item_id_for_review(key: str, code: str) -> str:
    """`RV-<文档key>-<B1>`：与讨论变更 `CR-`、需求分析 `AN-` 分开，统计能分开算。"""
    return f"RV-{key}-{code}"


def build_review_cards(bundle: ReviewBundle, *, actions: bool = True) -> list[dict[str, Any]]:
    """结论多于一页时**切成多张卡**（飞书单卡元素数有硬上限，超了整卡被拒）。

    切页按结论顺序连续切，不按级别拆：级别已经用编号区分（B/Q/S），再按级别
    分卡会让「阻断 1 张、待确认 3 张」这种碎片化结果出现在群里。

    上限是「拿**真实水位**再算」的：`_PER_CARD` 是保守默认值，实际能不能多放一条
    取决于这一份结论的字段有多长——`_fits_one_card` 按渲染出来的体积与元素数
    逐条试放，放得下就多放，放不下就退回默认值。这样短结论不会因为一份长结论
    的保守值被切成两张，长结论也不会被塞爆。
    """
    ordered = order_items(bundle.items)
    per_page = _page_size(bundle, ordered, actions=actions)
    if len(ordered) <= per_page:
        # 一张装得下就不写页码：`第 1/1 页` 只是在提醒读者「只有一张」，
        # 反而像漏发了后面的。
        return [build_review_card(bundle, actions=actions)]

    pages = [ordered[i:i + per_page] for i in range(0, len(ordered), per_page)]
    out: list[dict[str, Any]] = []
    for index, page in enumerate(pages, start=1):
        out.append(build_review_card(
            bundle, actions=actions, page=page,
            page_label=f"第 {index}/{len(pages)} 页"))
    return out


def _page_size(bundle: ReviewBundle, ordered: list[tuple[str, ReviewItem]],
               *, actions: bool) -> int:
    """在不超限的前提下每页能放几条（下限 `_PER_CARD`，上限 `_PAGE_SIZE_MAX`）。"""
    for size in range(min(len(ordered), _PAGE_SIZE_MAX), _PER_CARD - 1, -1):
        card = build_review_card(bundle, actions=actions, page=ordered[:size])
        if _fits_one_card(card):
            return size
    return _PER_CARD


def _fits_one_card(card: dict[str, Any]) -> bool:
    """按飞书的两个实测上限判这张卡发不发得出去。

    ① 元素数：带按钮的条目堆到 10 条会被整卡拒收（`230099 / 11310`）；
    ② 体积：卡片 JSON 约 30KB 以上同样被拒——长结论的 `问题/建议` 可以上千字，
       只数条目数会在这类卡片上翻车。
    两条都要过；判据取实测值再留一成余量。
    """
    body = card.get("body")
    elements = body.get("elements") if isinstance(body, dict) else None
    if not isinstance(elements, list) or len(elements) > _MAX_TOP_ELEMENTS:
        return False
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= _MAX_CARD_BYTES


def build_review_card(bundle: ReviewBundle, *, actions: bool = True,
                      page: list[tuple[str, ReviewItem]] | None = None,
                      page_label: str = "") -> dict[str, Any]:
    """渲染一张评审卡片：判定行 → 分级表格（逐条按钮） → 脚注。

    `actions=False` 只给单测与预览用：没有登记簿时按钮点不动，不如不给。
    `page` 只渲染这几条（多页时由 `build_review_cards` 切好传入）；
    `page_label` 写在标题后面，让人一眼知道后面还有几张。
    """
    from lark_channel import CardBuilder

    b = CardBuilder()
    b.config(width_mode="fill")
    title = plain(bundle.title or "未命名 PRD")
    head = f"🧭 PRD 评审 · {title[:26]}"
    blocking, pending = len(bundle.blocking), len(bundle.pending)
    if bundle.items:
        head += f"（阻断 {blocking} / 待确认 {pending}）"
    if page_label:
        head += f" · {page_label}"
    b.header(head)

    meta = [f"**{title}**" + (f" · #{bundle.story_id}" if bundle.story_id else "")]
    docs = [f"PRD [查看]({bundle.prd_url})" if bundle.prd_url else "PRD <font color='grey'>无链接</font>"]
    meta.append("｜".join(docs))
    b.markdown("\n".join(meta))
    b.markdown(verdict_text(bundle))

    key = _doc_key(bundle)
    ordered = page if page is not None else order_items(bundle.items)
    for level, hint in _LEVEL_HINT.items():
        rows = [(c, it) for c, it in ordered if _level(it) == level]
        if not rows:
            continue
        b.raw({"tag": "hr"})
        head_line = f"**【{level}】** {len(rows)} 条\n<font color='grey'>{hint}</font>"
        b.markdown(head_line)
        b.raw(grid_header(width=_LEFT_COL_WIDTH_LOCATE))
        for code, item in rows:
            label = level if not item.kind else f"{level}·{clip(item.kind, 4)}"
            b.raw(grid_row(label, int(code[1:]) if code[1:].isdigit() else 0,
                           _row_lines(item), jump_url=item.jump_url, code=code,
                           width=_LEFT_COL_WIDTH_LOCATE))
            if actions:
                from app.domains.prd_watch.analysis_card import build_action_row

                # compact：去掉「修正」一列。飞书单卡元素数有硬上限，
                # 少一列按钮就多放得下一条结论（修正走回执里的文字流程）。
                b.raw(build_action_row(item_id_for_review(key, code), compact=True))

    if not bundle.items:
        b.markdown("<font color='grey'>没有任何结论 —— 正文与表格都没读到？"
                   "请先确认 PRD 链接可读再评一次。</font>")
    if bundle.note:
        b.markdown(f"<font color='grey'>{bundle.note}</font>")
    b.markdown("<font color='grey'>判定口径：开发拿这份 PRD 就能写代码，不用再问任何人。"
               "按钮只写评论/记评分，不改正文。</font>")
    b.markdown(plain_commands(bundle))
    return dict(b.to_dict())


def plain_commands(bundle: ReviewBundle) -> str:
    """卡片脚注里的**文字指令**：转发过来的卡片按钮点不动时的退路。

    飞书把转发来的消息当只读，按钮点了只弹「这是转发消息，无法直接操作」——
    那是客户端语义，应用侧改不了。但编号是可见的，人把「采纳 B1」抄回自己的
    会话，走的是与按钮完全相同的副作用。编号只列本页的，避免一列二十个。
    """
    codes = [code for code, _ in order_items(bundle.items)][:4]
    shown = "、".join(codes)
    tail = " 等" if len(bundle.items) > len(codes) else ""
    return ("<font color='grey'>卡片转发后按钮点不动时，直接回一句指令即可："
            f"采纳 / 忽略 / 有用 / 判错 + 编号（本卡编号 {shown}{tail}）。"
            "动词可省写成「B1 采纳」，效果与点按钮一致。</font>")


def _row_lines(item: ReviewItem) -> list[str]:
    lines = [f"**{clip(item.title, _TITLE_MAX)}**"]
    if item.issue:
        lines.append(f"<font color='grey'>问题：</font>{clip(item.issue, _ISSUE_MAX)}")
    if item.suggestion:
        lines.append(f"<font color='grey'>建议：</font>{clip(item.suggestion, _SUGGEST_MAX)}")
    if item.quote:
        lines.append(f"<font color='grey'>原文：</font>{clip(item.quote, _QUOTE_MAX)}")
    where = item.section or "未定位章节"
    tail = f"<font color='grey'>位置：{where}"
    if item.evidence:
        tail += f"｜证据：{clip(item.evidence, 60)}"
    lines.append(tail + "</font>")
    return lines


def _doc_key(bundle: ReviewBundle) -> str:
    """条目编号里的文档部分：优先 Story ID，其次 token 前 8 位，都没有就用定值。"""
    if bundle.story_id:
        return str(bundle.story_id)
    if bundle.prd_token:
        return str(bundle.prd_token)[:8]
    return "PRD"


def to_analysis_items(bundle: ReviewBundle) -> list[Any]:
    """卡片条目 ↔ 登记簿条目（采纳/修正/评分走同一条 AN/RV 链路）。"""
    from app.domains.prd_watch.analysis_feedback import AnalysisItem

    key = _doc_key(bundle)
    out: list[AnalysisItem] = []
    for code, item in order_items(bundle.items):
        out.append(AnalysisItem(
            item_id=item_id_for_review(key, code),
            story_id=str(bundle.story_id or ""),
            story_title=str(bundle.title or ""),
            kind=str(item.kind or ""),
            level=_level(item),
            title=str(item.title or ""),
            quote=str(item.quote or ""),
            suggestion=str(item.suggestion or ""),
            block_id=str(item.block_id or ""),
            section=str(item.section or ""),
            prd_token=str(bundle.prd_token or ""),
            prd_url=str(bundle.prd_url or ""),
            # 「凭什么」：评审结论的依据不是机械自检，是判定维度 + 取到的证据
            basis=("PRD 评审（" + "、".join(x for x in (item.kind, item.evidence) if x) + "）")
            if (item.kind or item.evidence) else "",
        ))
    return out
