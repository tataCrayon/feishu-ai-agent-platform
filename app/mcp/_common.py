"""MCP 工具共用件 — 回执整形、配置单例、飞书三红线判定。

负责：三个 server 共用的 JSON 回执、配置缓存、前缀/长度/文档链接校验。
不负责：任何业务调用与协议收发（各 server 模块 + 下层 domains/protocols）。
调用方：`app.mcp.feishu_server` / `domains_server` / `order_agent_server`。

失败语义：校验函数返回错误文案（空串=通过）不抛异常；回执函数恒返回 JSON 字符串，
保证工具调用方（模型）看到的是可改参数的拒绝原因，而不是框架异常。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.config import AppConfig, load_config

logger = logging.getLogger("mcp.common")

# 飞书会话 ID 形态：群 oc_*、用户 ou_*。这是发消息目标的第一道硬闸，
# 不在提示词里靠模型自觉——模型可以改写参数，工具层不行。
CHAT_ID_PREFIXES = ("oc_", "ou_")
# 飞书消息 ID 形态：om_*。回复目标同样只认机器人可寻址的消息。
MESSAGE_ID_PREFIX = "om_"
# lark-cli `--doc` 既收文档链接也收裸 token
_DOC_URL_RE = re.compile(r"^https://[\w.-]+\.(?:feishu\.cn|larksuite\.com)/")
_DOC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")

_config: AppConfig | None = None


def app_config() -> AppConfig:
    """进程内配置单例。MCP server 是短生命周期子进程，配置只读一次。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """清配置缓存——单测替换配置用；生产进程不会调用。"""
    global _config
    _config = None


def ok(**payload: Any) -> str:
    """成功回执。"""
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def fail(error: str) -> str:
    """失败回执：error 必须写清「发生了什么」，能让调用方决定下一步。"""
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False)


def reject(reason: str) -> str:
    """红线拦截回执。前缀固定 `REJECT:`，与 dsh 探针断言口径一致。"""
    return fail(f"REJECT: {reason}")


def check_chat_id(chat_id: str) -> str:
    """会话 ID 必须以 oc_/ou_ 开头。返回错误文案，空串=通过。"""
    if not (chat_id or "").strip().startswith(CHAT_ID_PREFIXES):
        return f"chat_id 必须以 oc_（群）或 ou_（用户）开头，收到 {chat_id!r}"
    return ""


def check_message_id(message_id: str) -> str:
    """回复目标必须是机器人可寻址的消息 ID（om_ 开头）。"""
    if not (message_id or "").strip().startswith(MESSAGE_ID_PREFIX):
        return f"message_id 必须以 {MESSAGE_ID_PREFIX} 开头，收到 {message_id!r}"
    return ""


def current_chat_id() -> str:
    """本会话的 chat_id（MCP 子进程启动时由 main.py 按会话注入 env）。

    dsh 的 MCP 子进程是**按会话**拉起的（实测：同 spec 两个 session 两个 pid），
    所以 env 里固定的 chat_id 就是「发这条消息的那个会话」。返回空串表示没有
    注入（离线回归/手工挂载），调用方应要求用户显式给 chat_id。
    """
    return (os.environ.get("PROMATE_CALLER_CHAT_ID") or "").strip()


def sender_name() -> str:
    """本会话发送者的显示名（env 注入，与 chat_id 同一份身份）。

    谁提问就是谁——卡片上「来自某某」只能取这里，不能取模型填的参数。
    """
    return (os.environ.get("PROMATE_CALLER_SENDER_NAME") or "").strip()


def check_text(text: str, max_chars: int, field: str = "text") -> str:
    """文本非空且不超长。超长不静默截断——截断会把语义切坏，交由调用方自己裁。"""
    if not (text or "").strip():
        return f"{field} 不能为空"
    if len(text) > max_chars:
        return f"{field} 长度 {len(text)} 超过上限 {max_chars} 字，请拆分后重发"
    return ""


def check_doc_target(doc_url: str) -> str:
    """文档目标只认飞书域链接或裸 token，避免把任意 URL 交给 lark-cli。"""
    target = (doc_url or "").strip()
    if not target:
        return "doc_url 不能为空"
    if _DOC_URL_RE.match(target) or _DOC_TOKEN_RE.match(target):
        return ""
    return f"doc_url 必须是飞书文档链接（feishu.cn / larksuite.com）或文档 token，收到 {doc_url!r}"
