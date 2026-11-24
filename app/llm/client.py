"""LLM 客户端 + Prompt Caching 基础设施。

能力（对齐 LLM Agent 工程迁移包能力清单）：
- build_cacheable_messages: 静态 System Prompt 前置 + 动态用户输入后置
  （前缀稳定 → 可走 Prompt Caching，降本提速）
- extract_cache_metrics: 从 API response usage 提取缓存指标
- CacheMetrics.hit_rate: 缓存命中率（<50% 提示需审查 Prompt 结构）
- estimate_cost: 模型级成本估算（默认值 + 可扩展定价表）

本工程通过 cc-connect / 路由 LLM 调外部 OpenAI 兼容 API。
缓存字段（cache_read_input_tokens / cache_creation_input_tokens）
在 API 未返回时自动降级为 0，不破坏现有调用链。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("llm")

# 历史总字符预算闸（近似 token≈字符/2）。SessionHistory 的构造上限 =
# history_max_turns(6) × 2 帧 × _MAX_CONTENT_LEN(2000) = 24K 字符；
# 此线取约 2 倍余量，正常会话永不触发——触发即上游截断被改坏的回归信号。
HISTORY_BUDGET_CHARS = 48_000


class CacheMetrics(BaseModel):
    """Prompt Caching 指标。

    Attributes:
        input_tokens: 非命中的输入 token（缓存未命中部分）。
        cache_read_input_tokens: 缓存命中并读取的输入 token。
        cache_creation_input_tokens: 用于创建缓存的输入 token（首写）。
        output_tokens: 输出 token。
    """

    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        """总输入 token（命中 + 创建 + 未命中）。"""
        return (
            self.cache_read_input_tokens
            + self.cache_creation_input_tokens
            + self.input_tokens
        )

    @property
    def hit_rate(self) -> float:
        """缓存命中率。

        公式：cache_read / (cache_read + cache_creation + input)。
        返回 0.0~1.0，< 0.5 时应审查 Prompt 结构（动态内容落在静态前缀中）。
        """
        total = self.total_input_tokens
        if total == 0:
            return 0.0
        return round(self.cache_read_input_tokens / total, 4)

    @property
    def cache_savings_ratio(self) -> float:
        """缓存带来的成本节省比例（命中 token 占总输入比例）。"""
        total = self.total_input_tokens
        if total == 0:
            return 0.0
        return round(self.cache_read_input_tokens / total, 4)


# ── 模型定价表（USD / 1K tokens）─────────────────────────────────────────
# 未收录的模型使用 DEFAULT_PRICES。新增模型时在此追加。
# 按 aigw 官方定价页（example.com/pricing）校准；cache = 缓存读价。

DEFAULT_PRICES: dict[str, float] = {"input": 0.0003, "output": 0.0015}

MODEL_PRICES: dict[str, dict[str, float]] = {
    # 当前默认模型（.env LLM_MODEL）
    "deepseek-flash": {"input": 0.00014, "output": 0.00028},
    "qwen3.8-flash": {"input": 1.4e-07, "output": 4.3e-07, "cache": 1e-08},
    # 铁律模型：glm-5.3-flash（aigw 定价 $0.075/$0.25/$0.015 每 1M）
    "glm-5.3-flash": {"input": 0.000075, "output": 0.00025, "cache": 0.000015},
    # Anthropic Claude 系列（历史定价，仅供旧记录对账）
    "claude-opus-4.8": {"input": 0.015, "output": 0.075},
    "claude-opus-4.6": {"input": 0.015, "output": 0.075},
    "claude-sonnet-4.5": {"input": 0.003, "output": 0.015},
    "claude-haiku-4.5": {"input": 0.001, "output": 0.005},
    # DeepSeek 系列（路由层）
    "deepseek-v4-flash": {"input": 0.00014, "output": 0.00028},
    "deepseek-chat": {"input": 0.00014, "output": 0.00028},
    # GLM 系列（cc-connect provider）
    "glm-5.1": {"input": 0.0002, "output": 0.0002},
}


def get_model_prices(model: str) -> dict[str, float]:
    """获取模型定价，未收录时返回默认值。"""
    prices = MODEL_PRICES.get(model)
    if prices is None:
        # 尝试匹配前缀（如 claude-sonnet-4-5 → sonnet 系列）
        for key, p in MODEL_PRICES.items():
            if model and model.startswith(key.split("-")[0]):
                return p
        return DEFAULT_PRICES
    return prices


def get_model_name() -> str:
    """返回默认模型名（供调用方打点）。"""
    from app.config import load_config

    cfg = load_config()
    return cfg.router.model or "deepseek-flash"


def extract_cache_metrics(usage: Any) -> CacheMetrics:
    """从 OpenAI-compatible API response usage 提取缓存指标。

    Args:
        usage: resp.usage（openai 库对象或 dict）。

    Returns:
        CacheMetrics，缺失字段降级为 0。
    """
    if not usage:
        return CacheMetrics()

    def _get(attr: str) -> int:
        val = getattr(usage, attr, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(attr)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    return CacheMetrics(
        input_tokens=_get("prompt_tokens"),
        cache_read_input_tokens=_get("cache_read_input_tokens"),
        cache_creation_input_tokens=_get("cache_creation_input_tokens"),
        output_tokens=_get("completion_tokens"),
    )


def build_cacheable_messages(
    system_prompt: str,
    user_content: str,
    *,
    cache_breakpoint: bool = False,
    history: list[dict[str, Any]] | None = None,
    history_budget_chars: int = HISTORY_BUDGET_CHARS,
) -> list[dict[str, Any]]:
    """构建支持前缀缓存的 messages。

    原则（对齐迁移包 §4.1 结构重组）：
    - [0] system: 稳定 System Prompt（可缓存前缀）
    - [1..] 可选多轮历史（role=user/assistant，最旧在前）
    - [-1] user: 最新动态内容（不破坏前缀一致性）

    历史预算闸（回归防护，非压缩机制）：SessionHistory 已保证 6 轮 × 2000 字符
    （≤24K）的构造性上限，正常会话永不触发此闸；一旦触发说明历史来源异常
    （上游截断被改坏 / 未来放宽轮数），从最旧帧起丢到预算内并记 warning——
    爆炸会拖慢响应、稀释路由注意力，必须显式留痕而非静默吞掉。

    Args:
        system_prompt: 静态 System Prompt。
        user_content: 动态用户输入。
        cache_breakpoint: 是否需要显式缓存断点
                          （[OI] 自动缓存忽略；保留以兼容 Anthropic）。
        history: 该会话最近 N 轮对话历史（最旧在前）。可为 None。
        history_budget_chars: 历史总字符预算（近似口径：token≈字符/2）。

    Returns:
        messages 列表。
    """
    system_block: dict[str, Any] = {"role": "system", "content": system_prompt}
    if cache_breakpoint:
        system_block["cache_control"] = {"type": "ephemeral"}
    msgs: list[dict[str, Any]] = [system_block]
    if history:
        # 过滤空 content 帧：部分模型 API 拒绝空 assistant 消息，
        # 且空帧会稀释前缀缓存命中率（相邻请求历史不一致）
        frames = [h for h in history if str(h.get("content", "") or "").strip()]
        dropped = 0
        while (
            len(frames) > 1  # 至少保最新 1 帧
            and sum(len(str(h.get("content", ""))) for h in frames) > history_budget_chars
        ):
            frames = frames[1:]  # 丢最旧一帧
            dropped += 1
        if dropped:
            logger.warning(
                "History budget exceeded: kept %d newest frames, dropped %d oldest "
                "(budget=%d chars) — check upstream truncation regression",
                len(frames), dropped, history_budget_chars,
            )
        msgs.extend(frames)
    msgs.append({"role": "user", "content": user_content})
    return msgs


def record_llm_run(token_usage: dict[str, Any], metrics: CacheMetrics) -> dict[str, Any]:
    """把缓存指标合并进 token_usage，供监控/评估落库。

    Args:
        token_usage: 原始 token 用量 dict（prompt_tokens 等）。
        metrics: 提取出的缓存指标。

    Returns:
        合并后的 dict，含 cache_read_input_tokens / cache_creation_input_tokens / cache_hit_rate。
    """
    return {
        **token_usage,
        "cache_read_input_tokens": metrics.cache_read_input_tokens,
        "cache_creation_input_tokens": metrics.cache_creation_input_tokens,
        "cache_hit_rate": metrics.hit_rate,
    }


def estimate_cost(model: str, token_usage: dict[str, Any]) -> float:
    """按模型定价估算单次调用成本（缓存读 token 按 cache 价单独计）。

    Args:
        model: 模型名（如 "glm-5.3-flash"）。
        token_usage: 含 prompt_tokens/completion_tokens 的 dict；
            可选 cache_read_input_tokens（prompt_tokens 中命中缓存的部分）。

    Returns:
        成本（USD），保留 8 位小数（单次调用常为微美元级，6 位会抹零）。
    """
    prices = get_model_prices(model)
    input_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(token_usage.get("completion_tokens", 0) or 0)
    cache_read = min(int(token_usage.get("cache_read_input_tokens", 0) or 0), input_tokens)
    cache_price = prices.get("cache", prices["input"])
    cost = (
        (input_tokens - cache_read) * prices["input"]
        + cache_read * cache_price
        + output_tokens * prices["output"]
    ) / 1000
    return round(cost, 8)