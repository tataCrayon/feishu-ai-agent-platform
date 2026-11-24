"""schedule-agent handler — 定时任务创建入口 + 生命周期操作 + 查询。

用户用自然语言描述定时任务 → LLM（Function Calling）提取结构化参数 →
调用 create_scheduled_task 工具转交 ScheduledTaskService：
    评估（超出能力面驳回，回执原因）→ 建档 approved/ → 调度循环下个扫描周期拾取
    → 到点把指令交给现有 Agent 执行并投递目标会话。**没有审批门**：创建即生效。

生命周期操作（pause/resume/cancel）：创建人本人可操作自己的任务，
操作他人的任务须 L2（get_user_permission）。

查询类工具（list_tasks/get_task）读 logs/scheduled_tasks/ 档案目录与
logs/scheduler_runs/ 执行历史，无 DB 依赖。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.handlers import AgentHandler, ToolResult
from app.ops.scheduler.scheduler import ScheduledTaskService
from app.ops.scheduler.store import (
    MIN_INTERVAL_MINUTES,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PAUSED,
    STATUS_PENDING,
    STATUS_REJECTED,
)

logger = logging.getLogger("agents.handlers.schedule")

# 本地状态目录名 → 用户可读标签
_STATUS_LABELS = {
    STATUS_PENDING: "待审批（历史档案）",
    STATUS_APPROVED: "调度中",
    STATUS_PAUSED: "已暂停",
    STATUS_REJECTED: "已驳回",
    STATUS_DONE: "已过期归档",
    STATUS_CANCELLED: "已取消",
}


# ── 工具定义 ─────────────────────────────────────────────────────

SCHEDULE_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_scheduled_task",
            "description": "创建定时任务（评估通过即生效，无需等待审批）。"
                "定时任务能力边界：只支持『按周期执行一条指令，把回复发到指定群/用户』；"
                "扫描间隔最低 10 分钟。到点执行走与群里手动提问同一条链路；"
                "资讯类指令执行方填 aihot，提醒/固定文案不填（由模型自答）。"
                "超出边界的请求会评估驳回",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "到点要执行的具体指令原文（给 Agent 的完整任务描述）",
                    },
                    "target": {
                        "type": "string",
                        "description": "投递目标：群会话 ID（oc_ 开头）或用户 open_id（ou_ 开头）。"
                            "用户没给 ID 而是说'发到本群'时传当前会话 chat_id",
                    },
                    "target_name": {
                        "type": "string",
                        "description": "目标展示名（群名/人名），审批卡片展示用",
                    },
                    "schedule_kind": {
                        "type": "string",
                        "enum": ["every", "daily"],
                        "description": "调度类型：every=每 N 分钟；daily=每天固定时刻",
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "every 型：间隔分钟数，最低 10",
                    },
                    "daily_time": {
                        "type": "string",
                        "description": "daily 型：每天触发时刻 HH:MM（24 小时制）",
                    },
                    "expire_date": {
                        "type": "string",
                        "description": "到期日 YYYY-MM-DD，可空（长期任务）",
                    },
                },
                "required": ["instruction", "target", "schedule_kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "列出定时任务档案（待审批/调度中/已暂停/已驳回/已过期/已取消），含执行统计",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "paused", "rejected", "done", "cancelled", "all"],
                        "description": "按状态过滤，默认 all",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": "查询单条定时任务详情：调度参数、执行统计、最近 5 次执行记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "任务 ID（task- 开头，list_tasks 可查到）",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_task",
            "description": "暂停一条调度中的定时任务（暂停后不再到点执行，可恢复）。"
                "创建人本人可操作，操作他人的任务须 L2 管理员",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（task- 开头）"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_task",
            "description": "恢复一条已暂停的定时任务（下个调度周期自动重新排期）。"
                "创建人本人可操作，操作他人的任务须 L2 管理员",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（task- 开头）"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "取消一条定时任务（终态，不可恢复；暂停中的也可取消）。"
                "创建人本人可操作，操作他人的任务须 L2 管理员",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID（task- 开头）"},
                },
                "required": ["task_id"],
            },
        },
    },
]


class ScheduleAgentHandler(AgentHandler):
    """schedule-agent 处理器 — 定时任务创建/查询。"""

    agent_name = "schedule-agent"
    tools = SCHEDULE_AGENT_TOOLS

    def __init__(self, config, service: ScheduledTaskService | None = None):
        super().__init__(config)
        self._service = service

    def wire_service(self, service: ScheduledTaskService) -> None:
        """注入调度服务总装（main.py 启动期接线，绕开 discovery 工厂无参约束）。"""
        self._service = service

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        """执行工具调用。创建动作经 caller context 拿消息对象转交服务。"""
        if tool_name == "create_scheduled_task":
            return await self._create_task(args)
        if tool_name == "list_tasks":
            return self._list_tasks(args)
        if tool_name == "get_task":
            return self._get_task(args)
        if tool_name == "pause_task":
            return await self._transition(args, STATUS_PAUSED)
        if tool_name == "resume_task":
            return await self._transition(args, STATUS_APPROVED)
        if tool_name == "cancel_task":
            return await self._transition(args, STATUS_CANCELLED)
        return ToolResult(error=f"未知工具: {tool_name}")

    # ── 工具实现 ──

    async def _create_task(self, args: dict) -> ToolResult:
        from app.orchestrator.context import get_caller_context

        if self._service is None:
            return ToolResult(error="调度服务未挂载（[projects.scheduled_tasks].enabled=false）")
        kind = str(args.get("schedule_kind", "") or "")
        if kind == "every":
            try:
                interval = int(args.get("interval_minutes") or 0)
            except (TypeError, ValueError):
                interval = 0
            if interval < MIN_INTERVAL_MINUTES:
                return ToolResult(
                    error=f"扫描间隔不得低于 {MIN_INTERVAL_MINUTES} 分钟（当前 {interval}），"
                          f"请与用户确认调大间隔后再创建"
                )
        if kind not in ("every", "daily"):
            return ToolResult(error="schedule_kind 必须是 every 或 daily")

        ctx = get_caller_context()
        msg = ctx.get("message_obj")

        # 目标解析：把用户口语目标（本群/发给我/applink 链接/群名）归一成会话 ID。
        # 解析失败才要求用户补 ID——此前「没给 oc_/ou_ 就反问」是主要卡点。
        resolved = await self._resolve_target(str(args.get("target", "") or ""), ctx)
        if isinstance(resolved, str):
            return ToolResult(error=resolved)
        args = {**args, **resolved}

        if msg is None:
            # dsh 链路（MCP 子进程）没有消息对象：走不带 msg 的入口。
            chat_id = str(ctx.get("chat_id") or "")
            if not chat_id:
                return ToolResult(error="拿不到发起会话（非消息链路触发），"
                                        "请在飞书会话里重新发起")
            result = await self._service.submit(
                args, chat_id=chat_id,
                sender_id=str(ctx.get("sender_id") or ""),
                sender_name=str(ctx.get("sender_name") or ""),
            )
            if not result.get("ok"):
                return ToolResult(data={"reply": (
                    "🚫 定时任务创建被驳回（超出当前能力）：\n"
                    + str(result.get("reason") or "")
                    + "\n\n可调整后重新描述，例如「每 30 分钟总结一次日报发到本群」。")})
            return ToolResult(data={"reply": (
                f"✅ 定时任务已创建：{result.get('task_id')}（{result.get('schedule')}），"
                "已纳入调度，下个扫描周期内开始执行。")})

        await self._service.handle_request(msg, args)
        # 回执由服务直接发卡片/驳回文案给用户，这里给 LLM 一句固定语义
        return ToolResult(data="创建请求已受理：评估通过即纳入调度（回执卡片已发到本会话），"
                               "驳回则回执原因。无需等待任何审批。")

    # ── 目标解析 ──

    async def _resolve_target(self, raw: str, ctx: dict) -> dict | str:
        """把 target 归一为 {target, target_name}；无法解析返回错误文案。

        支持的口语/形态（按序匹配）：
        - oc_/ou_ 开头 ID：直接透传
        - applink 群链接（link_token）：飞书无服务端换取 API，告知用户改用群名
        - 「本群/这个群/当前群」：来源会话是群聊时取 chat_id
        - 「发给我/我/私信我」：sender open_id
        - 其他非空文本：按群名在 bot 所在群列表里精确/包含匹配
        """
        raw = (raw or "").strip()
        if raw.startswith(("oc_", "ou_")):
            return {}

        # applink 链接：link_token 没有服务端换 chat_id 的 API（applink 是客户端协议）
        if "applink.feishu.cn" in raw:
            return (
                "群链接（applink）无法在服务端解析成会话 ID——这是飞书客户端协议，"
                "没有开放的换取接口。请改用群名（如「发到 测试交流群」），"
                "或直接告诉我 oc_ 开头的群会话 ID。"
            )

        chat_id = ctx.get("chat_id", "") or ""
        sender_id = ctx.get("sender_id", "") or ""
        lowered = raw.lower()

        if raw in ("本群", "这个群", "当前群", "当前会话", "此群") or lowered in (
            "this chat", "here",
        ):
            if chat_id.startswith("oc_"):
                return {"target": chat_id, "target_name": "本群"}
            return "当前会话不是群聊，请说「发给我」或提供目标群/用户的 ID。"

        if raw in ("发给我", "我", "私信我", "发我", "通知我") or lowered in ("me", "dm me"):
            if sender_id.startswith("ou_"):
                return {"target": sender_id, "target_name": ctx.get("sender_name", "") or "我"}
            return "拿不到你的用户 ID，请重新在飞书会话里发起。"

        if raw:
            # 群名/人名 → 在 bot 所在群列表匹配（精确优先，其次包含）
            match = await self._find_chat_by_name(raw)
            if match is not None:
                cid, cname = match
                return {"target": cid, "target_name": cname}
            return (
                f"没有找到名为「{raw[:30]}」的群（我在的群里没有精确或包含匹配）。"
                "可以换一个更完整的群名，或提供 oc_ 开头的群会话 ID。"
            )

        # LLM 没传 target：按「发给我」处理（私聊场景自然成立）
        if sender_id.startswith("ou_"):
            return {"target": sender_id, "target_name": ctx.get("sender_name", "") or "我"}
        return "请告诉我要发到哪里：本群、发给我，或具体群名/会话 ID。"

    async def _find_chat_by_name(self, name: str) -> tuple[str, str] | None:
        """在 bot 所在群列表里按名称找群（精确 > 包含）。失败返回 None。"""
        if self._service is None:
            return None
        # dsh 的 MCP 子进程里没有 bot 的 channel 对象，但「我在哪些群」这个事实
        # 用 tenant token 就能查（`im/v1/chats`）。不复用 channel 是因为那边是
        # SDK 客户端、依赖长连接；这里是纯 HTTP，两种链路都能跑。
        try:
            from app.protocols.feishu_send import list_chats

            chats = list_chats()
            if chats:
                return _match_chat(name, chats)
        except Exception as e:
            logger.debug("[schedule] list_chats via HTTP failed: %s", e)
        try:
            ch = self._service._channel
            if ch is None:
                return None
            from lark_channel.api.im.v1.model.list_chat_request import ListChatRequest

            sdk_chats: list[tuple[str, str]] = []
            page_token = ""
            for _ in range(5):  # 最多翻 5 页，防失控
                builder = ListChatRequest.builder().page_size(100)
                if page_token:
                    builder = builder.page_token(page_token)
                resp = await ch._channel._client.im.v1.chat.alist(builder.build())
                data = getattr(resp, "data", None)
                items = getattr(data, "items", None) or []
                for item in items:
                    cid = getattr(item, "chat_id", "") or ""
                    cname = getattr(item, "name", "") or ""
                    if cid:
                        sdk_chats.append((cid, cname))
                page_token = getattr(data, "page_token", "") or ""
                if not page_token or not items:
                    break
            return _match_chat(name, sdk_chats)
        except Exception as e:
            logger.warning("[schedule] find_chat_by_name failed: %s", e)
        return None

    def _list_tasks(self, args: dict) -> ToolResult:
        if self._service is None:
            return ToolResult(error="调度服务未挂载")
        from app.ops.scheduler.store import ScheduledTaskStore

        status = str(args.get("status", "all") or "all")
        store: ScheduledTaskStore = self._service._store
        if status in _STATUS_LABELS:
            tasks = store.list_by_status(status)
        else:
            tasks = store.list_by_status(*_STATUS_LABELS)

        if not tasks:
            return ToolResult(data="当前没有定时任务档案")

        lines = []
        for t in tasks[:20]:
            label = _STATUS_LABELS.get(t.status, t.status)
            run_info = ""
            if t.run_count:
                run_info = f"，已执行 {t.run_count} 次（{'最近成功' if t.last_run_ok else '最近失败'}）"
            lines.append(
                f"- [{label}] {t.task_id} · {t.describe()} · {t.agent}{run_info}"
            )
        return ToolResult(data="\n".join(lines))

    def _get_task(self, args: dict) -> ToolResult:
        """单任务详情：调度参数 + 执行统计 + 最近 5 次执行记录。"""
        if self._service is None:
            return ToolResult(error="调度服务未挂载")
        from app.ops.scheduler.runs import run_recorder

        task_id = str(args.get("task_id", "") or "")
        task = self._service._store.get(task_id)
        if task is None:
            return ToolResult(error=f"未找到任务 {task_id}")

        label = _STATUS_LABELS.get(task.status, task.status)
        lines = [
            f"任务 {task.task_id} [{label}]",
            f"- 指令：{task.instruction[:120]}",
            f"- 调度：{task.describe()}（{task.agent}）",
            f"- 创建人：{task.creator_name or task.creator_id}",
        ]
        if task.expire_date:
            lines.append(f"- 到期日：{task.expire_date}")
        if task.run_count:
            lines.append(
                f"- 已执行 {task.run_count} 次，最近 {task.last_run_at or '-'}"
                f"（{'成功' if task.last_run_ok else '失败'}）"
            )
            if task.last_error:
                lines.append(f"- 最近错误：{task.last_error}")
        runs = run_recorder.read_recent(days=30, task_id=task_id)[:5]
        if runs:
            lines.append("最近执行：")
            for r in runs:
                mark = "✅" if r.get("outcome") == "ok" else "❌"
                lines.append(
                    f"  {mark} {r.get('started_at', '-')} "
                    f"({r.get('duration_ms', 0)}ms)"
                    + (f" {r.get('error', '')[:60]}" if r.get("error") else "")
                )
        return ToolResult(data="\n".join(lines))

    async def _transition(self, args: dict, target_status: str) -> ToolResult:
        """生命周期操作：pause/resume/cancel。创建人本人可操作，他人须 L2。"""
        if self._service is None:
            return ToolResult(error="调度服务未挂载")
        from app.orchestrator import get_user_permission
        from app.orchestrator.context import get_caller_context

        task_id = str(args.get("task_id", "") or "")
        store = self._service._store
        task = store.get(task_id)
        if task is None:
            return ToolResult(error=f"未找到任务 {task_id}")

        # 权限：创建人本人，或 L2 管理员
        ctx = get_caller_context()
        operator_id = ctx.get("sender_id", "")
        is_creator = operator_id and operator_id == task.creator_id
        perm = get_user_permission(operator_id, ctx.get("chat_id", ""))
        if not is_creator and perm.level < 2:
            return ToolResult(
                error="只有任务创建人本人或 L2 管理员可以执行该操作"
            )

        # 状态机校验
        act = {STATUS_PAUSED: "暂停", STATUS_APPROVED: "恢复", STATUS_CANCELLED: "取消"}[target_status]
        if target_status == STATUS_PAUSED:
            if task.status != STATUS_APPROVED:
                return ToolResult(error=f"任务当前状态为 {task.status}，只有调度中的任务可以暂停")
        elif target_status == STATUS_APPROVED:
            if task.status != STATUS_PAUSED:
                return ToolResult(error=f"任务当前状态为 {task.status}，只有已暂停的任务可以恢复")
        else:  # cancel
            if task.status not in (STATUS_APPROVED, STATUS_PAUSED):
                return ToolResult(error=f"任务当前状态为 {task.status}，只有调度中/已暂停的任务可以取消")

        if target_status == STATUS_CANCELLED:
            store.cancel(task, reason=f"由 {operator_id or '未知用户'} 手动取消")
        else:
            store.update(task, target_status)

        effect = {
            STATUS_PAUSED: "已暂停，不再到点执行（可随时恢复）",
            STATUS_APPROVED: "已恢复，下个调度周期自动重新排期",
            STATUS_CANCELLED: "已取消（终态，档案留档 cancelled/）",
        }[target_status]
        logger.info("[scheduler] task %s %s by %s", task_id, act, operator_id)
        return ToolResult(data=f"任务 {task_id} {act}完成：{effect}")


def _match_chat(name: str, chats: list[tuple[str, str]]) -> tuple[str, str] | None:
    """群名匹配：精确优先，其次包含（两轮扫，避免「测试」命中「测试2组」）。"""
    for cid, cname in chats:
        if cname == name:
            return cid, cname
    for cid, cname in chats:
        if name in cname:
            return cid, cname
    return None


def build(config, ctx: dict) -> ScheduleAgentHandler:
    """自动发现约定工厂：service 由 main.py 启动后 wire_service 注入。

    dsh 的 MCP 子进程没有 main 的 service 实例，这里按同一份配置就地建一个：
    创建要建档（评估通过即生效），没有 service 就等于「永远说不能建」。store 是
    文件系统，两个进程读写同一份档案；调度循环只在 main 里跑，子进程不发调度。
    """
    handler = ScheduleAgentHandler(config)
    if handler._service is None:
        try:
            from app.config import load_config
            from app.ops.scheduler.scheduler import ScheduledTaskService

            cfg = load_config()
            if cfg.scheduled_tasks.enabled:
                handler.wire_service(ScheduledTaskService(
                    scan_interval_sec=cfg.scheduled_tasks.scan_interval_sec,
                ))
        except Exception as e:
            logger.warning("[schedule] service 就地装配失败：%s", e)
    return handler
