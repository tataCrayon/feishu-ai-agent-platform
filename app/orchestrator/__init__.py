"""编排器入口 — 意图识别、路由、安全门禁与响应聚合。

三层路由（全部 LLM 驱动，无正则）：
1. LangGraph CrewOrchestrator（主路径）— 意图识别 + 复杂任务编排
2. 分发前置安全门禁，保护敏感操作

意图识别 100% 由 LLM 驱动（RouterLLM 的 OpenAI Function Calling），
无正则匹配、无关键词表、无静态路由。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.orchestrator.crew import (
    CrewOrchestrator,
    create_orchestrator,
    set_attachment_downloader,
    set_late_delivery_sender,
)

# 显式导出面：main.py / Handler / 审批链路从包入口取编排器与权限判定，
# 不直接摸 crew 子模块。
__all__ = [
    "CrewOrchestrator",
    "UserPermission",
    "create_orchestrator",
    "get_user_permission",
    "load_whitelist",
    "set_attachment_downloader",
    "set_late_delivery_sender",
]

logger = logging.getLogger("orchestrator")

# ── Paths ──────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/orchestrator/ -> feishu-agent-platform/
WHITELIST_FILE = PROJECT_DIR / "bot" / "todos" / "whitelist.json"


# ── Data types ──────────────────────────────────────────────────


@dataclass
class UserPermission:
    level: int  # 0 = guest, 1 = authorized, 2 = admin
    name: str = ""


# ── Permission ──────────────────────────────────────────────────


def load_whitelist() -> dict:
    """从 bot/todos/whitelist.json 加载权限白名单；文件缺失返回空表。"""
    if not WHITELIST_FILE.exists():
        return {"users": {}, "groups": {}, "level_permissions": {}}
    return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))


def get_user_permission(sender_id: str, chat_id: str = "") -> UserPermission:
    """判定用户权限等级：用户级 > 群级 > 默认游客（L0）。

    权限是所有敏感操作的闸门，非游客判定打 debug 日志留痕。
    """
    wl = load_whitelist()
    users = wl.get("users", {})
    groups = wl.get("groups", {})

    # 用户级权限优先于群级
    if sender_id in users:
        perm = UserPermission(
            level=users[sender_id].get("level", 0),
            name=users[sender_id].get("name", ""),
        )
        logger.debug("[permission] user=%s(%s) -> L%s", perm.name or sender_id, sender_id, perm.level)
        return perm

    # 群级权限（群成员默认等级）
    if chat_id in groups:
        perm = UserPermission(level=groups[chat_id].get("level", 0))
        logger.debug("[permission] group=%s member=%s -> L%s", chat_id, sender_id, perm.level)
        return perm

    return UserPermission(level=0)  # 默认：游客
