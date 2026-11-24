"""Data source registry — loads the daily-report data source list from a JSON config file.

The data source list (paths, file naming rules, cadence) is described in an external
JSON file (default: app/domains/daily/sources.json) so that content creators can add/remove
sources without touching code.

Each source carries a `match` rule (named-group regex + period template) that the
DailyReader uses to extract a period label from filenames.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("daily.source")

# Built-in fallback source — used when the sources file is missing/invalid.
DEFAULT_SOURCES_FILE = "app/domains/daily/sources.json"
FALLBACK_MAIN_SOURCE = {
    "id": "tech_daily",
    "name": "技术动态日报",
    "kind": "daily",
    "cadence": "daily",
    "path": "daily/技术动态日报",
    "description": "日更技术动态日报，含 GitHub 热门、大厂动态、AI 新闻等",
    "match": {
        "regex": r"^(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日_.*\.md$",
        "period_template": "{year}年{month}月{day}日",
    },
    "exclude": ["README.md"],
}


@dataclass
class DailySource:
    """A single daily-report data source (a directory of markdown files)."""

    id: str
    name: str
    path: Path
    kind: str  # daily | weekly | monthly
    cadence: str  # 更新节奏，用于 agent 提示
    description: str = ""
    match_regex: str = ""
    period_template: str = ""
    exclude: list[str] = field(default_factory=list)
    preview_chars: int = 2000
    period_from_heading: bool = False

    @property
    def is_daily(self) -> bool:
        return self.kind == "daily"

    @classmethod
    def from_dict(cls, raw: dict) -> DailySource:
        """Build a DailySource from a sources.json entry."""
        match = raw.get("match") or {}
        return cls(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            path=Path(raw["path"]),
            kind=raw.get("kind", "daily"),
            cadence=raw.get("cadence", raw.get("kind", "daily")),
            description=raw.get("description", ""),
            match_regex=match.get("regex", ""),
            period_template=match.get("period_template", ""),
            exclude=list(raw.get("exclude", [])),
            preview_chars=int(raw.get("preview_chars", 2000)),
            period_from_heading=bool(raw.get("period_from_heading", False)),
        )


class DailySourceRegistry:
    """Loads the data source list from a JSON config file.

    Falls back to a single built-in main source (tech_daily) if the file is
    missing or unparseable, so legacy configs keep working.
    """

    def __init__(self, sources_file: str | None = None):
        self._sources_file = Path(sources_file) if sources_file else Path(DEFAULT_SOURCES_FILE)
        self._sources: list[DailySource] = []
        self.load()

    def load(self):
        """Load sources from the JSON file, with fallback to the built-in main source."""
        try:
            if not self._sources_file.exists():
                logger.warning(
                    f"Sources file not found: {self._sources_file} — falling back to main source"
                )
                self._sources = [self._build_fallback()]
                return

            data = json.loads(self._sources_file.read_text(encoding="utf-8"))
            raw_sources = data.get("sources", [])
            if not raw_sources:
                logger.warning(f"No sources in {self._sources_file} — falling back to main source")
                self._sources = [self._build_fallback()]
                return

            self._sources = [DailySource.from_dict(s) for s in raw_sources]
            logger.info(f"Loaded {len(self._sources)} daily-report data sources")
        except Exception as e:
            logger.error(f"Failed to load sources file {self._sources_file}: {e} — falling back")
            self._sources = [self._build_fallback()]

    def _build_fallback(self) -> DailySource:
        return DailySource.from_dict(FALLBACK_MAIN_SOURCE)

    def get(self, source_id: str) -> DailySource | None:
        """Get a source by its id."""
        for s in self._sources:
            if s.id == source_id:
                return s
        return None

    def get_default(self) -> DailySource:
        """Get the default/main source (kind == daily, or id == tech_daily).

        Returns the first daily source, else the first source.
        """
        for s in self._sources:
            if s.is_daily:
                return s
        # Fallback: prefer tech_daily by id, else the first source
        return self.get("tech_daily") or self._sources[0]

    @property
    def sources(self) -> list[DailySource]:
        return list(self._sources)

    def list_ids(self) -> list[str]:
        return [s.id for s in self._sources]
