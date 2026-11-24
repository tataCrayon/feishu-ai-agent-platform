"""Daily Store — caches converted daily report results.

Caches the push-ready text by date so that multiple subscribers on the
same day share a single conversion result. Uses JSON file storage.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger("daily.store")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # -> feishu-agent-platform/
DEFAULT_CACHE_DIR = PROJECT_DIR / "logs" / "daily"


class DailyStore:
    """Persistent cache for converted daily report content.

    Caches by date string (YYYY-MM-DD). Cache is stored as a JSON file
    at `{cache_dir}/cache.json`.

    The cache file is a dict:
        {"2026-08-14": {"push_text": "...", "cached_at": "..."}, ...}
    """

    def __init__(self, cache_dir: str | None = None):
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "cache.json"
        self._cache: dict[str, dict] = {}
        self._load()

    def _load(self):
        """Load cache from disk."""
        if self._cache_file.exists():
            try:
                self._cache = json.loads(self._cache_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load cache: {e}")
                self._cache = {}

    def _save(self):
        """Save cache to disk."""
        try:
            self._cache_file.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def get_cached(self, dt: date) -> str | None:
        """Get cached push text for a date.

        Args:
            dt: The date to look up.

        Returns:
            Cached push text, or None if not cached.
        """
        key = dt.isoformat()
        entry = self._cache.get(key)
        if entry:
            return entry.get("push_text")
        return None

    def get_cached_markdown(self, dt: date) -> str | None:
        """Get cached full markdown for a date (AI HOT 底层写入，可选字段)。"""
        key = dt.isoformat()
        entry = self._cache.get(key)
        if entry:
            return entry.get("markdown")
        return None

    def get_cached_report(self, dt: date) -> dict | None:
        """Get cached raw AI HOT report dict for a date (卡片渲染的事实源)。"""
        key = dt.isoformat()
        entry = self._cache.get(key)
        if entry:
            report = entry.get("report")
            return report if isinstance(report, dict) else None
        return None

    def cache_report(self, dt: date, report: dict) -> bool:
        """Persist the raw report dict so cards render without re-fetching.

        Kept in the same entry as push_text/markdown：一份日报的三个视图
        （推送文本 / 全文 markdown / 卡片）必须同生同灭，否则卡片会拿着
        前几天缓存的板块去渲染今天的日期。
        """
        key = dt.isoformat()
        entry = self._cache.get(key) or {}
        entry["report"] = report
        self._cache[key] = entry
        self._save()
        return True

    def cache(self, dt: date, push_text: str, markdown: str | None = None) -> bool:
        """Cache the push text for a date.

        Args:
            dt: The date to cache.
            push_text: The push-ready text to cache.
            markdown: Optional full markdown report (AI HOT 底层写入)。

        Returns:
            True if cached successfully.
        """
        from datetime import datetime, timedelta, timezone

        TZ = timezone(timedelta(hours=8))
        key = dt.isoformat()
        entry = self._cache.get(key) or {}
        self._cache[key] = {
            "push_text": push_text,
            "cached_at": datetime.now(TZ).isoformat(),
        }
        if markdown is not None:
            self._cache[key]["markdown"] = markdown
        elif entry.get("markdown"):
            # 覆盖写（如 scheduler 只带 push_text）时保留已有 markdown
            self._cache[key]["markdown"] = entry["markdown"]
        self._save()
        logger.info(f"Cached daily report for {key} ({len(push_text)} chars)")
        return True

    def has_cache(self, dt: date) -> bool:
        """Check if a date is cached."""
        key = dt.isoformat()
        return key in self._cache

    def all_entries(self) -> dict[str, dict]:
        """Return a copy of all raw cache entries keyed by ISO date."""
        return dict(self._cache)

    def clear_old(self, keep_days: int = 7):
        """Remove cache entries older than keep_days.

        Args:
            keep_days: Number of days to keep.
        """
        from datetime import date, datetime, timedelta, timezone

        cutoff = datetime.now(timezone(timedelta(hours=8))).date()
        from datetime import timedelta as td
        cutoff = cutoff - td(days=keep_days)

        keys_to_remove = []
        for key in self._cache:
            try:
                cached_date = date.fromisoformat(key)
                if cached_date < cutoff:
                    keys_to_remove.append(key)
            except ValueError:
                continue

        for key in keys_to_remove:
            del self._cache[key]

        if keys_to_remove:
            self._save()
            logger.info(f"Cleared {len(keys_to_remove)} old cache entries")