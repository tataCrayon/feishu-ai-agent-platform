"""编排层自答引擎 — direct_answer 场景的专门生成环节。

此前 direct_answer 的回复由路由模型（deepseek-v4-flash）在 function-call
参数里顺手写一句 reply，质量差且不注入对话历史。本模块把自答升级为
独立 LLM 调用：专门的 system prompt + 多轮历史注入，短问候仍走
路由模型的 reply 秒回，一般问题认真回答。

提示词单源：bot/prompts/router/direct_answer_system_prompt.md（PromptLoader 加载）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import openai

from app.config import RouterConfig
from app.llm import build_cacheable_messages, estimate_cost, extract_cache_metrics
from app.multimodal import build_user_content
from app.prompts.loader import PromptLoader

logger = logging.getLogger("orchestrator.direct")

DIRECT_ANSWER_SYSTEM_PROMPT = PromptLoader.load(
    "router/direct_answer_system_prompt.md",
    fallback="你是飞书群聊智能助手 AgentMate。直接、简洁、准确地回答用户，中文回复，≤300字。",
)

# 短输入阈值：≤该长度且有路由模型 reply 时直接用 reply（秒回路径）。
# 中文问候/感谢/告别基本 ≤6 字；阈值过高会把短问题（如"缓存是什么？"）
# 误判为闲聊，用路由模型顺手写的低质量 reply 作答。
_GREETING_MAX_LEN = 8


class DirectAnswerLLM:
    """自答生成器 — 复用 RouterConfig 的模型配置。"""

    def __init__(self, config: RouterConfig):
        self._config = config
        self._client = openai.AsyncClient(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=config.max_retries,
        )
        self._prompt_version = PromptLoader.get_prompt_version(
            "router/direct_answer_system_prompt.md"
        )

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def should_fast_reply(self, query: str, router_reply: str) -> bool:
        """短问候/闲聊 → 直接用路由模型的 reply（免二次调用）。"""
        q = (query or "").strip()
        return bool(router_reply) and len(q) <= _GREETING_MAX_LEN

    async def generate(
        self,
        query: str,
        history: list[dict] | None = None,
        runtime_context: str = "",
        media_bundle: Any | None = None,
    ) -> dict[str, Any]:
        """生成自答回复。

        Args:
            query: 用户消息。
            history: 会话多轮历史（最旧在前）。
            runtime_context: 运行时事实上下文（如含版本号的能力清单），
                拼到 user 帧尾部——不放进 system（破坏前缀缓存）。
            media_bundle: MediaBundle 多模态媒体包（图片/视频/文档），
                非空时 user 帧升级为图文混合 content 数组。

        Returns:
            {"reply": str, "latency_ms": float, "tokens": int, "cost_usd": float}
            失败时 reply 为空字符串（调用方回退到 router_reply）。
        """
        start = time.time()
        try:
            user_content = query
            if runtime_context:
                user_content = f"{query}\n\n{runtime_context}"
            user_content_any: Any = build_user_content(user_content, media_bundle)
            msgs = build_cacheable_messages(
                DIRECT_ANSWER_SYSTEM_PROMPT,
                user_content_any,
                history=history,
            )
            resp = await self._client.chat.completions.create(
                model=self._config.model,
                **self._config.sampling_params("direct"),
                messages=msgs,
                timeout=self._config.timeout_sec,
            )
            latency_ms = (time.time() - start) * 1000
            usage = resp.usage
            cache_metrics = extract_cache_metrics(usage)
            cost = estimate_cost(self._config.model, {
                "prompt_tokens": cache_metrics.input_tokens,
                "completion_tokens": cache_metrics.output_tokens,
                "cache_read_input_tokens": cache_metrics.cache_read_input_tokens,
            })
            reply = (resp.choices[0].message.content or "").strip()
            tokens = usage.total_tokens if usage else 0
            logger.info(
                "DirectAnswer generated (%.0fms, %d tokens): %s",
                latency_ms, tokens, reply[:60],
            )
            return {
                "reply": reply,
                "latency_ms": latency_ms,
                "tokens": tokens,
                "cost_usd": cost,
                "prompt_version": self._prompt_version,
            }
        except Exception as e:
            logger.warning("DirectAnswer generate failed: %s", e)
            return {
                "reply": "",
                "latency_ms": (time.time() - start) * 1000,
                "tokens": 0,
                "cost_usd": 0.0,
                "prompt_version": self._prompt_version,
            }
