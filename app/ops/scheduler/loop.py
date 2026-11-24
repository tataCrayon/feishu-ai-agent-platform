"""调度循环 — 周期扫描 approved 档案，到点触发执行。

约束（需求红线）：扫描间隔不得低于 10 分钟。配置项
[projects.scheduled_tasks].scan_interval_sec 允许调大（降低打扰），
调低于 600 会被强制拉回 600 并打 warning。

每次扫描：
1. pending/ 残留收编（审批门下线前的档案，见 `_adopt_legacy_pending`）
2. approved/ 全量拾取（重启后自动恢复排期——档案即状态，无内存态）
3. 过期任务（expire_date < 今天）→ done/ 归档
4. next_run_ts 已到 → 排期下轮 + 后台执行（不阻塞扫描循环）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.ops.scheduler.executor import ScheduledTaskExecutor
from app.ops.scheduler.store import (
    STATUS_APPROVED,
    STATUS_PENDING,
    ScheduledTask,
    ScheduledTaskStore,
)

logger = logging.getLogger("scheduler.loop")

TZ = timezone(timedelta(hours=8))

# 扫描间隔下限（秒）——需求红线 10 分钟
MIN_SCAN_INTERVAL_SEC = 600


def compute_next_run(task: ScheduledTask, now: datetime) -> float:
    """计算任务下轮应触发时间（epoch 秒）。

    every 型：上次排期 + interval_minutes，**落后于现在时改成 now + interval**。
    daily 型：今天/明天的 HH:MM（已过今天时点则排明天）。

    为什么要「落后就重排」：按 `上次 + interval` 一路累加，进程停机两小时后恢复，
    一条 30 分钟的任务会在接下来每轮扫描都触发一次、直到把欠的 4 次补齐——群里
    看到的是同一份摘要连刷好几遍，而用户要的是「现在这一轮」。原生 Schedule 的
    语义也是「重复任务只补发最近一次错过的时点」，这里对齐它：错过的不补，
    从当前时刻重新起算。
    """
    if task.schedule_kind == "daily":
        hh, mm = (task.daily_time or "09:00").split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    interval = max(task.interval_minutes, 1) * 60
    if task.next_run_ts > 0:
        next_ts = task.next_run_ts + interval
        # 排期已经落在过去 = 中间有扫描没跟上的时段（停机/休眠/上次执行超时）
        return next_ts if next_ts > now.timestamp() else now.timestamp() + interval
    return now.timestamp() + interval


class ScheduledTaskLoop:
    """定时任务调度循环（asyncio 后台任务，随 bot 启停）。"""

    def __init__(
        self,
        store: ScheduledTaskStore,
        executor: ScheduledTaskExecutor,
        scan_interval_sec: int = 600,
    ):
        if scan_interval_sec < MIN_SCAN_INTERVAL_SEC:
            logger.warning(
                "[scheduler] scan_interval_sec=%d < %d（需求下限），强制拉回",
                scan_interval_sec, MIN_SCAN_INTERVAL_SEC,
            )
            scan_interval_sec = MIN_SCAN_INTERVAL_SEC
        self._store = store
        self._executor = executor
        self._interval = scan_interval_sec
        self._running = False
        self._task: asyncio.Task | None = None
        # 在跑的任务 id：执行时长可能超过一个扫描周期（实测 25 分钟 > 10 分钟间隔），
        # 没有这层去重就会对同一条任务并发起两次执行——两次抢同一个 dsh 会话，
        # 产出顺序与语义都不对（第二句还排在别人后面）。
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[scheduler] loop started (scan_interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[scheduler] loop stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._scan_once()
            except Exception as e:
                logger.exception("[scheduler] scan failed: %s", e)
            await asyncio.sleep(self._interval)

    async def _scan_once(self) -> None:
        """单次扫描：残留 pending 收编 + 过期归档 + 到点触发。"""
        now = datetime.now(TZ)
        today = now.date().isoformat()
        self._adopt_legacy_pending()
        tasks = self._store.list_by_status(STATUS_APPROVED)
        if not tasks:
            return

        fired = 0
        for task in tasks:
            # 过期归档
            if task.expire_date and task.expire_date < today:
                self._store.archive(task, f"已过到期日 {task.expire_date}，自动归档")
                logger.info("[scheduler] task %s expired -> done", task.task_id)
                continue

            # 首次排期（approved 后 next_run_ts 尚未设置）
            if task.next_run_ts <= 0:
                task.next_run_ts = compute_next_run(task, now)
                self._store.update(task)
                continue

            # 到点触发
            if time.time() >= task.next_run_ts:
                # 上一轮还没跑完：保持 next_run_ts 不动，下轮再判。
                # 先推进再执行会让「还在跑」与「已经排下一轮」同时成立，
                # 于是同一个任务叠两次执行。
                if task.task_id in self._in_flight:
                    logger.info(
                        "[scheduler] task %s 上一轮仍在执行，跳过本轮触发", task.task_id
                    )
                    continue
                task.next_run_ts = compute_next_run(task, now)
                self._store.update(task)
                fired += 1
                self._in_flight.add(task.task_id)
                running = self._executor.execute_in_background(task)
                if running is not None:
                    running.add_done_callback(self._release(task.task_id))
                else:  # 替身执行器（单测）不返回 Task：不占用「在跑」标记
                    self._in_flight.discard(task.task_id)

        if fired:
            logger.info("[scheduler] fired %d task(s)", fired)

    def _release(self, task_id: str) -> Callable[[Any], None]:
        """执行完成的回调：把任务从「在跑」集合里摘掉（否则再也不会被触发）。"""
        def _done(_task: Any) -> None:
            self._in_flight.discard(task_id)

        return _done

    def _adopt_legacy_pending(self) -> None:
        """把审批门下线前留下的 pending 档案收编进调度。

        那些任务的评估早已通过，只差一个「点通过」的人；审批卡已不存在，
        留着它们等于用户眼里的任务凭空消失（既跑不了，也没人点得动）。
        """
        for task in self._store.list_by_status(STATUS_PENDING):
            self._store.update(task, STATUS_APPROVED)
            logger.info("[scheduler] legacy pending adopted: %s", task.task_id)


__all__ = ["MIN_SCAN_INTERVAL_SEC", "ScheduledTaskLoop", "compute_next_run"]
