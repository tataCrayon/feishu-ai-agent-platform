"""调用方上下文（contextvars）— 进程内 Handler 感知消息来源。

crew 的 agent 节点只把 query 传给 Handler，sender/chat 上下文原本到不了
Handler。这里用 contextvars 在同一 asyncio Task 作用域内传递（并发安全，
不同消息各自独立 context）：

    crew._run_handler_agent  →  set_caller_context({...})
    Handler.execute_tool     →  get_caller_context()

未设置时 get 返回空 dict（decompose 子步等路径无上下文，Handler 须兜底引导）。
"""

from __future__ import annotations

from contextvars import ContextVar

_caller_ctx: ContextVar[dict[str, str] | None] = ContextVar("caller_ctx", default=None)


def set_caller_context(ctx: dict[str, str]) -> None:
    """在当前 asyncio Task 作用域内写入调用方上下文（sender_id/chat_id 等）。"""
    _caller_ctx.set(dict(ctx))


def get_caller_context() -> dict[str, str]:
    """读取当前调用方上下文；未设置返回空 dict（返回副本防调用方误改共享态）。"""
    return dict(_caller_ctx.get() or {})


__all__ = ["get_caller_context", "set_caller_context"]
