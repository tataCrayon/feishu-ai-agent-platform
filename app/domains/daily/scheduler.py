"""Daily Scheduler — periodic task that checks for due subscriptions
and sends daily report push messages.

Runs as an asyncio background task alongside the main bot loop.
Checks every 60 seconds if any subscriptions are due for delivery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.domains.daily.aihot import AihotError, get_daily_pair
from app.domains.daily.store import DailyStore
from app.domains.daily.subscription import SubscriptionManager

logger = logging.getLogger("daily.scheduler")

TZ = timezone(timedelta(hours=8))


class DailySource(Protocol):
    """日报来源协议：返回 (ISO日期, markdown, 推送文本) 或 None。"""

    async def get(self) -> tuple[str, str, str] | None: ...


class AIHOTDailySource:
    """AI HOT 单源 — get_daily_pair 已含本地缓存优先 + API 兜底。"""

    async def get(self) -> tuple[str, str, str] | None:
        try:
            return await get_daily_pair()
        except AihotError as e:
            logger.warning("AI HOT source failed: %s", e)
            return None


class DailyScheduler:
    """Periodic scheduler for daily report push deliveries.

    Usage:
        scheduler = DailyScheduler(source=..., store=..., subscription=..., converter=...)
        await scheduler.start(channel)  # starts background task
        ...
        await scheduler.stop()  # stops background task
    """

    def __init__(
        self,
        source: DailySource,
        store: DailyStore,
        subscription: SubscriptionManager,
        converter: Any,
        check_interval_sec: int = 60,
    ):
        self._source = source
        self._store = store
        self._subscription = subscription
        self._converter = converter
        self._check_interval = check_interval_sec
        self._running = False
        self._task: asyncio.Task | None = None
        self._channel = None

    async def start(self, channel):
        """Start the background scheduler task.

        Args:
            channel: BotChannel instance for sending messages.
        """
        self._channel = channel
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"Daily scheduler started (interval={self._check_interval}s)"
        )

    async def stop(self):
        """Stop the background scheduler task."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Daily scheduler stopped")

    async def _loop(self):
        """Main scheduler loop — checks for due subscriptions periodically."""
        while self._running:
            try:
                await self._check_and_deliver()
            except Exception as e:
                logger.exception(f"Daily scheduler check failed: {e}")
            await asyncio.sleep(self._check_interval)

    async def _check_and_deliver(self):
        """Check for due subscriptions and deliver if any."""
        due = self._subscription.get_due()
        if not due:
            return

        logger.info(f"Found {len(due)} due subscription(s) for delivery")

        # Get or generate the push text (shared across all subscribers)
        push_text = await self._get_push_text()
        if not push_text:
            logger.warning("No daily report available for push")
            return

        # Deliver to each subscriber
        for sub in due:
            await self._deliver(sub, push_text)

    async def _get_push_text(self) -> str | None:
        """Get the push-ready text for today.

        Checks cache first, then fetches from the configured source
        (AI HOT 单源，内部已含本地缓存优先与 API 兜底).

        Returns:
            Push-ready text, or None if no report available.
        """

        today = datetime.now(TZ).date()

        # Check cache first
        cached = self._store.get_cached(today)
        if cached:
            return cached

        # Fetch from source (AI HOT；本地缓存未命中时走 API)
        pair = await self._source.get()
        if not pair:
            logger.info("No daily report available from source")
            return None
        date_iso, _markdown, push_text = pair
        try:
            report_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            report_date = today
        self._store.cache(report_date, push_text)
        return push_text

    async def _deliver(self, sub: dict, push_text: str):
        """Deliver push text to a single subscriber.

        Args:
            sub: Subscription dict.
            push_text: The message text to send.
        """
        target_id = sub["target_id"]
        sub_id = sub["id"]
        sub_type = sub["type"]

        try:
            response = await self._channel.send(target_id, push_text)
            success = response is not None
            self._subscription.mark_delivery(sub_id, target_id, success)
            if success:
                logger.info(
                    f"Delivered daily report to {sub_type}={target_id}"
                )
            else:
                logger.warning(
                    f"Failed to deliver to {sub_type}={target_id}: no response"
                )
        except Exception as e:
            self._subscription.mark_delivery(sub_id, target_id, success=False)
            logger.error(
                f"Failed to deliver to {sub_type}={target_id}: {e}"
            )

    # ── Manual trigger ──

    async def send_now(self, channel, target_id: str) -> str | None:
        """Send today's daily report immediately.

        Used for the /daily now command.

        Args:
            channel: BotChannel instance.
            target_id: Target to send to (chat_id or open_id).

        Returns:
            The push text if sent, None if no report available.
        """
        push_text = await self._get_push_text()
        if not push_text:
            return None

        try:
            await channel.send(target_id, push_text)
            logger.info(f"Manual delivery to {target_id}")
            return push_text
        except Exception as e:
            logger.error(f"Manual delivery failed: {e}")
            return None

    async def get_today_text(self) -> str | None:
        """Get today's push text without sending.

        Used for the /daily command (response in chat).
        """
        return await self._get_push_text()