"""Daily Template — template loading, variable extraction, and rendering.

Templates are plain text files with {{variable}} placeholders.
Templates are re-read from disk on each render (hot-reload support).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("daily.template")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # -> feishu-agent-platform/
DEFAULT_TEMPLATE = PROJECT_DIR / "app" / "domains" / "daily" / "templates" / "default_push.md"


class DailyTemplate:
    """Template loader and renderer.

    Loads a template file from disk and renders it with variables.

    Usage:
        template = DailyTemplate("app/domains/daily/templates/default_push.md")
        result = template.render({"date": "2026年8月14日", ...})
    """

    def __init__(self, template_path: str | None = None):
        self._path = Path(template_path) if template_path else DEFAULT_TEMPLATE
        self._cached_content: str | None = None
        self._cached_mtime: float = 0.0

    def _load(self) -> str:
        """Load template from file, with caching across hot-reload.

        Re-reads if the file has been modified since last load.
        """
        if not self._path.exists():
            logger.warning(f"Template not found: {self._path}, using default")
            self._path = DEFAULT_TEMPLATE

        try:
            mtime = self._path.stat().st_mtime
            if self._cached_content is None or mtime > self._cached_mtime:
                self._cached_content = self._path.read_text(encoding="utf-8")
                self._cached_mtime = mtime
                logger.info(f"Template loaded: {self._path.name} (mtime={mtime})")
            return self._cached_content
        except Exception as e:
            logger.error(f"Failed to load template {self._path}: {e}")
            return ""

    def render(self, variables: dict[str, str]) -> str:
        """Render template with given variables.

        Each {{variable}} in the template is replaced with the corresponding
        value from the variables dict. Unknown variables are replaced with
        empty string.

        Args:
            variables: Dict of variable_name -> replacement text.

        Returns:
            Rendered template string.
        """
        content = self._load()
        if not content:
            return ""

        def _replace(match: re.Match) -> str:
            key = match.group(1).strip()
            return variables.get(key, "")

        result = re.sub(r"\{\{(\w+)\}\}", _replace, content)
        # Clean up: remove trailing whitespace lines, collapse multiple blank lines
        result = re.sub(r" +\n", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    @property
    def path(self) -> Path:
        return self._path

    @path.setter
    def path(self, template_path: str):
        self._path = Path(template_path)
        self._cached_content = None