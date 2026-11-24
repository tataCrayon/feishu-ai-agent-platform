"""用真实 PRD 文档打一套自测卡片 — `/easyprd demo <文档链接>` 的落地实现。

为什么要它：卡片按钮（采纳/修正/忽略）只有飞书客户端点得到，回调链路、评论写入、
决策回写三处都得有真人点一遍才算验过。但真实链路要求群↔Story 绑定、Meegle 凭证、
台账建表全都配好——配好之前没有任何合法入口能把那张卡片发出来。

这里只换掉「素材从哪来」这一环，其余全部走生产路径：锚点在真文档上定位、卡片由
`build_notice_card` 渲染、按钮回写用同一套 `CardActionHandler`、评论真写进文档。
验的是**按钮与评论链路**，不是识别质量（变更点是人指定的固定样例）。

边界：自测卡片写的是固定样例文案，来源行标「自测」，不与真实监听混在一起。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.anchor import BlockAnchor
from app.domains.prd_watch.ledger import COLUMNS, Ledger, StoryBinding
from app.domains.prd_watch.models import BAND_NORMAL, ChangePoint
from app.domains.prd_watch.render import (
    CardItem,
    StoryMeta,
    build_notice_card,
    make_cr_id,
)

logger = logging.getLogger("prd_watch.demo")

TZ = timezone(timedelta(hours=8))

# 自测样例：文档里真实存在的三处规则，覆盖「改参数 / 改流程 / 新增条款」三类，
# 用来验证三种卡片形态（有原文可点、有原文无章节、无原文新增）。
_SAMPLES: tuple[dict[str, Any], ...] = (
    {
        "title": "产品类型 Tab 默认项调整",
        "quote": "三个 Tab 依次为「RWA公募基金」「RWA私募基金」「RWA票據」，默认选中第一个",
        "current": "三个 Tab 依次为「RWA公募基金」「RWA私募基金」「RWA票據」，默认选中第一个「RWA公募基金」。",
        "suggested": "三个 Tab 依次为「RWA公募基金」「RWA私募基金」「RWA票據」，默认选中「RWA票據」。",
        "change_type": "流程变更",
        "modules": ["货架", "Tab"],
        "evidence": "客户主要是票据客户，默认应该落在票据上",
    },
    {
        "title": "申购/赎回按钮独立判断补充说明",
        "quote": "申购与赎回两个入口独立判断，互不牵连",
        "current": "申购与赎回两个入口独立判断，互不牵连。",
        "suggested": "申购与赎回两个入口独立判断，互不牵连；任一入口不可用不影响另一入口的展示与操作。",
        "change_type": "参数调整",
        "modules": ["申购", "赎回"],
        "evidence": "之前出现过赎回不可用把申购按钮一起置灰的问题",
    },
    {
        "title": "新增价格展示兜底规则",
        "quote": "",
        "current": "",
        "suggested": "中台产品库未返回「下单价格确定方式」时，货架与详情页均不展示价格，并提示「价格待定」。",
        "change_type": "新增条款",
        "modules": ["价格"],
        "evidence": "新品上架时价格字段常延迟，页面会显示空白",
    },
)


def _points() -> list[ChangePoint]:
    out: list[ChangePoint] = []
    for s in _SAMPLES:
        out.append(ChangePoint(
            title=s["title"], quote_text=s["quote"], current_text=s["current"],
            suggested_text=s["suggested"], change_type=s["change_type"],
            modules=list(s["modules"]), compliance="无",
            evidence_quote=s["evidence"], speaker="自测样例",
            scenario="自测（非真实讨论）", rationale="验证卡片按钮与评论写入链路",
            confidence=0.95, band=BAND_NORMAL, is_new_clause=not s["current"]))
    return out


async def run_demo(
    doc: str, *, chat_id: str, ledger: Ledger, config: PrdWatchConfig,
    anchor: BlockAnchor | None = None, prd_url: str = "",
) -> tuple[dict[str, Any], list[ChangePoint], StoryBinding]:
    """在指定 PRD 上出一张自测卡片 → (卡片, 变更点, 绑定)。

    `prd_url` 由调用方从**用户给的链接**透传（wiki 链接原样可用），不在这里拼——
    token 拼 URL 打不开，而卡片上打不开的链接比没有链接更糟。用户只给 token 时
    就留空，PRD 列写明「无 PRD 链接」。

    评论**不在这里写**：demo 只负责把卡片发出去给人点。点「采纳」时才按生产路径
    写回复评论，只有「修正」会新写一条评论——这样误点也不会在文档里留垃圾。
    """
    lark_anchor = anchor or BlockAnchor()
    title = ""
    try:
        title = await lark_anchor.title(doc)
    except Exception as e:
        logger.info("[prd_watch] demo 取文档标题失败: %s", str(e)[:100])
    base_url = (prd_url or "").split("#")[0]
    binding = StoryBinding(
        story_id="DEMO", story_title=title or "PRD 自测文档", prd_token=doc,
        chat_id=chat_id, prd_url=base_url,
        source="demo", comment_id="", source_ref="demo")

    stamp = datetime.now(TZ)
    items: list[CardItem] = []
    points = _points()
    for i, cp in enumerate(points, start=1):
        cr_id = make_cr_id("DEMO", i, stamp)
        cp.cr_id = cr_id
        cp.story_id = binding.story_id
        try:
            cp.block_id = await lark_anchor.locate(doc, cp.quote_text or "")
        except Exception as e:
            logger.info("[prd_watch] demo 锚定失败(%s): %s", cp.title, str(e)[:80])
        section = ""
        if cp.block_id:
            try:
                section = await lark_anchor.section_of(doc, cp.block_id)
            except Exception:
                section = ""
        row = cp.to_ledger_row(cr_id, binding.story_id, chat_id)
        row[COLUMNS["comment_id"]] = ""
        row["PRD文档token"] = doc
        row["关联群chat_id"] = chat_id
        await ledger.record_change(row)
        items.append(CardItem(
            story=StoryMeta(story_id=binding.story_id, story_title=binding.story_title,
                            story_type="PRD", space_name="自测", status="自测",
                            prd_title=binding.story_title),
            prd_url=base_url, cr_id=cr_id, point=cp,
            section=section,
            jump_url=f"{base_url}#{cp.block_id}" if base_url and cp.block_id else ""))
    card = build_notice_card(
        items, source="自测卡片（真实文档 + 生产按钮链路，非识别结果）",
        source_url="", at_names=[])        # 不 @ 具体人：自测不该给任何人造成待办
    return card, points, binding
