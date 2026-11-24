"""执行器 — 定时任务触发时把指令交给现有能力执行，结果发到目标会话。

「利用现有能力执行」的实现：不新建执行通道，指令走**与用户消息同一条链路**
（dsh 会话桥优先；dsh 未启用时回落到旧编排器），产出文本投递到目标群/用户。

为什么优先 dsh：定时任务的产出必须与用户此刻手动发同一句话的结果一致。
dsh 启用后旧编排器已不参与消息路由，若这里仍走它，同样的指令会得到与群里
不同的答复（工具表、技能目录、模型全不一样）。

编排器/gateway 与 channel 由 main.py 启动后注入（wire），wire 前触发只记
错误不丢档案（下个周期重试）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.ops.scheduler.runs import TaskRunRecord, run_recorder
from app.ops.scheduler.store import ScheduledTask, ScheduledTaskStore

logger = logging.getLogger("scheduler.executor")

TZ = timezone(timedelta(hours=8))


class ScheduledTaskExecutor:
    """执行单条到期的定时任务：产出 → 投递目标 → 回写执行统计。"""

    def __init__(self, store: ScheduledTaskStore):
        self._store = store
        self._orchestrator: Any | None = None
        self._dsh_gateway: Any | None = None
        self._channel: Any | None = None

    def wire(self, orchestrator, channel, dsh_gateway=None) -> None:
        """注入执行链路与 channel（main.py 启动完成后调用）。"""
        self._orchestrator = orchestrator
        self._dsh_gateway = dsh_gateway
        self._channel = channel

    async def _run_instruction(self, task: ScheduledTask, trace_id: str) -> str:
        """把指令交给在用的执行链路，返回答复文本。

        dsh 会话以 target_id 为 chat 键：定时任务在目标会话里就是「这个群里的
        一次提问」，上下文与群友手动提问共用同一条会话史。
        """
        if self._dsh_gateway is not None:
            return await self._dsh_gateway.handle(
                task.instruction,
                task.target_id,
                caller={"sender_id": "", "sender_name": f"定时任务 {task.task_id}"},
            )
        if self._orchestrator is not None:
            return await self._orchestrator.execute(
                message=task.instruction,
                chat_id=task.target_id,
                sender_id="",
                sender_name=f"定时任务 {task.task_id}",
                session_key=f"scheduler:{task.task_id}",
                trace_id=trace_id,
            )
        return ""

    async def execute(self, task: ScheduledTask) -> bool:
        """执行一条任务；返回是否成功。执行统计回写 manifest。"""
        started = time.time()
        ok = False
        err = ""
        reply_text = ""
        trace_id = f"sched-{task.task_id}-{int(time.time())}"

        if self._channel is None or (
            self._dsh_gateway is None and self._orchestrator is None
        ):
            err = "执行链路/channel 未就绪（启动中或已停机）"
        else:
            try:
                reply_text = await self._run_instruction(task, trace_id)
                if reply_text:
                    result = await self._channel.send(task.target_id, reply_text)
                    # 飞书拒收（机器人被移出群/无权限）走的是 `success=False`，
                    # 不抛异常——只看「没抛」会把没送到的任务记成成功，
                    # 运行历史全绿而群里一个字都没有。
                    sent_ok = getattr(result, "success", True)
                    if sent_ok:
                        ok = True
                    else:
                        err = _send_error_text(result)
                else:
                    err = "执行链路未返回内容"
            except Exception as e:
                err = str(e)[:200]
                logger.warning("[scheduler] task %s execute failed: %s", task.task_id, e)

        # 回写执行统计（无论成败，档案是唯一事实源）
        task.run_count += 1
        task.last_run_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        task.last_run_ok = ok
        task.last_error = err
        self._store.update(task)

        duration_ms = (time.time() - started) * 1000

        # 逐次执行历史落盘（监控/详情查询数据源，写入自吞异常不阻断）
        run_recorder.write(TaskRunRecord(
            task_id=task.task_id,
            instruction=task.instruction,
            agent=task.agent,
            target_id=task.target_id,
            started_at=task.last_run_at or datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            duration_ms=int(duration_ms),
            outcome="ok" if ok else "fail",
            reply_preview=reply_text,
            error=err,
            trace_id=trace_id,
        ))

        logger.info(
            "[scheduler] task %s %s (%.0fms) target=%s",
            task.task_id, "ok" if ok else f"failed: {err[:80]}", duration_ms,
            task.target_id,
        )
        return ok

    def execute_in_background(self, task: ScheduledTask) -> asyncio.Task:
        """fire-and-forget 执行（调度循环不阻塞在慢任务上）。

        返回 Task 让调用方挂完成回调：调度器靠它把任务从「在跑」集合里摘掉，
        否则一条任务跑完后再也不会被触发。
        """
        return asyncio.get_running_loop().create_task(self.execute(task))


__all__ = ["ScheduledTaskExecutor"]


def _send_error_text(result: Any) -> str:
    """把 `SendResult.failure` 翻成一句人话（写进 manifest 的 last_error）。

    只取错误码与提示里的关键片段：完整报文进日志，档案里放一行够定位即可。
    """
    error = getattr(result, "error", None)
    if error is None:
        return "投递失败（飞书返回 success=False，无错误详情）"
    code = getattr(error, "code", "") or ""
    hint = getattr(error, "hint", "") or getattr(error, "msg", "") or ""
    text = f"{code}: {hint}" if code else str(hint)
    return (text.strip() or "投递失败（无错误详情）")[:200]
