"""skill-catalog-agent — 团队 Skill 目录 + 规范状态：查询、立项查重、清单审查、状态报告。

只读本地快照。刷新是唯一会访问 Git 的动作，且失败不覆盖上一份快照。
回复由工具拼好，模型只负责选工具和原样转述，不总结、不判断能不能立项。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.handlers import AgentHandler, ToolResult
from app.domains.skill_catalog.query import (
    find_similar,
    find_status_entries,
    format_catalog,
    format_inventory_overview,
    format_review,
    format_similar,
    format_status_board,
    format_status_card,
    review_entry,
)
from app.domains.skill_catalog.store import CatalogStore

logger = logging.getLogger("agents.handlers.skill_catalog")


SKILL_CATALOG_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_team_skills",
            "description": "列出团队 Skill 目录。用户问「我们/团队有哪些 Skill、Agent、"
                "AI 能力、技能」这类资产盘点问题时用 view=overview（紧凑概览，"
                "同名多分支合并）；问「建设情况/某分支有什么」用 view=full；"
                "用户点名分支时传 branch 展开",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "分支名，如 infra-ops-skills。不限定则传空字符串",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["overview", "full"],
                        "description": "overview=资产盘点紧凑视图（默认，~31 行按链分组）；"
                            "full=全量目录（逐分支条目带 Git 链接）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_similar_skills",
            "description": "立项检查：按名字和 description 找最接近的现成 Skill。"
                "用户说「我想做 XXX / 有没有现成的 / 会不会重复建设」时使用。"
                "没有命中也要如实说没有，不要说可以立项",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用户想建设的能力，保留原话关键词",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_skill",
            "description": "审查某个 Skill：对照 owner / 明确不做 / 复审日 / version 打勾，"
                "并给出该文件所在分支的 Git 链接。不打通过或不通过",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill 名字，如 nacos-guard",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_status_board",
            "description": "团队 Skill 建设现状总表（规范状态）。用户问「技能建设情况 / 规范状态 / "
                "去留表 / 有多少生产了 / 治理缺口 / 谁在提交 / 哪些分支没合」时使用。"
                "返回状态计数、八项字段缺口、近 30 天活跃与分支在飞",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_status",
            "description": "查某个 Skill 的规范状态：草稿/生产/未验证/下线候选 + 八项字段逐项打勾。"
                "用户问「XX 技能什么状态 / 是生产了吗 / 缺什么字段」时使用。"
                "复审过期是状态变化（自动降级为未验证），不是提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill 名字，如 flp-database-design",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_skill_catalog",
            "description": "手动刷新团队 Skill 目录（git fetch）。"
                "用户明确说「刷新 Skill 目录 / 重新拉仓库」时才使用，不要在普通查询时调用",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class SkillCatalogHandler(AgentHandler):
    """团队 Skill 目录。工具返回的文本就是给用户的回复。"""

    agent_name = "skill-catalog-agent"
    tools = SKILL_CATALOG_TOOLS

    def __init__(self, config, store: CatalogStore):
        super().__init__(config)
        self._store = store

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        if tool_name == "refresh_skill_catalog":
            return await self._refresh()
        snap = self._store.load()
        if snap is None or self._store.is_stale(snap):
            snap, note = await self._store.refresh(force=snap is None)
            if snap is None:
                return ToolResult(error=note)
            # 过期触发的刷新把状态句留给本次回复，避免用户以为这是实时的
            prefix_note = "" if note == "已刷新" else note
        else:
            prefix_note = ""

        if tool_name == "list_team_skills":
            view = str(args.get("view") or "overview").strip().lower()
            branch = str(args.get("branch") or "")
            if view == "full":
                text = format_catalog(snap, branch=branch)
            else:
                # overview：资产盘点紧凑视图（点名 branch 时 full 才有意义，
                # overview 忽略 branch——分支明细是 full 的职责）
                text = format_inventory_overview(snap)
        elif tool_name == "find_similar_skills":
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolResult(error="缺少要检查的能力描述")
            text = format_similar(snap, query, find_similar(snap, query))
        elif tool_name == "review_skill":
            name = str(args.get("name") or "").strip()
            if not name:
                return ToolResult(error="缺少 Skill 名字")
            text = format_review(review_entry(snap, name), snap)
        elif tool_name == "get_skill_status_board":
            text = format_status_board(snap)
        elif tool_name == "get_skill_status":
            name = str(args.get("name") or "").strip()
            if not name:
                return ToolResult(error="缺少 Skill 名字")
            text = format_status_card(find_status_entries(snap, name), snap)
        else:
            return ToolResult(error=f"未知工具: {tool_name}")
        if prefix_note:
            text = f"{prefix_note}\n\n{text}"
        return ToolResult(data=text)

    async def _refresh(self) -> ToolResult:
        snap, note = await self._store.refresh(force=True)
        if snap is None:
            return ToolResult(error=note)
        return ToolResult(data=f"{note}\n\n{format_catalog(snap)}")


def build(config, ctx: dict) -> SkillCatalogHandler:
    """自动发现工厂。ctx['skill_catalog'] 由 main 注入。"""
    store = ctx.get("skill_catalog")
    if not isinstance(store, CatalogStore):
        raise RuntimeError("skill_catalog store 未注入，拒绝以空目录上线")
    return SkillCatalogHandler(config, store)
