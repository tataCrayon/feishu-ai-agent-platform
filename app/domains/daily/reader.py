"""Daily Reader — reads local report markdown files for a data source.

Reads from a single data source directory (configured via `DailySource`).
Supports multiple file-naming conventions via the source's match regex +
period template. The time dimension is normalized to a `period` string:
- daily source:   "2026年8月17日"
- weekly source:  "2026年8月第2周"
- monthly source: "2026年8月"

For backward compatibility, `DailyReader` still accepts a plain `str`
directory (wrapped into a built-in daily source).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.domains.daily.source import DailySource, DailySourceRegistry

logger = logging.getLogger("daily.reader")

TZ = timezone(timedelta(hours=8))

# Matches a markdown title heading like "# 技术动态日报 · 2026年8月" or "# Skill 排行 · 2026年8月"
_HEADING_PERIOD_RE = re.compile(r"^\s*#\s+.+?[·.]?\s*(\d{4})年(\d{1,2})月(?:第?(\d{1,2})?周)?", re.MULTILINE)


class DailyReader:
    """Reads report markdown files from a single data source directory.

    Args:
        source: A DailySource describing the data source directory and its
            file-naming rules. For backward compatibility, a plain str
            directory is accepted and wrapped into a built-in daily source.
    """

    def __init__(self, source: DailySource | str):
        if isinstance(source, str):
            # Backward-compatible: wrap a bare directory into the default main source
            registry = DailySourceRegistry()
            main = registry.get_default()
            source = DailySource(
                id=main.id,
                name=main.name,
                path=Path(source),
                kind=main.kind,
                cadence=main.cadence,
                match_regex=main.match_regex,
                period_template=main.period_template,
                exclude=main.exclude,
                preview_chars=main.preview_chars,
                period_from_heading=main.period_from_heading,
            )
        self._source = source
        self._report_dir = source.path
        self._pattern = re.compile(source.match_regex) if source.match_regex else None
        self._exclude = set(source.exclude)
        self._period_template = source.period_template

    # ── period extraction ──

    def _extract_period(self, filename: str) -> str | None:
        """Extract a period label from a filename using the source's rules.

        First tries the match regex (named groups + period template). If the
        source declares `period_from_heading` (or no template matched), falls
        back to parsing the first title heading of the file.
        """
        # 1) Named-group regex + period template
        if self._pattern:
            m = self._pattern.match(filename)
            if m and self._period_template:
                try:
                    return self._period_template.format(**m.groupdict())
                except (KeyError, IndexError):
                    pass
            # regex matched but no template → try heading (monthly kind)
            if m and self._source.period_from_heading:
                return self._extract_period_from_heading(filename)

        # 2) Heading fallback
        if self._source.period_from_heading:
            return self._extract_period_from_heading(filename)
        return None

    def _extract_period_from_heading(self, filename: str) -> str | None:
        """Parse the first markdown title heading of the file for a period."""
        filepath = self._report_dir / filename
        if not filepath.exists():
            return None
        try:
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        return self._parse_heading_period(line)
                    break
        except OSError as e:
            logger.warning(f"Failed to read heading of {filepath}: {e}")
        return None

    def _parse_heading_period(self, line: str) -> str | None:
        """Extract period from a heading line, e.g. '# Skill 排行 · 2026年8月'."""
        m = _HEADING_PERIOD_RE.match(line)
        if not m:
            return None
        year, month, week = m.group(1), m.group(2), m.group(3)
        if week:
            return f"{year}年{month}月第{week}周"
        return f"{year}年{month}月"

    # ── legacy date-based helpers (daily source) ──

    def _date_to_filename(self, dt: date) -> str:
        """Convert a date to the expected filename (daily source only)."""
        if not self._source.is_daily:
            return ""
        year, month, day = dt.year, dt.month, dt.day
        return f"{year}年{month}月{day}日_技术动态日报.md"

    def _filename_to_date(self, filename: str) -> date | None:
        """Extract a date from a filename (daily source only)."""
        if not self._source.is_daily:
            return None
        match = re.match(
            r"(\d{4})年(\d{1,2})月(\d{1,2})日_.*\.md$",
            filename,
        )
        if not match:
            return None
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    def _date_to_period(self, dt: date) -> str:
        """Convert a date to the daily-source period string."""
        return f"{dt.year}年{dt.month}月{dt.day}日"

    # ── file discovery ──

    def find_files(self, limit: int = 30) -> list[tuple[str, Path]]:
        """List the source's matching files as (period, path), newest first.

        Args:
            limit: Maximum number of files to return.
        """
        if not self._report_dir.exists():
            logger.warning(f"Report directory not found: {self._report_dir}")
            return []

        results: list[tuple[str, Path]] = []
        for f in self._report_dir.iterdir():
            if not f.is_file() or f.suffix != ".md":
                continue
            if f.name in self._exclude:
                continue
            period = self._extract_period(f.name)
            if period:
                results.append((period, f))

        # Sort newest first by period string (year>month>day>week lexicographically works
        # for zero-padded forms; our non-padded forms sort correctly by length grouping too).
        results.sort(key=lambda t: t[0], reverse=True)
        if limit and limit > 0:
            return results[:limit]
        return results

    def _find_by_period(self, period: str) -> list[Path]:
        """Find all files whose period label matches the given period."""
        matches = []
        for p, path in self.find_files(limit=0):
            if p == period:
                matches.append(path)
        return matches

    # ── read methods ──

    def read_today(self) -> tuple[str, dict] | None:
        """Read today's report (daily source only).

        Returns:
            (markdown_text, metadata_dict) if found, None otherwise.
            For non-daily sources returns None.
        """
        if not self._source.is_daily:
            return None
        today = datetime.now(TZ).date()
        return self.read_by_period(self._date_to_period(today))

    def read_by_date(self, dt: date) -> tuple[str, dict] | None:
        """Read the report for a specific date (daily source only)."""
        if not self._source.is_daily:
            return None
        return self.read_by_period(self._date_to_period(dt))

    def read_latest(self) -> tuple[str, dict] | None:
        """Read the most recent file in the source.

        Useful for weekly/monthly sources that have no 'today'.
        """
        files = self.find_files(limit=1)
        if not files:
            return None
        _, path = files[0]
        period, _ = files[0]
        return self._read_file(path, period)

    def read_by_period(self, period: str) -> tuple[str, dict] | None:
        """Read report(s) for a given period label.

        If multiple files share the same period (e.g. a weekly source with
        several articles that week), their contents are concatenated.

        Returns:
            (markdown_text, metadata_dict) if found, None otherwise.
        """
        paths = self._find_by_period(period)
        if not paths:
            logger.info(f"No report found for period {period!r} in {self._report_dir}")
            return None
        return self._read_file(paths[0], period, extra_files=paths[1:])

    def _read_file(self, path: Path, period: str, extra_files: list[Path] | None = None) -> tuple[str, dict] | None:
        """Read one or more files into a single text + metadata."""
        try:
            texts = [path.read_text(encoding="utf-8")]
            for extra in (extra_files or []):
                texts.append(f"\n\n---\n\n{extra.read_text(encoding='utf-8')}")

            text = "".join(texts)
            metadata = {
                "period": period,
                "source_id": self._source.id,
                "source_name": self._source.name,
                "kind": self._source.kind,
                "date_str": period,
                "filename": path.name,
                "filepath": str(path),
                "size": len(text),
                "file_count": 1 + len(extra_files or []),
            }
            logger.info(f"Read report: {path.name} (period={period}, {len(text)} chars)")
            return text, metadata
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            return None

    def list_available_dates(self, limit: int = 30) -> list[date]:
        """List available dates (daily source only), newest first.

        Backward-compatible alias for the daily source; for non-daily sources
        returns [].
        """
        if not self._source.is_daily:
            return []
        dates = []
        for period, _ in self.find_files(limit=limit):
            m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日$", period)
            if m:
                try:
                    dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                except ValueError:
                    continue
        return dates
