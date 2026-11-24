"""目录条目与快照。只存 description，不存正文。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class CatalogEntry:
    """一个分支上的一份 Skill。同名跨分支不合并。

    治理字段（owner/autonomy/…）缺省空串：快照只存事实，
    规范状态由 app/domains/skill_catalog/status.py 在渲染时推导。
    """

    name: str
    description: str
    version: str
    branch: str
    commit: str
    committed_at: str
    path: str
    blob_url: str
    # frontmatter 里能直接看到的治理缺口；审查用，不参与查重打分
    missing: list[str] = field(default_factory=list)
    # 治理八项（docs/features/skill-catalog.md §4.1）。description 已有，不重复存。
    out_of_scope: str = ""
    owner: str = ""
    autonomy: str = ""
    tools: str = ""
    success_criteria: str = ""
    escalate_to: str = ""
    review_by: str = ""
    # frontmatter status 手工强制（仅「已下线」类值参与推导，其余状态一律推导）
    manual_status: str = ""
    # 提交人：该分支最近一次改动此文件的人（不是 frontmatter owner）
    author: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["author"] = self.author
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> CatalogEntry:
        return cls(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or ""),
            branch=str(raw.get("branch") or ""),
            commit=str(raw.get("commit") or ""),
            committed_at=str(raw.get("committed_at") or ""),
            path=str(raw.get("path") or ""),
            blob_url=str(raw.get("blob_url") or ""),
            missing=list(raw.get("missing") or []),
            out_of_scope=str(raw.get("out_of_scope") or ""),
            owner=str(raw.get("owner") or ""),
            autonomy=str(raw.get("autonomy") or ""),
            tools=str(raw.get("tools") or ""),
            success_criteria=str(raw.get("success_criteria") or ""),
            escalate_to=str(raw.get("escalate_to") or ""),
            review_by=str(raw.get("review_by") or ""),
            manual_status=str(raw.get("manual_status") or ""),
            author=str(raw.get("author") or ""),
        )


@dataclass
class CatalogSnapshot:
    """一次成功扫描的结果。失败不覆盖上一份。"""

    fetched_at: str
    repo_url: str
    branches_scanned: list[str]
    branches_skipped: list[str]
    error: str = ""
    entries: list[CatalogEntry] = field(default_factory=list)
    # 活动证据：master 近 N 天提交（path 属于 skills 目录的）；分支在飞（未合并 MR）
    recent_commits: list[dict] = field(default_factory=list)
    open_branches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "repo_url": self.repo_url,
            "branches_scanned": self.branches_scanned,
            "branches_skipped": self.branches_skipped,
            "error": self.error,
            "entries": [e.to_dict() for e in self.entries],
            "recent_commits": list(self.recent_commits),
            "open_branches": list(self.open_branches),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> CatalogSnapshot:
        return cls(
            fetched_at=str(raw.get("fetched_at") or ""),
            repo_url=str(raw.get("repo_url") or ""),
            branches_scanned=list(raw.get("branches_scanned") or []),
            branches_skipped=list(raw.get("branches_skipped") or []),
            error=str(raw.get("error") or ""),
            entries=[CatalogEntry.from_dict(e) for e in (raw.get("entries") or [])],
            recent_commits=list(raw.get("recent_commits") or []),
            open_branches=list(raw.get("open_branches") or []),
        )
