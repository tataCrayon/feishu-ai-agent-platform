"""日报卡片 — 把 AI HOT 日报渲染成飞书交互卡片（结构由代码钉死）。

## 为什么要有这一层

模型转述日报时结构每轮都在漂：图片与文本两条链路各是各的段落、条目标题
没有被链起来（蓝链只在模型自己决定写 markdown 时出现），用户想点开原条目
还得回聊天里翻。卡片把三件事固定下来：

1. **要点头**：头部标题「技术日报要点 · 2026-11-20」+ 一句 lead 导读；
2. **分区**：按 API 原始板块顺序（不得重排成排行榜），快讯单独成段；
3. **可点**：每条标题都是 `[标题](links.aihot)`，落点优先站内条目页，
   站内链接缺失时用必有的 `links.original`（技能约定）。

## 不变量

- **只渲染 API 返回的内容**，不做摘要判断、不补第一条；`lead` 缺失时用板块
  分布合成一句，与 `aihot._lead_text` 同口径。
- **链接是唯一可选项**：某条两个链接都空时只出纯文本标题，不拼死链。
- **条数封顶**：卡片是扫读入口，不是全文；超出写「另有 N 条」，全量走
  `daily_report` 的 markdown。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_FLASHES",
    "MAX_ITEMS_PER_SECTION",
    "build_daily_card",
    "collect_links",
    "render_card_markdown",
]

# 卡片体积上限 28KB：摘要全带上时 19 条约 7KB，这里留够余量给热门日。
MAX_ITEMS_PER_SECTION = 8
MAX_FLASHES = 6
# 要点头的导读软上限：卡片的 lead 是一行，取 API leadParagraph 的开头整句。
LEAD_MAX = 80
# lark_md 里单条摘要的软上限：卡片要扫读，长摘要留给 markdown 全文。
SUMMARY_MAX = 110

_NOTICE = "数据来自 AI HOT 精选日报（{date}）· 出处 aihot.virxact.com · 引用数字请回原文核对"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _links_of(node: dict[str, Any]) -> dict[str, Any]:
    """`node.links` 归一成 dict：API 里可能是 null，也可能混进别的形态。"""
    links = node.get("links")
    return links if isinstance(links, dict) else {}


def _daily_link(report: dict[str, Any]) -> str:
    """日报自身的站内页（report.links.aihot），缺失时为空。"""
    return _text(_links_of(report).get("aihot"))


def _item_link(item: dict[str, Any]) -> str:
    """条目链接：站内条目页优先，缺失回落到第三方原文（技能约定）。"""
    links = _links_of(item)
    return _text(links.get("aihot")) or _text(links.get("original"))


def _clip_sentence(text: str, limit: int) -> str:
    """句界截断：卡片的导读要一眼读完，不能在半句话上砍断。"""
    text = _text(text)
    if len(text) <= limit:
        return text
    window = text[:limit]
    # 第一个句末标点就收：导读是一句，不是一小段
    for i, ch in enumerate(window):
        if ch in "。！？" and i >= 9:
            return window[:i + 1]
    # 没有句末标点才退回逗号/分号断句：这些标点本身就能收尾，读着不像被剪断
    if window[-1] in "；，":
        return window
    for i in range(len(window) - 1, 11, -1):
        if window[i - 1] in "；，":
            return window[:i]
    return window + "…"


def _lead_text(report: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """一句话导读：API lead 的段落优先，其次标题；都没有就用板块分布兜底。"""
    lead = report.get("lead")
    if isinstance(lead, dict):
        for key in ("leadParagraph", "summary", "text", "title"):
            value = _text(lead.get(key))
            if value:
                return _clip_sentence(value, LEAD_MAX)
    elif isinstance(lead, str) and lead.strip():
        return _clip_sentence(lead, LEAD_MAX)
    labels = "、".join(dict.fromkeys(
        _text(s.get("label"))
        for s in report.get("sections") or []
        if isinstance(s, dict) and s.get("items")
    ))
    if len(items) > MAX_ITEMS_PER_SECTION:
        # 卡片只展开每板块前 N 条，导读不能报全量条数让用户以为卡片放全了。
        return f"今日板块：{labels or '暂无'}；卡片列出各板块前 {MAX_ITEMS_PER_SECTION} 条。"
    return f"今日精选 {len(items)} 条，覆盖 {labels or '多个'}板块。"


def _summary_line(summary: str, title: str) -> str:
    """摘要行：与标题重复时不重复说一遍；超长在句界截断。"""
    text = _text(summary)
    if not text or text == _text(title) or len(text) <= 8:
        return ""
    if len(text) <= SUMMARY_MAX:
        return text
    window = text[:SUMMARY_MAX]
    for i in range(len(window) - 1, 29, -1):
        if window[i - 1] in "。！？；":
            return window[:i]
    return window + "…"


def _entry_line(index: int, item: dict[str, Any], *, link: str) -> str:
    """一条条目：序号 + 可点标题 + 可选来源；摘要另起一行缩进。"""
    title = _text(item.get("title")) or "（无标题）"
    source = _text((item.get("source") or {}).get("name")) if isinstance(
        item.get("source"), dict) else ""
    headline = f"**[{title}]({link})**" if link else f"**{title}**"
    line = f"{index}. {headline}"
    if source:
        line += f" · <font color='grey'>{source}</font>"
    summary = _summary_line(_text(item.get("summary")), title)
    return line + (f"\n　　{summary}" if summary else "")


def _section_items(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(分区条目, 快讯条目)；保持 API 原始顺序，不合并不重排。"""
    flat: list[dict[str, Any]] = []
    for section in report.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("items") or []:
            if isinstance(item, dict):
                flat.append(item)
    flashes = [f for f in report.get("flashes") or [] if isinstance(f, dict)]
    return flat, flashes


def collect_links(report: dict[str, Any]) -> list[str]:
    """卡片里会出现的全部外链（回归/自检用：断言每条都落到了 aihot 站内页）。"""
    flat, flashes = _section_items(report)
    links = [_item_link(i) for i in flat + flashes]
    return [link for link in links if link]


def render_card_markdown(report: dict[str, Any]) -> list[str]:
    """卡片正文的分段 markdown：导读 → 各板块 → 快讯 → 尾注。

    每段单独成一个 markdown 元素而不是整篇拼成一坨：飞书对单元素渲染高度
    有限制，长文挤在一个元素里被裁掉时用户只看到半句话。
    """
    flat, flashes = _section_items(report)
    blocks: list[str] = [f"**{_lead_text(report, flat)}**"]

    index = 0
    for section in report.get("sections") or []:
        if not isinstance(section, dict):
            continue
        items = [i for i in section.get("items") or [] if isinstance(i, dict)]
        if not items:
            continue
        lines = [f"**{_text(section.get('label')) or '动态'}**"]
        for item in items[:MAX_ITEMS_PER_SECTION]:
            index += 1
            lines.append(_entry_line(index, item, link=_item_link(item)))
        leftover = len(items) - MAX_ITEMS_PER_SECTION
        if leftover > 0:
            lines.append(f"<font color='grey'>另有 {leftover} 条未在卡片展开</font>")
        blocks.append("\n".join(lines))

    if flashes:
        lines = ["**快讯**"]
        for item in flashes[:MAX_FLASHES]:
            title = _text(item.get("title")) or "（无标题）"
            link = _item_link(item)
            lines.append("· " + (f"[{title}]({link})" if link else title))
        if len(flashes) > MAX_FLASHES:
            lines.append(f"<font color='grey'>另有 {len(flashes) - MAX_FLASHES} 条快讯</font>")
        blocks.append("\n".join(lines))

    footer = _NOTICE.format(date=_text(report.get("date")) or "—")
    blocks.append(f"<font color='grey'>{footer}</font>")
    return blocks


def build_daily_card(report: dict[str, Any]) -> dict[str, Any]:
    """渲染整张卡片：header 写要点头（日期），正文按 `render_card_markdown` 分段。

    头部不显示 `来自 XXX 的消息` 副标题——日报是产品内容，不是某个人代发的
    通知；副标题留给「谁把日报发给了我」这类事实性说明。
    """
    from lark_channel import CardBuilder

    date_iso = _text(report.get("date"))
    flat, _ = _section_items(report)
    b = CardBuilder()
    b.header(
        f"技术日报要点 · {date_iso or '今日'}",
        subtitle=f"AI HOT 精选 · 共 {len(flat)} 条",
        template="blue",
    )
    for block in render_card_markdown(report):
        b.markdown(block)
    day_url = _daily_link(report)
    if day_url:
        b.button("查看完整日报", url=day_url, style="primary")
    return b.to_dict()
