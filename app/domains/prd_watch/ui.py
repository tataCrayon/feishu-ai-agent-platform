"""PRD ↔ UI 设计稿对账 — 文本层差异（第一期）。

设计取向：
- **只做文本层**。第一期不引入视觉模型：Figma 的 design data 里已经有图层名、
  可见文案、组件与状态命名，足够回答「PRD 写的字段/入口/状态，稿子里有没有、
  文案对不对」。视觉层级、间距、配色不在本期范围（没做就明说，别假装看过图）。
- **复用 easyprd 的识别与闭环**。差异也是 `ChangePoint`：同样写 PRD 评论、同样发
  卡片、同样采纳/忽略。产品不用学第二套操作。
- **两种差异都记**：PRD 有稿子没有（漏做）、稿子有 PRD 没写（漏写）。只报前者会
  让"设计稿自己加了东西"永远进不了 PRD。

不猜的事：Figma 的 frame 名与 PRD 章节没有固定映射，`anchor` 只在 PRD 正文里
按文案/字段名找块；找不到就是「未定位」，不硬凑。

Figma 访问：走 REST（`/v1/files/{key}` 的 design data），token 从配置或环境变量取。
不挂 figma MCP：MCP 是给交互式客户端用的，旁路服务需要的是稳定的 HTTP 调用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai

from app.domains.prd_watch.models import (
    BAND_DROPPED,
    BAND_FLAGGED,
    BAND_NORMAL,
    ChangePoint,
)
from app.llm import build_cacheable_messages, estimate_cost, extract_cache_metrics
from app.prompts.loader import PromptLoader

logger = logging.getLogger("prd_watch.ui")

FIGMA_API = "https://api.figma.com/v1"

# Figma 链接：`figma.com/(file|design|proto)/<key>/<name>?node-id=1-2`
_KEY_RE = re.compile(r"figma\.com/(?:file|design|proto|board)/([A-Za-z0-9]+)")
_NODE_RE = re.compile(r"[?&]node-id=([0-9A-Za-z:\-]+)")

# 图层里当「可见文案」的键。TEXT 节点的 characters 是唯一可信文案来源；
# 组件/画板名只作结构线索（它们常是英文组件名，不当成 UI 文案）。
_COMPONENT_NAME_KEYS = ("name", "componentName", "description")
_SKIP_NAME_RE = re.compile(r"^(Frame|Group|Rectangle|Ellipse|Line|Vector|Image|Auto\s?layout)",
                           re.I)


@dataclass
class UiFrame:
    """一个画板/节点提取出来的可比较信息。"""

    node_id: str = ""
    name: str = ""
    texts: list[str] = field(default_factory=list)      # 可见文案（TEXT.characters）
    components: list[str] = field(default_factory=list)  # 组件/图层名（结构线索）
    fields: list[str] = field(default_factory=list)      # 疑似表单字段（从文本+组件名推）


def parse_figma_link(link: str) -> tuple[str, str]:
    """→ (file_key, node_id)。node_id 可为空（整文件）。"""
    text = (link or "").strip()
    m = _KEY_RE.search(text)
    key = m.group(1) if m else ""
    n = _NODE_RE.search(text)
    # URL 里的 node-id 用 `-` 分隔（1-2），API 要 `1:2`
    node = (n.group(1).replace("-", ":") if n else "")
    return key, node


# Figma token 的查找顺序（先进程环境，再本机已有的配置文件）。
# 为什么还要读 mcp-gateway：这台机器的 Figma token 一直配在
# `~/.claude/mcp-gateway/backends.json` 的 figma backend env 里（Claude Code 的
# figma MCP 用那份）。bot 是独立进程、不继承那份 env，只认 .env 的话就会出现
# 「明明配过 token 却报未配置」——实测就是这个原因。
_TOKEN_ENV = ("FIGMA_API_KEY", "FIGMA_TOKEN")
_GATEWAY_BACKENDS = Path.home() / ".claude" / "mcp-gateway" / "backends.json"
# 见过 token 的失效形态：Figma 个人 token 会过期（`401 Token has expired`），
# 而网关那份常年没人换。调用方据此能把「过期」和「没配」分开说。
_EXPIRED_MARKERS = ("token has expired", "invalid token", "expired")


class FigmaAuthError(RuntimeError):
    """Figma 鉴权失败（过期/无效），message 直接面向用户。"""


def resolve_token() -> str:
    """Figma token：进程环境 → mcp-gateway 的 figma backend env → 空串。

    只读、不改写：拿不到就返回空串，由调用方明确说「未配置」，
    绝不回退到匿名请求（Figma REST 不接受匿名，会得到 403 一堆噪声）。
    """
    for name in _TOKEN_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return _token_from_gateway()


def _token_from_gateway() -> str:
    """从 `~/.claude/mcp-gateway/backends.json` 读 figma backend 的 FIGMA_API_KEY。"""
    try:
        data = json.loads(_GATEWAY_BACKENDS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    backend = data.get("figma") if isinstance(data, dict) else None
    env = (backend or {}).get("env") if isinstance(backend, dict) else None
    if not isinstance(env, dict):
        return ""
    for name in _TOKEN_ENV:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def is_expired_error(text: str) -> bool:
    """错误文本是否属于「token 过期/失效」（与「没配」区分开）。"""
    low = (text or "").lower()
    return any(m in low for m in _EXPIRED_MARKERS)


def _retry_after_text(raw: str | None) -> str:
    """`Retry-After` 秒 → 人话（Figma 组织账号实测 9000+ 秒，不说清会反复重试）。"""
    try:
        seconds = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds >= 3600:
        return f"，约需等待 {seconds / 3600:.1f} 小时"
    return f"，约需等待 {max(1, seconds // 60)} 分钟"


def is_rate_limited(text: str) -> bool:
    """错误文本是否属于限流（`429 Rate limit exceeded`）。

    单独判是因为处理动作完全不同：限流是**等一会儿**，不需要换 token；
    而 Figma 对各端点的限流窗口不同（实测 `/v1/me` 正常、`/v1/files` 429），
    混成一句「读取失败」会让排查方向跑偏。
    """
    low = (text or "").lower()
    return "rate limit" in low or "429" in low


def fetch_file(key: str, token: str, *, node_id: str = "",
               timeout_sec: float = 30.0) -> dict[str, Any]:
    """拉 Figma design data（同步 urllib，放线程里跑由调用方决定）。"""
    if not key:
        raise ValueError("Figma 链接里没有文件 key")
    if not token:
        raise ValueError("缺少 Figma token（FIGMA_API_KEY）；REST 接口不接受匿名访问")
    params = "?depth=6" + (f"&ids={node_id}" if node_id else "")
    url = f"{FIGMA_API}/files/{key}{params}"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


async def load_frames(link: str, token: str = "") -> tuple[list[UiFrame], list[str]]:
    """Figma 链接 → 画板素材（失败原因进 errors，不抛给上层）。"""
    import asyncio

    errors: list[str] = []
    key, node = parse_figma_link(link)
    if not key:
        return [], ["不是 Figma 文件链接（需要 figma.com/file|design/...）"]
    tok = token or resolve_token()
    if not tok:
        return [], ["未配置 Figma token（.env 的 FIGMA_API_KEY 或 "
                    "~/.claude/mcp-gateway/backends.json 的 figma 后端），设计稿无法读取"]
    try:
        payload = await asyncio.to_thread(fetch_file, key, tok, node_id=node)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:160]
        except Exception:
            detail = ""
        if is_rate_limited(detail) or e.code == 429:
            wait = _retry_after_text(e.headers.get("Retry-After") if e.headers else None)
            return [], [f"Figma 接口限流（429）{wait}——"
                        "不需要换 token；Figma 组织账号的 /v1/files 配额较低（"
                        "`X-Figma-Rate-Limit-Type: low`），批量对账要拉长间隔"]
        if e.code in (401, 403) and is_expired_error(detail):
            return [], ["Figma token 已过期（Figma 回的原文：Token has expired）——"
                        "需到 Figma 设置里重新生成 personal access token，"
                        "写 .env 的 FIGMA_API_KEY 或 mcp-gateway 的 figma 后端"]
        hint = "（token 无效或无该文件权限）" if e.code in (401, 403) else ""
        return [], [f"Figma 读取失败 HTTP {e.code}{hint}"
                    + (f"：{detail[:80]}" if detail else "")]
    except Exception as e:
        return [], [f"Figma 读取异常: {str(e)[:140]}"]
    document = payload.get("document") or {}
    frames: list[UiFrame] = []
    _walk(document, frames, node_filter=node)
    if not frames:
        errors.append("设计稿里没有取到可比较的画板/文本（可能只有图片填充）")
    return frames, errors


def _walk(node: Any, out: list[UiFrame], *, node_filter: str = "") -> None:
    """递归收集：把 FRAME/COMPONENT 当画板，TEXT 当文案。"""
    if not isinstance(node, dict):
        return
    ntype = str(node.get("type") or "")
    node_id = str(node.get("id") or "")
    if node_filter and node_id == node_filter:
        out.append(_frame_of(node))
        return
    if ntype in ("FRAME", "COMPONENT", "SECTION", "CANVAS"):
        if node_filter:
            for child in node.get("children") or []:
                _walk(child, out, node_filter=node_filter)
            return
        frame = _frame_of(node)
        if frame.texts or frame.components:
            out.append(frame)
        return
    for child in node.get("children") or []:
        _walk(child, out, node_filter=node_filter)


def _frame_of(node: dict[str, Any]) -> UiFrame:
    texts: list[str] = []
    comps: list[str] = []
    _collect(node, texts, comps)
    return UiFrame(node_id=str(node.get("id") or ""), name=str(node.get("name") or ""),
                   texts=_uniq(texts), components=_uniq(comps),
                   fields=_guess_fields(texts, comps))


def _collect(node: Any, texts: list[str], comps: list[str]) -> None:
    if not isinstance(node, dict):
        return
    ntype = str(node.get("type") or "")
    name = str(node.get("name") or "").strip()
    if ntype == "TEXT":
        characters = str(node.get("characters") or "").strip()
        if characters:
            texts.append(characters)
    elif name and not _SKIP_NAME_RE.match(name):
        comps.append(name)
    for child in node.get("children") or []:
        _collect(child, texts, comps)


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _guess_fields(texts: list[str], comps: list[str]) -> list[str]:
    """疑似表单字段：短文案里带这些后缀的（率、额、数量、状态、日期…）。

    只是给识别器的**线索**，不是判据；识别器要自己决定这条差异成不成立。
    """
    hints = ("率", "额", "数量", "金额", "状态", "日期", "时间", "名称", "编号",
             "类型", "账号", "手机", "邮箱", "备注", "期限", "价格", "净值")
    out: list[str] = []
    for text in texts:
        clean = text.strip()
        if 1 < len(clean) <= 20 and any(h in clean for h in hints):
            out.append(clean)
    for comp in comps:
        if any(k in comp.lower() for k in ("input", "select", "field", "form", "checkbox")):
            out.append(comp)
    return _uniq(out)[:40]


def build_ui_frame_text(frames: list[UiFrame]) -> str:
    """把画板素材压成识别器可读的一段文本（控制长度：识别只需要骨架）。"""
    blocks: list[str] = []
    for f in frames[:6]:
        head = f"——— 画板：{f.name or f.node_id} ———"
        lines = [head]
        if f.fields:
            lines.append("疑似字段/控件：" + "、".join(f.fields[:20]))
        if f.texts:
            lines.append("可见文案：" + " | ".join(f.texts[:40]))
        if f.components:
            lines.append("图层/组件名：" + "、".join(f.components[:20]))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:9000]


# ── 差异 → ChangePoint ────────────────────────────────────────────

UI_PROMPT_PATH = "agents/prd-watch-ui-diff.md"

_UI_FIELDS = ("title", "ui_text", "prd_text", "suggested_text", "quote_text",
              "diff_type", "evidence", "confidence", "story_id")


class UiMatcher:
    """PRD↔UI 差异识别器（与 `ChangeExtractor` 同接口：`match` / `model`）。

    为什么单独一个类而不是给 extract.py 加参数：输入帧、判据 prompt、输出字段全都
    不同（那边是讨论片段→变更，这里是两组文本→差异），塞进一个类会变成
    「if kind == 'ui'」的分支堆。共用的只有**分带阈值**与**下游链路**。
    """

    def __init__(self, config: Any, watch_config: Any):
        self._config = config
        self._wc = watch_config
        self._client = openai.AsyncClient(
            api_key=config.api_key, base_url=config.base_url,
            max_retries=config.max_retries)
        self._system_prompt = PromptLoader.load(
            UI_PROMPT_PATH,
            fallback="你是 PRD 与 UI 设计稿的对账器，只输出 {\"diffs\": []}。")
        self.prompt_version = PromptLoader.get_prompt_version(UI_PROMPT_PATH)

    @property
    def model(self) -> str:
        return self._config.model

    async def match(
        self, ui_frame_text: str, prd_excerpt: str,
    ) -> tuple[list[ChangePoint], dict[str, Any]]:
        """→ (差异列表, 统计)。统计口径与 `ChangeExtractor.extract` 一致。"""
        frame = _build_frame(ui_frame_text, prd_excerpt)
        stats: dict[str, Any] = {"candidates": 0, "dropped": 0, "flagged": 0,
                                 "prompt_version": self.prompt_version}
        if not (ui_frame_text or "").strip():
            stats["error"] = "设计稿素材为空（Figma 未取到文本层）"
            return [], stats
        start = time.monotonic() * 1000
        try:
            resp = await self._client.chat.completions.create(
                model=self._config.model,
                # 与变更识别同口径：temperature=0 让同一份素材的输出尽量稳定
                temperature=0.0,
                messages=_cacheable(self._system_prompt, frame),
                timeout=self._config.timeout_sec,
            )
        except Exception as e:
            logger.warning("UI 对账调用失败: %s", e)
            stats["error"] = str(e)[:200]
            return [], stats
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        usage = resp.usage
        cm = extract_cache_metrics(usage)
        stats.update(
            tokens=usage.total_tokens if usage else 0,
            cost_usd=estimate_cost(self._config.model, {
                "prompt_tokens": cm.input_tokens,
                "completion_tokens": cm.output_tokens,
                "cache_read_input_tokens": cm.cache_read_input_tokens,
            }),
            latency_ms=round(time.monotonic() * 1000 - start, 1),
        )
        points, band_stats = points_from_json(
            raw, min_conf=self._wc.extract_min_confidence,
            flag_conf=self._wc.extract_flag_confidence)
        stats.update({k: v for k, v in band_stats.items() if k != "candidates"})
        stats["candidates"] = band_stats.get("candidates", 0)
        if band_stats.get("error"):
            stats["error"] = band_stats["error"]
        return points, stats


def _cacheable(system: str, user: str) -> Any:
    """build_cacheable_messages 的结果按 Any 交给 SDK（存量类型缺口，同 extract.py）。"""
    return build_cacheable_messages(system, user)


def _build_frame(ui_text: str, prd_excerpt: str) -> str:
    return "\n".join([
        "【PRD 原文节选】（prd_text / quote_text 只能从这里逐字引用）",
        (prd_excerpt or "（未取到 PRD 正文，所有差异按「稿有PRD无」处理）").strip()[:7000],
        "",
        "【设计稿文本层素材】（ui_text 只能从这里逐字取）",
        (ui_text or "").strip()[:7000],
        "",
        "只输出 JSON。",
    ])


def points_from_json(raw: str, *, min_conf: float, flag_conf: float,
                     story_id: str = "") -> tuple[list[ChangePoint], dict[str, Any]]:
    """识别器输出 → ChangePoint 列表（与 extract 的分带口径一致）。

    分带必须共用 `extract_min_confidence` / `extract_flag_confidence`：
    两套阈值会让「这条差异算不算数」在两种来源下不一样，产品无法建立信任。
    """
    stats: dict[str, Any] = {"candidates": 0, "dropped": 0, "flagged": 0}
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        stats["error"] = f"输出非 JSON: {(raw or '')[:120]}"
        return [], stats
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        stats["error"] = f"输出非 JSON: {(raw or '')[:120]}"
        return [], stats
    items = data.get("diffs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], stats
    stats["candidates"] = len(items)
    out: list[ChangePoint] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cp = _to_point(item, story_id)
        if cp.confidence < min_conf:
            stats["dropped"] += 1
            continue
        if cp.confidence < flag_conf:
            cp.band = BAND_FLAGGED
            stats["flagged"] += 1
        else:
            cp.band = BAND_NORMAL
        out.append(cp)
    return out, stats


def _to_point(item: dict[str, Any], story_id: str) -> ChangePoint:
    known = {k: v for k, v in item.items() if k in _UI_FIELDS}
    text = str(known.get("ui_text") or "").strip()
    prd = str(known.get("prd_text") or "").strip()
    diff_type = str(known.get("diff_type") or "").strip()
    # 差异类型 → easyprd 已有的 change_type 词汇（卡片/评论模板不用改）
    change_type = {
        "稿有PRD无": "新增条款",
        "PRD有稿无": "删除条款",
        "文案不一致": "参数调整",
        "流程不一致": "流程变更",
    }.get(diff_type, "参数调整")
    cp = ChangePoint(
        title=str(known.get("title") or "").strip(),
        current_text=prd,
        suggested_text=str(known.get("suggested_text") or "").strip(),
        quote_text=str(known.get("quote_text") or "").strip() or prd,
        is_new_clause=(diff_type == "稿有PRD无"),
        change_type=change_type,
        modules=["UI"],
        evidence_quote=str(known.get("evidence") or text).strip(),
        speaker="设计稿",
        scenario="PRD↔UI 对账",
        rationale=f"差异类型：{diff_type or '未标注'}",
        story_id=story_id,
        band=BAND_DROPPED,
    )
    try:
        cp.confidence = max(0.0, min(1.0, float(known.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        cp.confidence = 0.0
    return cp
