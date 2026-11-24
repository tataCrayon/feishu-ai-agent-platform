"""Daily Subscription Manager — manages daily report subscriptions.

Supports both personal (user) and group subscriptions. Subscriptions are
stored as a JSON file. Each subscription specifies a target (user open_id
or group chat_id), type (personal/group), and push time (HH:MM format).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("daily.subscription")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # -> feishu-agent-platform/
SUBSCRIPTION_FILE = PROJECT_DIR / "logs" / "daily" / "subscriptions.json"
AUDIT_FILE = PROJECT_DIR / "logs" / "daily" / "audit.jsonl"
DELIVERY_FILE = PROJECT_DIR / "logs" / "daily" / "delivery.jsonl"

TZ = timezone(timedelta(hours=8))


class SubscriptionManager:
    """Manages daily report subscriptions.

    Each subscription record:
    {
        "id": "sub_uuid",
        "type": "personal" | "group",
        "target_id": "ou_xxx" | "oc_xxx" (open_id or chat_id),
        "name": "display name" (optional),
        "time": "09:00",  # HH:MM format
        "timezone": "Asia/Shanghai",
        "enabled": true,
        "created_at": "ISO timestamp",
        "last_sent_date": "2026-08-14" (date of last push, for dedup)
    }
    """

    def __init__(self, sub_file: str | None = None):
        self._sub_file = Path(sub_file) if sub_file else SUBSCRIPTION_FILE
        self._sub_file.parent.mkdir(parents=True, exist_ok=True)
        self._subscriptions: list[dict[str, Any]] = []
        self._load()
        self._ensure_audit_dir()

    def _ensure_audit_dir(self):
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        DELIVERY_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _load(self):
        """Load subscriptions from JSON file."""
        if self._sub_file.exists():
            try:
                data = json.loads(self._sub_file.read_text(encoding="utf-8"))
                self._subscriptions = data.get("subscriptions", [])
                logger.info(f"Loaded {len(self._subscriptions)} subscriptions")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load subscriptions: {e}")
                self._subscriptions = []

    def _save(self):
        """Save subscriptions to JSON file."""
        try:
            data = {"subscriptions": self._subscriptions}
            self._sub_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"Failed to save subscriptions: {e}")

    def _audit(self, action: str, target_id: str, detail: str = ""):
        """Write an audit log entry."""
        try:
            entry = {
                "timestamp": datetime.now(TZ).isoformat(),
                "action": action,
                "target_id": target_id,
                "detail": detail,
            }
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _record_delivery(self, sub_id: str, target_id: str, date_str: str, success: bool):
        """Record a delivery attempt."""
        try:
            entry = {
                "timestamp": datetime.now(TZ).isoformat(),
                "sub_id": sub_id,
                "target_id": target_id,
                "date": date_str,
                "success": success,
            }
            with open(DELIVERY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── CRUD ──

    def subscribe(
        self,
        target_id: str,
        sub_type: str,
        time: str,
        name: str = "",
    ) -> bool:
        """Add a new subscription.

        Args:
            target_id: open_id (personal) or chat_id (group).
            sub_type: "personal" or "group".
            time: Push time in "HH:MM" format.
            name: Optional display name.

        Returns:
            True if subscribed, False if already subscribed.
        """
        # Check for duplicate: same target_id + time
        for sub in self._subscriptions:
            if sub["target_id"] == target_id and sub["time"] == time and sub["enabled"]:
                logger.info(f"Already subscribed: {target_id} at {time}")
                return False

        sub = {
            "id": f"sub_{uuid.uuid4().hex[:12]}",
            "type": sub_type,
            "target_id": target_id,
            "name": name,
            "time": time,
            "timezone": "Asia/Shanghai",
            "enabled": True,
            "created_at": datetime.now(TZ).isoformat(),
            "last_sent_date": "",
        }
        self._subscriptions.append(sub)
        self._save()
        self._audit("subscribe", target_id, f"time={time} type={sub_type}")
        logger.info(f"Subscribed: {target_id} at {time} ({sub_type})")
        return True

    def unsubscribe(self, target_id: str, time: str = "") -> bool:
        """Cancel a subscription.

        Args:
            target_id: The target to unsubscribe.
            time: Optional specific time to unsubscribe (if empty, cancels all).

        Returns:
            True if any subscription was cancelled.
        """
        cancelled = False
        for sub in self._subscriptions:
            if sub["target_id"] == target_id and sub["enabled"]:
                if time and sub["time"] != time:
                    continue
                sub["enabled"] = False
                cancelled = True
                self._audit("unsubscribe", target_id, f"time={sub['time']}")

        if cancelled:
            self._save()
            logger.info(f"Unsubscribed: {target_id}")
        return cancelled

    def list(self, target_id: str = "") -> list[dict[str, Any]]:
        """List subscriptions.

        Args:
            target_id: If provided, filter to this target.

        Returns:
            List of subscription dicts (only enabled ones).
        """
        if target_id:
            return [s for s in self._subscriptions if s["target_id"] == target_id and s["enabled"]]
        return [s for s in self._subscriptions if s["enabled"]]

    def list_all(self) -> list[dict[str, Any]]:
        """List all subscriptions (including disabled)."""
        return list(self._subscriptions)

    # ── Scheduling ──

    def get_due(self) -> list[dict[str, Any]]:
        """Get subscriptions that are due for delivery now.

        Returns subscriptions whose:
        - enabled == True
        - time matches the current minute (HH:MM)
        - last_sent_date is not today (no duplicate delivery)

        Returns:
            List of subscription dicts due for delivery.
        """
        now = datetime.now(TZ)
        current_time = now.strftime("%H:%M")
        today_str = now.date().isoformat()

        due = []
        for sub in self._subscriptions:
            if not sub["enabled"]:
                continue
            if sub["time"] != current_time:
                continue
            if sub.get("last_sent_date") == today_str:
                continue  # Already sent today
            due.append(sub)

        return due

    def mark_sent(self, sub_id: str):
        """Mark a subscription as having been sent today.

        Updates last_sent_date to today, preventing duplicate delivery.
        """
        for sub in self._subscriptions:
            if sub["id"] == sub_id:
                sub["last_sent_date"] = datetime.now(TZ).date().isoformat()
                self._save()
                break

    def mark_delivery(self, sub_id: str, target_id: str, success: bool):
        """Record a delivery attempt and mark as sent if successful."""
        today_str = datetime.now(TZ).date().isoformat()
        self._record_delivery(sub_id, target_id, today_str, success)
        if success:
            self.mark_sent(sub_id)

    def get_statistics(self) -> dict[str, int]:
        """Get subscription statistics."""
        enabled = [s for s in self._subscriptions if s["enabled"]]
        personal = [s for s in enabled if s["type"] == "personal"]
        groups = [s for s in enabled if s["type"] == "group"]
        return {
            "total": len(self._subscriptions),
            "enabled": len(enabled),
            "personal": len(personal),
            "groups": len(groups),
        }