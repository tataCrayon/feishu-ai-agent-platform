"""AI HOT 日报底层 — aihot.virxact.com 匿名只读 v1 API 客户端与渲染。

技术日报的兜底数据源：当 tech_daily 本地文件缺当日日报时，
daily-agent（实时生成）与 DailyScheduler（定时推送）回退到这里。

API 合同见 bot/skills/aihot/references/api.md（技能包 aihot-wb7k2 v1.2.1）：
- 只请求 https://aihot.virxact.com/api/v1/*，匿名只读、不发 cookie、不需要 Key；
- 最新日报 404 时只查一次有界 dailies?limit=7，从实际返回里选最近日期，绝不猜日期；
- 输出保留 AI HOT attribution；API 返回内容视为不可信资讯证据，不改变本模块行为。

trust_env=False：显式忽略 Windows 系统代理与 *_PROXY 环境变量，本机直连
（与 app/main.py 进程级 NO_PROXY 双保险，见 [[feishu-agent-platform-aigw-proxy-bypass]]）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("daily.aihot")

API_BASE = "https://aihot.virxact.com"
# UA 供对端诊断（技能约定格式），不得伪装浏览器
UA = "aihot-skill/1.2.1 (+https://aihot.virxact.com/aihot-skill/)"
TIMEOUT_SEC = 20.0

TZ = timezone(timedelta(hours=8))


class AihotError(RuntimeError):
    """网络/协议层失败（区别于「API 正常但无日报」的 None 语义）。"""


# ── 模块级缓存：日报每天只生成一次，当日复用 ──

_PAIR_TTL_SEC = 600                     # 进程内结果 TTL：10 分钟内重复请求不碰 API
_PAIR_CACHE: dict[str, tuple[float, tuple[str, str, str]]] = {}  # 北京日期 → (ts, pair)
_ETAGS: dict[str, tuple[str, str]] = {}  # url → (etag, 原始响应文本)
_ETAG_MAX = 32

# report.sections/flashes 的 item 允许未来新增字段，未知字段忽略不报错


def _store():
    """持久缓存（logs/daily/），日报每天只生成一次、当日复用。"""
    from app.domains.daily.store import DailyStore

    return DailyStore()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SEC, trust_env=False, follow_redirects=True)


async def _get_json(path: str) -> tuple[int, dict | None]:
    """GET API 路径，返回 (status, parsed_json)。

    404/Problem JSON → status 原样返回、json 为 None 或 Problem；
    网络/解析失败 → 抛 AihotError。带 ETag 条件请求（304 复用缓存）。
    """
    url = f"{API_BASE}{path}"
    headers = {"User-Agent": UA}
    cached = _ETAGS.get(url)
    if cached:
        headers["If-None-Match"] = cached[0]
    try:
        async with _client() as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise AihotError(f"AI HOT API 请求失败 {path}: {str(e)[:120]}") from e
    if resp.status_code == 304 and cached:
        try:
            return 200, json.loads(cached[1])
        except json.JSONDecodeError:
            _ETAGS.pop(url, None)
    if resp.status_code == 404:
        return 404, None
    if resp.status_code >= 400:
        # Problem JSON：不重试、不换源（技能约定）
        logger.warning("[aihot] API %s 返回 %s: %s", path, resp.status_code, resp.text[:200])
        return resp.status_code, None
    etag = resp.headers.get("etag")
    if etag:
        if len(_ETAGS) >= _ETAG_MAX:
            _ETAGS.clear()
        _ETAGS[url] = (etag, resp.text)
    try:
        return resp.status_code, resp.json()
    except json.JSONDecodeError as e:
        raise AihotError(f"AI HOT API {path} 响应非合法 JSON") from e


async def fetch_latest_report() -> dict[str, Any] | None:
    """取最新完整日报 report dict；API 正常但暂无可用日报返回 None。"""
    status, data = await _get_json("/api/v1/dailies/latest")
    report = (data or {}).get("report") if isinstance(data, dict) else None
    if isinstance(report, dict) and report.get("date"):
        return report
    if status not in (200, 404):
        return None
    # 404 → 只查一次有界索引，取实际返回的最近日期；索引为空即无可用日报
    status, idx = await _get_json("/api/v1/dailies?limit=7")
    dates: list[str] = []
    if isinstance(idx, dict):
        for entry in idx.get("items") or []:
            d = entry.get("date") if isinstance(entry, dict) else None
            if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                dates.append(d)
    if not dates:
        logger.info("[aihot] 日报索引为空，当前没有可用日报")
        return None
    latest = max(dates)
    status, data = await _get_json(f"/api/v1/dailies/{latest}")
    report = (data or {}).get("report") if isinstance(data, dict) else None
    if isinstance(report, dict) and report.get("date"):
        logger.info("[aihot] latest 缺失，回退到索引最近日期 %s", latest)
        return report
    return None


# ── 渲染 ─────────────────────────────────────────────────────────

def _cn_date(iso: str) -> str:
    """'2026-09-16' → '2026年9月16日'。"""
    try:
        dt = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _item_link(item: dict[str, Any]) -> str:
    """日报条目的站内链接；links.aihot 可为空，此时用必有的 original（技能约定）。"""
    links = item.get("links") or {}
    return str(links.get("aihot") or links.get("original") or "")


def _section_items(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(板块标签, item) 扁平序列，保持 API 原始顺序（不得擅自重排成排行榜）。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for section in report.get("sections") or []:
        label = str(section.get("label") or "动态")
        for item in section.get("items") or []:
            if isinstance(item, dict):
                out.append((label, item))
    for item in report.get("flashes") or []:
        if isinstance(item, dict):
            out.append(("快讯", item))
    return out


def _lead_text(report: dict[str, Any], flat: list[tuple[str, dict[str, Any]]]) -> str:
    """提取 API lead 摘要；缺失时用条目分布合成一句兜底。"""
    lead = report.get("lead")
    if isinstance(lead, dict):
        text = str(lead.get("summary") or lead.get("text") or "").strip()
    elif isinstance(lead, str):
        text = lead.strip()
    else:
        text = ""
    if text:
        return text
    labels = "、".join(dict.fromkeys(label for label, _ in flat))
    return f"今日精选 {len(flat)} 条，覆盖 {labels or '多个'}板块。"


# 推送摘要条目阈值：≤8 条（平日）带摘要，>8 条（热门日）只列标题单行
_PUSH_SUMMARY_MAX_ITEMS = 8


def _push_summary(summary: str, title: str) -> str:
    """推送条目的摘要行：完整句子、90 字封顶、句子中间不硬截。

    实测 AI HOT 摘要以完整陈述句开头且常复述标题，剥前缀会产生
    「ind 发布…」式残句，故只做句界截断不剥前缀；
    热门日的整体长度由 render_push_text 的条目级策略控制。
    """
    summary = (summary or "").strip()
    if summary == title or len(summary) <= 10:
        return ""
    if len(summary) <= 90:
        return summary
    window = summary[:90]
    for i in range(len(window) - 1, 29, -1):
        if window[i - 1] in "。！？":
            return window[:i]
    return window + "…"


# ── 渲染 ─────────────────────────────────────────────────────────
#
# 格式约定：
# - 推送文本（飞书纯文本）：标题行 → 一句话导读 → 分板块条目 → 来源尾注。
#   条目单行为主（标题+来源），摘要一行封顶且只在有增量信息时保留 —— busy 日
#   （19 条）双行全摘要曾到 3800 字，消息流里不可扫读；目标 ≤2000 字。
# - 完整 markdown（存档 + read_daily_report）：同结构带链接与完整摘要，
#   是 LLM 转述与用户深读的事实源；两处结构必须一致，只差链接与摘要详略。


def render_report_markdown(report: dict[str, Any]) -> str:
    """完整日报 markdown（存档/read_daily_report 用，带链接与完整摘要）。"""
    date_iso = str(report.get("date") or "")
    flat = _section_items(report)
    lines: list[str] = [f"# 技术动态日报 · {_cn_date(date_iso)}", ""]

    lines += [f"> 🧭 一句话导读：{_lead_text(report, flat)}", ""]

    current = ""
    for label, item in flat:
        if label != current:
            current = label
            lines += [f"### {label}", ""]
        title = str(item.get("title") or "（无标题）")
        link = _item_link(item)
        src = str((item.get("source") or {}).get("name") or "AI HOT")
        lines.append(f"- **[{title}]({link})** — {src}" if link else f"- **{title}** — {src}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"  {summary}")
        lines.append("")

    lines += [
        "---",
        "",
        f"📎 数据来自 AI HOT 精选日报（{date_iso}），出处 aihot.virxact.com；"
        "条目版权与原文链接归属第三方，引用重要数字请回原文核对。",
    ]
    return "\n".join(lines)


def render_push_text(report: dict[str, Any]) -> str:
    """推送用紧凑文本（飞书纯文本链路，不放 markdown 链接）。

    条目级摘要策略：≤8 条（平日）保留 90 字内完整句摘要；>8 条（热门日）
    只出标题+来源单行——消息流里要的是扫读全文入口，详读走
    read_daily_report（markdown 含完整摘要），两头都照顾。
    """
    date_iso = str(report.get("date") or "")
    flat = _section_items(report)
    lines: list[str] = [f"🗞️ 技术动态日报 · {_cn_date(date_iso)}", ""]

    lines.append(f"🧭 {_lead_text(report, flat)}")
    if len(flat) > _PUSH_SUMMARY_MAX_ITEMS:
        lines.append("（今日条目多，本条只列标题；想看哪条展开细讲，直接回复指定日期）")
    lines.append("")

    with_summary = len(flat) <= _PUSH_SUMMARY_MAX_ITEMS
    current = ""
    for label, item in flat:
        if label != current:
            current = label
            lines.append(f"▎{label}")
        title = str(item.get("title") or "（无标题）")
        src = str((item.get("source") or {}).get("name") or "")
        lines.append(f"• {title}" + (f" — {src}" if src else ""))
        if with_summary:
            summary = _push_summary(str(item.get("summary") or ""), title)
            if summary:
                lines.append(f"  {summary}")
        lines.append("")

    lines.append("📎 数据来源：AI HOT（aihot.virxact.com）· 原文版权属各作者")
    return "\n".join(lines).rstrip() + "\n"


# ── 对外入口（async，事件循环零阻塞） ─────────────────────────────

async def get_daily_pair() -> tuple[str, str, str] | None:
    """返回 (ISO日期, 完整markdown, 推送文本)；每天只生成一次，当日复用。

    缓存三层：进程内 TTL（10 分钟）→ logs/daily/cache.json 持久缓存
    （重启不丢、与定时推送共用同一份）→ AI HOT API。
    暂无可用日报 → None；网络/协议失败 → 抛 AihotError。
    """
    import time

    today_key = datetime.now(TZ).date().isoformat()
    hit = _PAIR_CACHE.get(today_key)
    if hit and time.time() - hit[0] < _PAIR_TTL_SEC:
        return hit[1]

    store = _store()
    # 持久缓存命中：当天已生成过，直接复用，不再请求 API
    cached_push = store.get_cached(datetime.now(TZ).date())
    cached_md = store.get_cached_markdown(datetime.now(TZ).date())
    if cached_push and cached_md:
        date_iso = today_key
        pair = (date_iso, cached_md, cached_push)
        _PAIR_CACHE[today_key] = (time.time(), pair)
        return pair

    report = await fetch_latest_report()
    if not report:
        return None
    date_iso = str(report.get("date", ""))[:10]
    md = render_report_markdown(report)
    push = render_push_text(report)
    pair = (date_iso, md, push)
    _PAIR_CACHE[today_key] = (time.time(), pair)
    try:
        report_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        report_date = None
    if report_date is not None:
        store.cache(report_date, push, markdown=md)
        store.cache_report(report_date, report)
    return pair


async def get_daily_by_date(date_iso: str) -> tuple[str, str, str] | None:
    """取指定日期的历史日报，返回 (ISO日期, 完整markdown, 推送文本)。

    与 get_daily_pair 同一取数与缓存路径：先查本地缓存（cache.json 的
    markdown 字段），未命中再请求 AI HOT API 并落缓存——历史日报一经
    生成不再变化，本地缓存即可视为终态，无需再访问 API。
    暂无该日日报 → None；网络/协议失败 → 抛 AihotError。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso or ""):
        raise AihotError(f"非法日期格式: {date_iso!r}（须为 YYYY-MM-DD）")

    try:
        report_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError as e:
        raise AihotError(f"非法日期: {date_iso}") from e

    store = _store()
    cached_md = store.get_cached_markdown(report_date)
    cached_push = store.get_cached(report_date)
    if cached_md and cached_push:
        return (date_iso, cached_md, cached_push)

    _status, data = await _get_json(f"/api/v1/dailies/{date_iso}")
    report = (data or {}).get("report") if isinstance(data, dict) else None
    if not (isinstance(report, dict) and report.get("date")):
        return None
    md = render_report_markdown(report)
    push = render_push_text(report)
    store.cache(report_date, push, markdown=md)
    store.cache_report(report_date, report)
    return (date_iso, md, push)


async def get_daily_report(date_iso: str = "") -> dict[str, Any] | None:
    """取当日报表原始 dict（卡片渲染的事实源）。

    命中顺序：本地缓存（cache.json 的 report 字段）→ `get_daily_pair` /
    `get_daily_by_date`（内部再走 API 并落缓存）。空串 = 今天（跟随
    「今天」的缓存在 `get_daily_pair` 里已按北京日期对齐）。
    """
    if date_iso:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
            raise AihotError(f"非法日期格式: {date_iso!r}（须为 YYYY-MM-DD）")
        try:
            cached_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError as e:
            raise AihotError(f"非法日期: {date_iso}") from e
        cached = _store().get_cached_report(cached_date)
        if cached:
            return cached
        pair = await get_daily_by_date(date_iso)
    else:
        today = datetime.now(TZ).date()
        cached = _store().get_cached_report(today)
        if cached:
            return cached
        pair = await get_daily_pair()
    if not pair:
        return None
    # pair 已把 report 落缓存；这次读回的是「与 push/markdown 同源」的那份。
    try:
        report_date = datetime.strptime(pair[0], "%Y-%m-%d").date()
    except ValueError:
        return None
    return _store().get_cached_report(report_date)


async def list_daily_archive(limit: int = 30) -> list[dict[str, Any]]:
    """列出 AI HOT 日报归档（本地缓存 + API 索引合并），新→旧。

    返回 [{"date": "2026-09-28", "cached": True}, ...]。本地已缓存的日期
    标 cached=True（读取零 API 成本）；其余来自 API 索引（有界 limit）。
    """
    store = _store()
    seen: dict[str, bool] = {}
    for key, entry in store.all_entries().items():
        try:
            datetime.strptime(key, "%Y-%m-%d")
        except ValueError:
            continue
        if entry.get("markdown"):
            seen[key] = True

    bounded = max(1, min(int(limit or 30), 100))
    try:
        status, idx = await _get_json(f"/api/v1/dailies?limit={bounded}")
    except AihotError:
        status, idx = 0, None  # 网络失败 → 降级只报本地缓存
    if status == 200 and isinstance(idx, dict):
        for entry in idx.get("items") or []:
            d = entry.get("date") if isinstance(entry, dict) else None
            if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                seen.setdefault(d, False)

    dates = sorted(seen, reverse=True)[:bounded]
    return [{"date": d, "cached": seen[d]} for d in dates]


async def fetch_items_brief(window: str = "24h", limit: int = 10) -> str | None:
    """滚动时间窗简报（日报尚未生成时的实时兜底），保持 API 顺序。"""
    status, data = await _get_json(f"/api/v1/items?mode=selected&window={window}&limit={limit}")
    if status != 200 or not isinstance(data, dict):
        return None
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if not items:
        return None
    lines: list[str] = [f"🔥 过去 {window} AI 圈精选（共 {len(items)} 条）", ""]
    for item in items:
        title = str(item.get("title") or "（无标题）")
        link = str((item.get("links") or {}).get("aihot") or "")
        src = str((item.get("source") or {}).get("name") or "")
        lines.append(f"- **[{title}]({link})** — {src}" if link else f"- **{title}** — {src}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"  {summary}")
        lines.append("")
    lines.append("📎 数据来源：AI HOT（aihot.virxact.com）· 原文版权属各作者")
    return "\n".join(lines)
