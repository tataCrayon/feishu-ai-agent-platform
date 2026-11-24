"""Daily Converter — extracts structured data from daily report markdown
and renders it through the push template.

The converter parses the structured sections of the daily report markdown
into template variables, then uses DailyTemplate to render the final message.
"""

from __future__ import annotations

import logging
import re

from app.domains.daily.template import DailyTemplate

logger = logging.getLogger("daily.converter")

# Template variable keys
VAR_DATE = "date"
VAR_SUMMARY = "summary"
VAR_GITHUB_ITEMS = "github_items"
VAR_NEWS_ITEMS = "news_items"
VAR_QUESTIONS_BLOCK = "questions_block"
VAR_TOMORROW_ITEMS = "tomorrow_items"
VAR_DATA_SOURCE = "data_source"


class DailyConverter:
    """Converts daily report markdown into push-friendly format.

    Extracts structured sections from the standardized daily report format
    and renders them through the configured template.
    """

    def __init__(self, template: DailyTemplate | None = None):
        self._template = template or DailyTemplate()

    def extract_variables(self, markdown: str, date_str: str = "") -> dict[str, str]:
        """Extract structured variables from daily report markdown.

        Parses the following sections:
        - 一句话导读 (summary)
        - 今日 GitHub 热门 (github_items)
        - 今日大厂与中文技术圈动态 (news_items)
        - 值得追问 (questions_block)
        - 明日关注 (tomorrow_items)
        - 数据说明 (data_source)

        Args:
            markdown: Full daily report markdown text.
            date_str: Date string for the template (e.g. "2026年8月14日").

        Returns:
            Dict of template variable names to their values.
        """
        variables = {
            VAR_DATE: date_str,
            VAR_SUMMARY: self._extract_summary(markdown),
            VAR_GITHUB_ITEMS: self._extract_github_items(markdown),
            VAR_NEWS_ITEMS: self._extract_news_items(markdown),
            VAR_QUESTIONS_BLOCK: self._extract_section(markdown, "值得追问"),
            VAR_TOMORROW_ITEMS: self._extract_section(markdown, "明日关注"),
            VAR_DATA_SOURCE: self._extract_data_source(markdown),
        }
        logger.debug(f"Extracted {len(variables)} variables from daily report")
        return variables

    def convert(self, markdown: str, date_str: str = "") -> str:
        """Convert daily report markdown to push-ready text.

        This is the main entry point. It extracts variables and renders
        through the template.

        Args:
            markdown: Full daily report markdown text.
            date_str: Date string (auto-extracted from markdown if empty).

        Returns:
            Push-ready text string.
        """
        if not date_str:
            date_str = self._extract_date_from_markdown(markdown)

        variables = self.extract_variables(markdown, date_str)
        return self._template.render(variables)

    # ── Section extractors ──

    def _extract_summary(self, markdown: str) -> str:
        """Extract the one-sentence summary from the daily report."""
        match = re.search(
            r">\s*一句话导读[：:]\s*(.+?)(?:\n|$)",
            markdown,
            re.DOTALL,
        )
        if match:
            return match.group(1).strip()
        return ""

    def _extract_github_items(self, markdown: str) -> str:
        """Extract GitHub trending items as a compact bullet list.

        Format: • repo — N⭐ description
        """
        section = self._extract_section_raw(markdown, "今日 GitHub 热门")
        if not section:
            return ""

        # Parse the markdown table
        lines = section.split("\n")
        items = []
        in_table = False
        for line in lines:
            line = line.strip()
            if not in_table and line.startswith("|"):
                in_table = True
                continue
            if not in_table:
                continue
            if not line.startswith("|"):
                continue

            cells = [c.strip() for c in line.split("|")[1:-1]]
            # Skip table separator rows (e.g. |---|---|)
            if len(cells) >= 1 and all(c.startswith("-") or c == "" for c in cells):
                continue
            if len(cells) >= 5:
                repo = cells[0].strip()
                # Extract repo name from markdown link [name](url)
                repo_match = re.search(r"\[([^\]]+)\]", repo)
                repo_name = repo_match.group(1) if repo_match else repo
                # Remove URL
                repo_name = re.sub(r"\(https?://[^)]+\)", "", repo_name).strip()

                star_text = cells[3].strip() if len(cells) > 3 else ""
                desc = cells[4].strip() if len(cells) > 4 else ""

                items.append(f"• {repo_name} — {star_text} {desc}")

        if not items:
            return ""

        return "\n".join(items[:8])  # Max 8 items

    def _extract_news_items(self, markdown: str) -> str:
        """Extract news items as a compact bullet list.

        Format: • 来源: 标题
        """
        section = self._extract_section_raw(markdown, "今日大厂与中文技术圈动态")
        if not section:
            return ""

        lines = section.split("\n")
        items = []
        in_table = False
        for line in lines:
            line = line.strip()
            if not in_table and line.startswith("|"):
                in_table = True
                continue
            if not in_table:
                continue
            if not line.startswith("|"):
                continue

            cells = [c.strip() for c in line.split("|")[1:-1]]
            # Skip table separator rows
            if len(cells) >= 1 and all(c.startswith("-") or c == "" for c in cells):
                continue
            if len(cells) >= 3:
                source = cells[0].strip()
                # Extract plain text from markdown links
                source = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", source)
                title = cells[2].strip() if len(cells) > 2 else ""
                # Title may contain its own link
                title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
                items.append(f"• {source}: {title}")

            if len(items) >= 10:
                break

        if not items:
            return ""

        return "\n".join(items)

    def _extract_section(self, markdown: str, section_title: str) -> str:
        """Extract a section's content as formatted text.

        Returns the section content as a bullet list or paragraph.
        """
        section = self._extract_section_raw(markdown, section_title)
        if not section:
            return ""

        # Clean up: remove extra whitespace, keep bullet points
        lines = []
        for line in section.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("|"):
                continue  # Skip table lines
            lines.append(line)

        return "\n".join(lines[:6])  # Max 6 lines

    def _extract_data_source(self, markdown: str) -> str:
        """Extract data source description."""
        section = self._extract_section_raw(markdown, "数据说明")
        if not section:
            return ""

        # Get first line of data source
        lines = [line.strip() for line in section.split("\n") if line.strip()]
        if lines:
            return lines[0][:100]  # Truncate
        return ""

    def _extract_date_from_markdown(self, markdown: str) -> str:
        """Extract date string from the markdown heading."""
        match = re.search(r"^#\s+技术动态日报\s*[·.]?\s*(.+?)$", markdown, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    # ── Helpers ──

    def _extract_section_raw(self, markdown: str, title: str) -> str:
        """Extract the raw content of a section by its title.

        Finds the section header (## Title) and returns text until the next
        section header (##) or end of string.

        Supports headers with trailing content in parentheses, e.g.
        "## 今日 GitHub 热门（趋势中的新动作）" matches title "今日 GitHub 热门".
        Also handles cases where the next section header is on the same line
        as the last content line (no newline separator).
        """
        # Match section header: ## Title (with optional trailing content)
        # The next section starts at a "##" that starts on its own line,
        # OR is preceded by a newline (inline ## after content)
        pattern = (
            rf"##\s*{re.escape(title)}(?:（[^）]*）)?\s*\n"
            r"(.+?)"
            r"(?=\n##\s|\Z)"
        )
        match = re.search(pattern, markdown, re.DOTALL)
        if match:
            section = match.group(1).strip()
            # Cut at any ## heading within the section (handles inline ## on same line)
            cut = re.search(r"##\s", section)
            if cut:
                section = section[:cut.start()]
            return section.strip()
        return ""

    @property
    def template(self) -> DailyTemplate:
        return self._template

    @template.setter
    def template(self, template: DailyTemplate):
        self._template = template