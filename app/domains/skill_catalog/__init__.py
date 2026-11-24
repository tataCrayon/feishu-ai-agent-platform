"""团队 Skill 目录 — 按分支读 description，供查询与立项检查。

事实源是 team-skills/skills 仓库的各分支，不是 master，也不是 feishu-agent-platform
自己的 bot/skills/。Bot 只查询：不安装、不写回、不拦截立项。

快照一天一刷（可手动强制刷新）。查询读本地 JSON，不在每次提问时 fetch。
"""

from app.domains.skill_catalog.model import CatalogEntry, CatalogSnapshot
from app.domains.skill_catalog.query import (
    find_similar,
    find_status_entries,
    format_catalog,
    format_review,
    format_similar,
    format_status_board,
    format_status_card,
    review_entry,
)
from app.domains.skill_catalog.store import CatalogStore

__all__ = [
    "CatalogEntry",
    "CatalogSnapshot",
    "CatalogStore",
    "find_similar",
    "find_status_entries",
    "format_catalog",
    "format_review",
    "format_similar",
    "format_status_board",
    "format_status_card",
    "review_entry",
]
