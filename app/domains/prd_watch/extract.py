"""easyprd 变更识别 — 讨论片段 → ChangePoint 列表（含置信度分带）。

LLM 调用范式照 app/orchestrator/direct.py（同一网关、同一缓存构造、同一费用口径），
不另开一套 client：新增调用点必须能被费用统计看到，否则「我们变强了」没有数据支撑。

判据单源在 bot/prompts/agents/prd-watch-agent.md，本模块只做取数与校验，
不重复写判据（两处写规则必然漂移）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import openai

from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch.models import (
    BAND_DROPPED,
    BAND_FLAGGED,
    BAND_NORMAL,
    ChangePoint,
    PrdCandidate,
)
from app.llm import build_cacheable_messages, estimate_cost, extract_cache_metrics
from app.prompts.loader import PromptLoader

logger = logging.getLogger("prd_watch.extract")

PROMPT_PATH = "agents/prd-watch-extract.md"

# 模型偶尔会裹 ```json 围栏或前后加话，取第一个 { 到最后一个 } 再解析
_JSON_RE = re.compile(r"\{.*\}", re.S)

# prompt 声明的字段全集；未知键丢弃、缺键取 ChangePoint 默认值。
_FIELDS = ("quote_text", "is_new_clause", "title", "current_text", "suggested_text",
           "change_type", "modules", "compliance", "evidence_quote", "speaker",
           "scenario", "rationale", "confidence", "story_id")


class ChangeExtractor:
    """一次调用的变更提取器（无状态，可被 watcher 并发复用）。"""

    def __init__(self, config: RouterConfig, watch_config: PrdWatchConfig):
        self._config = config
        self._wc = watch_config
        self._client = openai.AsyncClient(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=config.max_retries,
        )
        self._system_prompt = PromptLoader.load(
            PROMPT_PATH,
            fallback="你是 PRD 变更识别器，只输出 {\"changes\": []}。",
        )
        self.prompt_version = PromptLoader.get_prompt_version(PROMPT_PATH)

    @property
    def model(self) -> str:
        """识别用的模型名（任务档案 cost/model 归因需要）。"""
        return self._config.model

    async def extract(
        self, discussion_text: str, prd_excerpt: str = "",
        *, candidates: list[PrdCandidate] | None = None,
    ) -> tuple[list[ChangePoint], dict[str, Any]]:
        """返回 (变更点列表, 观测统计)。

        两种输入形态：
        - `prd_excerpt`（单篇）：群讨论只绑一个 Story 的老路径，变更不填 story_id，
          由调用方补。
        - `candidates`（多篇）：**一场会牵涉多个 Story / 多篇 PRD**。
          模型必须为每条变更回填归属的 story_id，代码按候选集校验——
          **清单外的归属直接丢弃**，因为写进别人的 PRD 比漏报代价高得多。

        统计含 candidates/dropped/flagged/tokens/cost_usd/latency_ms —— watcher 把它
        原样落 TaskRecord，这是调 `extract_min_confidence` 的唯一数据来源。
        """
        text = (discussion_text or "").strip()
        if not text:
            return [], {"candidates": 0, "dropped": 0, "flagged": 0}

        pool = {c.story_id: c for c in (candidates or []) if c.story_id}
        user = (_build_multi_frame(text, candidates or []) if pool
                else _build_user_frame(text, prd_excerpt))
        start_ms = _now_ms()
        stats: dict[str, Any] = {"candidates": 0, "dropped": 0, "flagged": 0,
                                 "unattributed": 0,
                                 "prompt_version": self.prompt_version}
        try:
            resp = await self._client.chat.completions.create(
                model=self._config.model,
                # 识别固定 temperature=0 且**不吃站点覆盖**：实测同一份会议
                # 纪要连跑 3 次）条数稳定在 6~7 条，但每条标题措辞每次不同 → 卡片上同一处
                # 变更换一种说法，用户体感就是"结果不稳定"。0 温度下措辞仍会漂移是模型
                # 本身的性质，但这已是最小值；再往上调只会更漂。
                temperature=0.0,
                # build_cacheable_messages 返回 list[dict]，openai SDK 要 TypedDict
                # （同 direct.py 遇到的存量问题，见 mypy.ini 豁免说明；此处局部收敛）
                messages=_cacheable(self._system_prompt, user),
                timeout=self._config.timeout_sec,
            )
        except Exception as e:  # 网关抖动不得伪装成「没有变更」，交上层记 error
            logger.warning("变更识别调用失败: %s", e)
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
            latency_ms=round(_now_ms() - start_ms, 1),
        )

        items = _parse_items(raw)
        if items is None:
            stats["error"] = f"识别输出非 JSON: {raw[:120]}"
            logger.warning(stats["error"])
            return [], stats

        stats["candidates"] = len(items)
        points: list[ChangePoint] = []
        for it in items:
            cp = _to_point(it)
            # 多 PRD 模式：归属不在候选集里就丢（模型可能编一个 StoryID，或把
            # 「持仓」的结论标到「申购」上——前者代码能拦，后者拦不住，所以
            # prompt 里要求"归属拿不准就不输出"。代码只保证不写错文档。）
            if pool and cp.story_id not in pool:
                stats["unattributed"] += 1
                logger.info("[prd_watch] 丢弃无归属变更：story_id=%r 不在 %d 篇候选里",
                            cp.story_id[:20], len(pool))
                continue
            if cp.confidence < self._wc.extract_min_confidence:
                stats["dropped"] += 1  # 只计数：误报率靠这个数降，不写出来就看不见
                continue
            if cp.confidence < self._wc.extract_flag_confidence:
                cp.band = BAND_FLAGGED
                stats["flagged"] += 1
            else:
                cp.band = BAND_NORMAL
            points.append(cp)
        return points, stats


def _cacheable(system: str, user: str) -> Any:
    """build_cacheable_messages 的结果按 Any 交给 SDK（存量类型缺口，见 mypy.ini）。"""
    return build_cacheable_messages(system, user)


def _build_user_frame(discussion: str, prd_excerpt: str) -> str:
    parts = [
        "【PRD 原文节选】（current_text/quote_text 只能从这里逐字引用）",
        prd_excerpt.strip()[:6000] if prd_excerpt else "（未提供，按新增条款处理）",
        "",
        "【讨论片段】（格式：[时间] 说话人: 内容）",
        discussion[:8000],
        "",
        "只输出 JSON。",
    ]
    return "\n".join(parts)


def _build_multi_frame(discussion: str, candidates: list[PrdCandidate]) -> str:
    """多篇 PRD 的输入帧：每篇带 StoryID 与标题，模型按 story_id 回填归属。

    节选长度按篇数分摊（总量 ~14k 字封顶）：5 篇各 2800 字比 5 篇各 6000 字更省，
    而识别只需要「这篇覆盖哪些模块」的骨架 + 关键条款，不需要整篇正文。
    """
    per = max(1200, min(4000, 14000 // max(1, len(candidates))))
    blocks: list[str] = []
    for c in candidates:
        blocks.append(
            f"——— story_id={c.story_id} ｜ {c.story_title} ———\n"
            f"{(c.excerpt or '（本篇正文取不到，不要归给它）').strip()[:per]}")
    parts = [
        f"【候选 PRD】共 {len(candidates)} 篇。每条变更必须归到其中一篇，"
        "在输出里回填该篇的 story_id；归属拿不准就**不要输出这条**。"
        "current_text/quote_text 只能逐字引用**它所归属那一篇**的原文。",
        "",
        "\n\n".join(blocks),
        "",
        "【讨论片段】（格式：[时间] 说话人: 内容；或会议纪要条目）",
        discussion[:8000],
        "",
        "只输出 JSON。",
    ]
    return "\n".join(parts)


def _parse_items(raw: str) -> list[dict[str, Any]] | None:
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    items = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _to_point(item: dict[str, Any]) -> ChangePoint:
    known = {k: v for k, v in item.items() if k in _FIELDS}
    # 类型归一：模型给的 modules 可能是字符串、is_new_clause 可能是 "true"，
    # 直接塞进 dataclass 会在渲染处炸（str 迭代成单字符是最难查的那种炸）
    mods = known.get("modules")
    if isinstance(mods, str):
        mods = re.split(r"[,，、/]+", mods)
    known["modules"] = [str(m).strip() for m in (mods or []) if str(m).strip()][:6]
    if isinstance(known.get("is_new_clause"), str):
        known["is_new_clause"] = known["is_new_clause"].strip().lower() in ("true", "1", "yes")
    for key in ("title", "current_text", "suggested_text", "quote_text",
                "change_type", "compliance", "evidence_quote", "speaker",
                "scenario", "rationale", "story_id"):
        if key in known:
            known[key] = str(known[key] or "").strip()
    cp = ChangePoint(**known)
    try:
        cp.confidence = max(0.0, min(1.0, float(cp.confidence)))
    except (TypeError, ValueError):
        cp.confidence = 0.0
    cp.band = BAND_DROPPED
    return cp


def _now_ms() -> float:
    import time

    return time.monotonic() * 1000
