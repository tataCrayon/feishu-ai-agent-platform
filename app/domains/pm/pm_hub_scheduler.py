"""PM Hub Scheduler - 【已废弃】旧定时调度器

2026-11-02 起不再被任何进程启动（app/main.py 里的启动点已移除）。

为什么留着而不删：
    读它的代码能看懂「双份推送」是怎么来的 —— 它和 pm_daemon 主循环各算各的
    09:00 / 17:30，两边都有自己的内存标记，谁也不知道对方已经发过。
    唯一出口现在是 pm_daemon，幂等由 .schedule_slots.json 保证。

别在这里加功能。要加定时，去 pm_daemon.run_daemon 的循环里加一个 slot。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    PMO_GROUP_ID,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm.pm_hub_sender import (
    build_morning_brief,
    build_evening_review_card,
    send_group_message,
)
from app.domains.pm.pm_confirm_card import send_interactive_card
from app.domains.pm.feishu_pm_sync import (
    generate_today_tasks,
    sync_tasks_to_bitable,
    fetch_existing_records,
)

logger = logging.getLogger("pm_hub.scheduler")
TZ = timezone(timedelta(hours=8))


class PMHubScheduler:
    def __init__(self, check_interval_sec: int = 30):
        self._check_interval = check_interval_sec
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_morning_date: Optional[str] = None
        self._last_evening_date: Optional[str] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("PMHubScheduler 已启动后台定时轮询")

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PMHubScheduler 已停止")

    async def _loop(self):
        while self._running:
            try:
                await self._check_and_trigger()
            except Exception as e:
                logger.exception(f"PMHubScheduler 轮询异常: {e}")
            await asyncio.sleep(self._check_interval)

    async def _check_and_trigger(self):
        now = datetime.now(TZ)
        today_str = now.strftime("%Y-%m-%d")

        # 周末不主动催办（0~4 为工作日）
        if now.weekday() >= 5:
            return

        # 1. 晨间 09:00 准时提醒李四列出今日计划 + 09:30 晨报与日计划同步
        if (now.hour == 9 and now.minute >= 0) or (now.hour > 9 and now.hour < 12):
            if self._last_morning_date != today_str:
                self._last_morning_date = today_str
                await self._run_morning_routine(now.date())

        # 2. 晚间复盘卡片 (17:30 之后均可触发)
        if (now.hour == 17 and now.minute >= 30) or (now.hour >= 18 and now.hour <= 22):
            if self._last_evening_date != today_str:
                self._last_evening_date = today_str
                await self._run_evening_routine(now.date())

    async def _run_morning_routine(self, today_dt: date):
        logger.info(f"触发 PM Hub 晨报与多维表格自动同步: {today_dt}")
        loop = asyncio.get_running_loop()

        def _sync():
            vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
            stories = parse_stories(vals)
            radar = evaluate_daily_radar(stories, today=today_dt)
            # 同步多维表格
            tasks = generate_today_tasks(radar, today_dt=today_dt)
            sync_res = sync_tasks_to_bitable(tasks)
            # 发送群晨报
            brief_md = build_morning_brief(radar)
            send_group_message(PMO_GROUP_ID, brief_md)
            return sync_res

        try:
            res = await loop.run_in_executor(None, _sync)
            logger.info(f"PM Hub 晨报与表格同步完成: {res}")
        except Exception as e:
            logger.exception(f"执行晨报失败: {e}")

    async def _run_evening_routine(self, today_dt: date):
        logger.info(f"触发 PM Hub 晚间复盘提醒与日清盘点: {today_dt}")
        loop = asyncio.get_running_loop()

        def _send():
            vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
            stories = parse_stories(vals)
            radar = evaluate_daily_radar(stories, today=today_dt)
            records = fetch_existing_records()
            card = build_evening_review_card(records, radar)
            send_interactive_card(PMO_GROUP_ID, card)

        try:
            await loop.run_in_executor(None, _send)
        except Exception as e:
            logger.exception(f"执行晚报提醒失败: {e}")