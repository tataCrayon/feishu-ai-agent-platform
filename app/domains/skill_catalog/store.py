"""快照读写。查询只读 JSON，刷新失败不覆盖上一份成功快照。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domains.skill_catalog.model import CatalogSnapshot
from app.domains.skill_catalog.scan import CatalogScanner

logger = logging.getLogger("skill_catalog.store")

TZ = timezone(timedelta(hours=8))


class CatalogStore:
    """本地快照 + 刷新策略。"""

    def __init__(
        self,
        scanner: CatalogScanner,
        snapshot_path: str,
        refresh_interval_hours: int = 24,
    ):
        self._scanner = scanner
        self._path = Path(snapshot_path)
        self._interval = timedelta(hours=max(refresh_interval_hours, 1))
        self._refreshing = False

    def load(self) -> CatalogSnapshot | None:
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            snap = CatalogSnapshot.from_dict(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[skill_catalog] snapshot unreadable: %s", e)
            return None
        if snap.error or not snap.entries:
            return None
        return snap

    def is_stale(self, snap: CatalogSnapshot | None) -> bool:
        if snap is None or not snap.fetched_at:
            return True
        try:
            fetched = datetime.fromisoformat(snap.fetched_at)
        except ValueError:
            return True
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=TZ)
        return datetime.now(TZ) - fetched >= self._interval

    async def refresh(self, *, force: bool) -> tuple[CatalogSnapshot | None, str]:
        """刷新。返回 (可用快照, 给人看的状态句)。

        force=False 且未过期：不 fetch。
        fetch 失败：保留旧快照，状态句写明失败。
        """
        current = self.load()
        if not force and not self.is_stale(current):
            return current, "快照未过期，未访问仓库"
        if self._refreshing:
            return current, "已有一次刷新在进行，这次用现有快照"
        self._refreshing = True
        try:
            fresh = await self._scanner.scan(force_fetch=True)
        finally:
            self._refreshing = False
        if fresh.error or not fresh.entries:
            if current is not None:
                return current, f"刷新失败，仍用上次快照：{fresh.error or '没有扫到 Skill'}"
            return None, f"刷新失败，且没有可用快照：{fresh.error or '没有扫到 Skill'}"
        self._write(fresh)
        return fresh, "已刷新"

    def _write(self, snap: CatalogSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(snap.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
