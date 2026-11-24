"""调度服务总装 — 创建入口 + 评估 + 调度循环组装。

入口约定（对齐能力热插拔 CapabilityIngestService 的形态）：
- dsh 会话里的 `create_scheduled_task` 工具（MCP 子进程）或旧编排链路的
  schedule-agent Handler 调 handle_request()/submit()
- 内完成：建档 → 评估（确定性规则，超出能力面驳回）→ 直接纳入调度 → 发回执卡
- **没有审批门**：创建即生效，卡片只是回执。评估驳回仍然保留——那拦的是
  「我们做不到的事」，不是权限。
"""

from __future__ import annotations

import logging
from typing import Any

from app.ops.scheduler.cards import build_created_card, build_rejected_card
from app.ops.scheduler.evaluator import ScheduledTaskEvaluator
from app.ops.scheduler.executor import ScheduledTaskExecutor
from app.ops.scheduler.loop import ScheduledTaskLoop
from app.ops.scheduler.store import (
    STATUS_REJECTED,
    ScheduledTaskStore,
    normalize_target,
)

logger = logging.getLogger("scheduler.service")

# 可承接定时执行的 Agent 白名单（超出即驳回——能力面约束）。
#
# 为什么不是 dev-bot：消息链路已经迁到 dsh，定时任务的执行面也跟它同一条
# （`executor._run_instruction` 先走 DshGateway）。dsh 会话里根本没有 `dev-bot`
# 这个 Agent——那是 cc-connect 通道上的通用编码 Agent，且 `routable: false`，
# 技能目录里也不存在。把 dev-bot 写成默认值，等于每张审批卡都批给一个
# 「到点执行时不存在」的执行方：档案看着通过，真正跑起来拿到的是空回复。
#
# 默认值 = 可以直接在 dsh 会话里执行的能力：`aihot` 是技能目录里的资讯入口，
# 「每天总结资讯发到本群」这类最常见的定时需求直接成立。其余非 Agent 类指令
# （提醒、固定文案）走模型自答，同样不需要指定执行方。
DEFAULT_SCHEDULE_AGENT = "aihot"
SCHEDULABLE_AGENTS = [DEFAULT_SCHEDULE_AGENT]


class ScheduledTaskService:
    """定时任务创建/评估/调度的总装门面。"""

    def __init__(
        self,
        store: ScheduledTaskStore | None = None,
        scan_interval_sec: int = 600,
    ):
        self._store = store or ScheduledTaskStore()
        self._evaluator = ScheduledTaskEvaluator(allowed_agents=SCHEDULABLE_AGENTS)
        self._executor = ScheduledTaskExecutor(self._store)
        self._loop = ScheduledTaskLoop(
            self._store, self._executor, scan_interval_sec=scan_interval_sec
        )
        self._send_card: Any | None = None
        self._reply: Any | None = None
        self._channel: Any | None = None

    # ── main.py 接线 ──

    def wire(
        self,
        send_card_callback,
        reply_callback,
        update_card_callback=None,
        orchestrator=None,
        channel=None,
        dsh_gateway=None,
    ) -> None:
        """注入 channel 回调与执行链路（启动完成后调用）。

        dsh_gateway 与 orchestrator 二选一优先 dsh：定时任务的产出要与用户
        此刻在群里手动发同一句话一致，执行面必须跟消息链路同一个。

        `update_card_callback` 保留在签名里只为兼容既有接线（回执卡不再原地
        更新——没有按钮也就没有「点了之后刷成结果」这一步）。
        """
        self._send_card = send_card_callback
        self._reply = reply_callback
        self._executor.wire(orchestrator, channel, dsh_gateway)
        self._channel = channel

    def on_agents_reloaded(self, agents: list[str]) -> None:
        """能力热插拔 rebuild 后刷新可承接 Agent 名单。

        只做并集、不做替换：替换会把默认的 `aihot` 摘掉，下一张审批卡就又会
        批给一个名单外、到点不存在的执行方。
        """
        merged = [*SCHEDULABLE_AGENTS, *(a for a in agents if a not in SCHEDULABLE_AGENTS)]
        self._evaluator.reload_agents(merged)

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    # ── 创建入口 ──

    async def submit(
        self,
        params: dict[str, Any],
        *,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        """建档 + 评估 + 纳入调度 + 发回执卡，返回结构化结果（不依赖飞书消息对象）。

        为什么不复用 `handle_request(msg, ...)`：那条路要 `InboundMessage` 才能
        回复驳回收执。dsh 链路（MCP 子进程）里没有消息对象，但评估门照旧——
        超出能力面的请求照样驳回，只是不再需要「有人点通过」。

        Returns:
            {"ok": True, "task_id", "status", "schedule"} 或
            {"ok": False, "reason"}（评估驳回，档案落 rejected）。
        """
        target_type, target_id = normalize_target(str(params.get("target", "")))
        task = self._store.new_task(
            instruction=str(params.get("instruction", "")).strip(),
            agent=str(params.get("agent", "") or DEFAULT_SCHEDULE_AGENT),
            target_type=target_type,
            target_id=target_id,
            target_name=str(params.get("target_name", "") or ""),
            schedule_kind=str(params.get("schedule_kind", "") or "every"),
            interval_minutes=int(params.get("interval_minutes") or 0),
            daily_time=str(params.get("daily_time", "") or ""),
            expire_date=str(params.get("expire_date", "") or ""),
            creator_id=sender_id,
            creator_name=sender_name,
            source_chat_id=chat_id,
            source_message_id=source_message_id,
        )

        report = self._evaluator.evaluate(task)
        task.eval_ok = report.ok
        task.eval_reason = "；".join(report.blocks) if report.blocks else "评估通过"
        self._store.update(task)

        if not report.ok:
            self._store.update(task, STATUS_REJECTED)
            # 驳回也要发卡：调用方（MCP 子进程）里的模型手上没有评估细节，
            # 只靠 `reason` 复述容易漏掉「改到 10 分钟以上就行」这类可行动提示。
            if self._send_card is not None:
                try:
                    await self._send_card(chat_id, build_rejected_card(task, report))
                except Exception as e:
                    logger.warning("[scheduler] 驳回卡发送失败 %s: %s",
                                   task.task_id, str(e)[:140])
            return {"ok": False, "task_id": task.task_id,
                    "reason": "；".join(report.block_lines())}

        card = build_created_card(task, report)
        message_id = ""
        if self._send_card is not None:
            # 卡发不出去不影响「任务已建」：回执里带 message_id 只是为了让后续
            # 点按钮能原地更新，缺了它任务照样按盘上档案跑。
            try:
                message_id = str(await self._send_card(chat_id, card) or "")
            except Exception as e:
                logger.warning("[scheduler] 创建卡发送失败 %s: %s",
                               task.task_id, str(e)[:140])
        logger.info(
            "[scheduler] task created: %s (%s) by %s — 已纳入调度（无审批门）",
            task.task_id, task.describe(), sender_id,
        )
        return {"ok": True, "task_id": task.task_id, "status": task.status,
                "card_message_id": message_id, "schedule": task.describe()}

    async def handle_request(self, msg, params: dict[str, Any]) -> None:
        """处理一条创建请求（schedule-agent Handler 评估通过后转交）。

        params 结构（schedule-agent 的 create_scheduled_task 工具产出）：
            instruction / agent / target / schedule_kind /
            interval_minutes / daily_time / expire_date / target_name
        """
        chat_id = msg.conversation.chat_id
        sender_id = msg.sender.open_id
        sender_name = msg.sender.display_name or ""
        if not sender_name:
            # P2P 消息不带 display_name；回退 caller context（main.py 已回填），
            # 再不行由 _reply 回执链路的名单解析兜底（空名卡片显示 open_id）
            from app.orchestrator.context import get_caller_context

            sender_name = get_caller_context().get("sender_name", "")

        target_type, target_id = normalize_target(str(params.get("target", "")))
        task = self._store.new_task(
            instruction=str(params.get("instruction", "")).strip(),
            agent=str(params.get("agent", "") or DEFAULT_SCHEDULE_AGENT),
            target_type=target_type,
            target_id=target_id,
            target_name=str(params.get("target_name", "") or ""),
            schedule_kind=str(params.get("schedule_kind", "") or "every"),
            interval_minutes=int(params.get("interval_minutes") or 0),
            daily_time=str(params.get("daily_time", "") or ""),
            expire_date=str(params.get("expire_date", "") or ""),
            creator_id=sender_id,
            creator_name=sender_name,
            source_chat_id=chat_id,
            source_message_id=getattr(msg, "id", "") or "",
        )

        report = self._evaluator.evaluate(task)
        task.eval_ok = report.ok
        task.eval_reason = "；".join(report.blocks) if report.blocks else "评估通过"
        self._store.update(task)

        if not report.ok:
            # 超出能力面：直接驳回（迁移 rejected/），回执评估结论
            self._store.update(task, STATUS_REJECTED)
            if self._send_card is not None:
                try:
                    await self._send_card(chat_id, build_rejected_card(task, report))
                except Exception as e:
                    logger.warning("[scheduler] 驳回卡发送失败 %s: %s",
                                   task.task_id, str(e)[:140])
            return

        card = build_created_card(task, report)
        if self._send_card is not None:
            try:
                await self._send_card(chat_id, card)
            except Exception as e:
                logger.warning("[scheduler] 创建卡发送失败 %s: %s",
                               task.task_id, str(e)[:140])
        logger.info(
            "[scheduler] task created: %s (%s) by %s",
            task.task_id, task.describe(), sender_id,
        )
