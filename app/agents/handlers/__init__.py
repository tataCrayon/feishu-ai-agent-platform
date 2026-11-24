"""Agent Handler 基类 — 统一 LLM + Tool Calling 模式。

每个独立 Agent 遵循：
1. 加载 System Prompt（从 PromptLoader）
2. 构建 Function Calling 工具列表
3. LLM 选择工具 + 传参
4. Python 执行工具
5. 结果返回 LLM → 格式化回复
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

import openai

from app.config import RouterConfig
from app.llm import (
    CacheMetrics,
    build_cacheable_messages,
    extract_cache_metrics,
)
from app.observability import AgentTrace, TraceContext, tracer
from app.prompts.loader import PromptLoader

logger = logging.getLogger("agents.handlers.base")


class ToolResult:
    """工具执行结果。"""

    success: bool
    data: Any
    error: str = ""

    def __init__(self, data: Any = None, error: str = ""):
        self.success = error == ""
        self.data = data
        self.error = error


class AgentHandler:
    """Agent Handler 基类。

    Subclass 需要：
    - 定义 agent_name（对应 PromptLoader 和 ROUTER_TOOLS）
    - 定义 tools（Function Calling 工具列表）
    - 实现 execute_tool() 方法执行具体工具
    """

    agent_name: str = ""
    """Agent 名称，用于加载 Prompt 和路由。"""

    tools: ClassVar[list[dict[str, Any]]] = []
    """Function Calling 工具定义列表。"""

    MAX_TOOL_ROUNDS = 4
    """单次 run 允许的最大工具调用轮数（防失控循环）。"""

    def __init__(self, config: RouterConfig):
        self._config = config
        self._client = openai.AsyncClient(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=config.max_retries,
        )
        # 加载 System Prompt
        self._system_prompt = self._load_prompt()
        self._prompt_version = PromptLoader.get_prompt_version(
            f"agents/{self.agent_name}.md"
        )

    def _load_prompt(self) -> str:
        """加载 Agent 的 System Prompt。"""
        return PromptLoader.load_agent_prompt(self.agent_name)

    def _new_trace(
        self,
        chat_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        trace_id: str = "",
        span_id: str = "",
        parent_span_id: str = "",
    ) -> TraceContext:
        """构建 handler 的 TraceContext（有外部链时承接，否则新建）。"""
        if trace_id and span_id:
            return TraceContext(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
            )
        return TraceContext.new(chat_id, sender_id, sender_name)

    def _trace(
        self,
        ctx: TraceContext,
        *,
        input_text: str,
        output_text: str = "",
        status: str = "success",
        duration_ms: float = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_metrics: CacheMetrics | None = None,
        error: str | None = None,
    ) -> None:
        """写入一条 AgentTrace（含 token/cache/cost 归因）。"""
        trace = AgentTrace(
            agent=self.agent_name,
            intent=self.agent_name,
            chat_id=ctx.chat_id,
            sender_id=ctx.sender_id,
            sender_name=ctx.sender_name,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            input_text=input_text[:200],
            output_text=output_text[:200],
            status=status,
            duration_ms=duration_ms,
            model=self._config.model,
            prompt_version=self._prompt_version,
            error=error,
        ).with_usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_metrics=cache_metrics,
            model=self._config.model,
        )
        tracer.trace(trace)

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        """执行工具调用。Subclass 必须实现。"""
        raise NotImplementedError

    def set_progress_callback(self, cb) -> None:
        """注入处理进度回调（豆包式进度卡数据源；crew 分派前后各调一次还原）。

        cb 签名：async fn(event: dict)，event = {"stage", "detail", "tool", "seq"}。
        None = 关闭上报。基类默认持有，子类在工具循环里调 _emit_progress。
        """
        self._progress_cb = cb

    async def _emit_progress(self, stage: str, detail: str = "", tool: str = "") -> None:
        """向进度回调发一条事件（fire-and-forget，失败静默——增强项不挡主链）。"""
        cb = getattr(self, "_progress_cb", None)
        if cb is None:
            return
        try:
            seq = getattr(self, "_progress_seq", 0) + 1
            self._progress_seq = seq
            import asyncio as _asyncio
            task = _asyncio.ensure_future(cb({
                "stage": stage, "detail": detail[:120], "tool": tool, "seq": seq,
            }))
            task.add_done_callback(lambda t: t.exception())  # 吞掉回调异常（fire-and-forget）
        except Exception:
            pass

    def _tool_narrative(self, tool_name: str, args: dict, done: bool) -> str:
        """工具事件 → 叙事化文案（主流 bot 思考过程形态，不暴露内部工具名）。

        子类可覆写提供领域化措辞；默认给出通用安全句式。
        """
        return f"处理中：{tool_name}" if not done else f"已完成 {tool_name}"

    async def run(self, query: str, history: list[dict] | None = None) -> str:
        """执行完整 Agent 处理流程。

        Args:
            query: 用户查询。
            history: 该会话最近 N 轮对话历史（可选），用于承接上文。

        Returns:
            回复文本。
        """
        start = time.time()
        # 独立 Handler 无外部 trace 上下文（主调用方不传）→ 新建根 span 记录本次调用
        ctx = self._new_trace()
        self._progress_seq = 0
        final_text = ""
        total_ms = 0.0
        status = "success"
        error: str | None = None

        try:
            msgs = build_cacheable_messages(self._system_prompt, query, history=history)

            import json

            final_text = ""
            total_tokens = 0
            # 多轮工具循环：LLM 可连续调用工具（如 list→generate 实测）直到给出
            # 最终答复。此前只支持单轮 tool→answer，模型想再验证时只能输出
            # 半截话（「再实测一次生成：…」）被原样发给用户。
            tokens_in = 0
            tokens_out = 0
            cache_metrics = None
            for _round in range(self.MAX_TOOL_ROUNDS):
                resp = await self._client.chat.completions.create(
                    model=self._config.model,
                    **self._config.sampling_params("handler"),
                    messages=msgs,
                    tools=self.tools,
                    tool_choice="auto",
                    timeout=self._config.timeout_sec,
                )

                choice = resp.choices[0]
                total_tokens += resp.usage.total_tokens if resp.usage else 0
                cache_metrics = extract_cache_metrics(resp.usage)
                tokens_in += cache_metrics.input_tokens
                tokens_out += cache_metrics.output_tokens

                # LLM 选择了工具调用 → 执行本轮全部 tool_calls 后进入下一轮
                if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                    msgs.append({
                        "role": "assistant",
                        "content": choice.message.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id or f"call_{i}",
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for i, tc in enumerate(choice.message.tool_calls)
                        ],
                    })
                    for i, tool_call in enumerate(choice.message.tool_calls):
                        tool_name = tool_call.function.name
                        try:
                            args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError as e:
                            # LLM 生成的参数偶发非法 JSON（小模型长嵌套参数高发）：
                            # 错误回填给 LLM 让它重试，不让整个 handler 崩掉
                            status = "error"
                            error = f"tool args JSON invalid: {e}"
                            msgs.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id or f"call_{i}",
                                "content": f"工具参数不是合法 JSON（{e}），请修正后重新调用",
                            })
                            continue

                        logger.info(
                            "%s: tool=%s args=%s (round %d/%d, %.0fms, %d tokens)",
                            self.agent_name, tool_name, str(args)[:100],
                            _round + 1, self.MAX_TOOL_ROUNDS,
                            (time.time() - start) * 1000, total_tokens,
                        )

                        # 进度上报（叙事化）：工具开始
                        await self._emit_progress(
                            "tool_start", self._tool_narrative(tool_name, args, done=False),
                            tool_name,
                        )

                        # 执行工具
                        tool_result = await self.execute_tool(tool_name, args)

                        # 工具结束事件
                        await self._emit_progress(
                            "tool_done",
                            self._tool_narrative(tool_name, args, done=True)
                            if tool_result.success
                            else f"{tool_name} 未成功：{tool_result.error}",
                            tool_name,
                        )

                        if not tool_result.success:
                            # 失败不直接打断：把错误回填给 LLM，由它组织诚实答复
                            # （如「日报生成实测失败：X」），trace 仍记 error。
                            status = "error"
                            error = tool_result.error[:200]
                            msgs.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id or f"call_{i}",
                                "content": f"工具执行失败：{tool_result.error[:200]}",
                            })
                            continue

                        msgs.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id or f"call_{i}",
                            "content": str(tool_result.data),
                        })
                    continue
                elif choice.message.content:
                    # LLM 直接给出最终答复（无工具调用）→ 采纳并结束循环。
                    # 此前缺这个分支：直接答复被丢弃、同一请求最多重复打 4 轮
                    # LLM 才走「轮数耗尽」兜底，白烧 token 且拖长整链延迟。
                    final_text = choice.message.content
                    break

            # 工具轮数耗尽仍未给最终答复：带着全部工具结果再要一次最终答复
            # （此前直接返回空串，用户收到空白——数据都取回来了却丢在最后一跳）
            if not final_text:
                logger.warning("%s: tool rounds exhausted, forcing final answer", self.agent_name)
                msgs.append({
                    "role": "user",
                    "content": "工具调用轮数已用完。请基于以上已取回的信号数据立即给出最终答复，"
                               "不要再调用任何工具；未取到的信号如实标注不可用。",
                })
                resp = await self._client.chat.completions.create(
                    model=self._config.model,
                    **self._config.sampling_params("handler"),
                    messages=msgs,
                    timeout=self._config.timeout_sec,
                )
                final_text = resp.choices[0].message.content or ""
                if resp.usage:
                    total_tokens += resp.usage.total_tokens
                    cache_metrics = extract_cache_metrics(resp.usage)
                    tokens_in += cache_metrics.input_tokens
                    tokens_out += cache_metrics.output_tokens
            if not final_text:
                logger.warning("%s: LLM returned empty response", self.agent_name)
                final_text = "⚠️ 处理失败，请稍后再试"
                status = "error"
                error = "empty response"

            total_ms = (time.time() - start) * 1000
            logger.info(
                "%s: done (%.0fms, %d total tokens)",
                self.agent_name, total_ms, total_tokens,
            )

            self._trace(
                ctx, input_text=query, output_text=final_text,
                status=status, duration_ms=total_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
                cache_metrics=cache_metrics, error=error,
            )
            return final_text

        except Exception as e:
            logger.exception("%s: handler error: %s", self.agent_name, e)
            total_ms = (time.time() - start) * 1000
            self._trace(
                ctx, input_text=query,
                status="error", duration_ms=total_ms,
                error=str(e)[:100],
            )
            return f"⚠️ 处理出错: {str(e)[:100]}"


class AgentHandlerRegistry:
    """Agent Handler 注册表，管理名称到 Handler 的映射。"""

    def __init__(self):
        self._handlers: dict[str, AgentHandler] = {}

    def register(self, name: str, handler: AgentHandler):
        """注册一个 Agent Handler。"""
        self._handlers[name] = handler

    def get(self, name: str) -> AgentHandler | None:
        """获取 Agent Handler。"""
        return self._handlers.get(name)

    def has_handler(self, name: str) -> bool:
        """检查是否有 Handler。"""
        return name in self._handlers

    def list(self) -> list[str]:
        """列出所有已注册的 Handler 名称。"""
        return list(self._handlers.keys())


# 约定式自动发现（放末尾导入 — discovery 依赖上面的基类与注册表）
from app.agents.handlers.discovery import auto_discover_handlers  # noqa: E402

__all__ = ["AgentHandler", "AgentHandlerRegistry", "ToolResult", "auto_discover_handlers"]