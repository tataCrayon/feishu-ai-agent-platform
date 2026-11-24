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

from app.llm.client import (
    DEFAULT_PRICES,
    MODEL_PRICES,
    CacheMetrics,
    build_cacheable_messages,
    estimate_cost,
    extract_cache_metrics,
    get_model_prices,
    record_llm_run,
)

__all__ = [
    "DEFAULT_PRICES",
    "MODEL_PRICES",
    "CacheMetrics",
    "build_cacheable_messages",
    "estimate_cost",
    "extract_cache_metrics",
    "get_model_prices",
    "record_llm_run",
]