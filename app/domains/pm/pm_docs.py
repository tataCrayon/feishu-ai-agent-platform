"""读飞书文档 —— 能力层出口，只转交不改写。

复用的是 `app.protocols.lark_cli.LarkCli`（**user 身份**，需本机已登录）：
bot 应用身份只有 `im` 权限，`wiki` / `docx` / `drive` 一律 403。这是「读消息用
user token」同一条约束的延伸，不是这里的特殊选择。

职责边界：返回 `token` / `revision_id` / `content` / `truncated`，不解析、不总结。
「这份文档在说什么」是调用方的事。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger("pm_hub.docs")


def read_doc(doc_url: str, max_chars: int = 4000) -> Dict[str, Any]:
    """读文档正文（同步封装，内部起一个事件循环跑 async 客户端）。

    为什么在里层起循环而不是让调用方 await：HTTP 路由是异步的，但 `pm_tools`
    这类同步调用点也要用同一个入口。`LarkCli` 内部跑的是子进程（lark-cli），
    没有真实的 I/O 并发需求，起个短循环是安全的。

    已在事件循环里调用会抛 `RuntimeError`——那种情况下由调用方直接 await
    `LarkCli().get_doc_content`（异步路由就是这么做的）。
    """
    from app.protocols.lark_cli import LarkCli

    async def _run() -> Dict[str, Any]:
        res = await LarkCli().get_doc_content(doc_url, max_chars=max_chars)
        return {
            "token": res["token"],
            "revision_id": res["revision_id"],
            "content": res["content"],
            "truncated": len(res["content"]) >= max_chars,
        }

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    raise RuntimeError("已在事件循环内：请直接 await LarkCli().get_doc_content")


async def read_doc_async(doc_url: str, max_chars: int = 4000) -> Dict[str, Any]:
    """异步版，给 HTTP 路由用。"""
    from app.protocols.lark_cli import LarkCli

    res = await LarkCli().get_doc_content(doc_url, max_chars=max_chars)
    return {
        "token": res["token"],
        "revision_id": res["revision_id"],
        "content": res["content"],
        "truncated": len(res["content"]) >= max_chars,
    }
