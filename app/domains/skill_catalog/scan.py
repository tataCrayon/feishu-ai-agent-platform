"""bare clone 扫描。只 git fetch + ls-tree + show frontmatter，不 checkout。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone

from app.domains.skill_catalog.model import CatalogEntry, CatalogSnapshot
from app.domains.skill_catalog.parse import (
    fallback_name_from_path,
    is_skill_path,
    parse_skill_frontmatter,
)

logger = logging.getLogger("skill_catalog.scan")

TZ = timezone(timedelta(hours=8))

# 这些分支是试验或已被覆盖的早期包。默认不进目录，配置可改。
DEFAULT_SKIP_BRANCHES = ("addNewSkill", "addNewSkill_v1", "addNewSkill_v2", "merge_all", "init")


def blob_url(repo_url: str, branch: str, path: str) -> str:
    """GitLab 浏览链接。repo_url 允许带 .git 后缀。"""
    base = repo_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    return f"{base}/-/blob/{branch}/{path}"


class CatalogScanner:
    """持有一份 bare clone，按分支抽出 Skill description。"""

    def __init__(
        self,
        repo_url: str,
        bare_dir: str,
        skip_branches: list[str] | None = None,
        fetch_timeout_sec: int = 120,
        activity_days: int = 30,
    ):
        self.repo_url = repo_url
        self.bare_dir = bare_dir
        self.skip_branches = list(skip_branches if skip_branches is not None else DEFAULT_SKIP_BRANCHES)
        self.fetch_timeout_sec = fetch_timeout_sec
        self.activity_days = max(activity_days, 1)

    async def scan(self, *, force_fetch: bool) -> CatalogSnapshot:
        """刷新远端引用（按需）并扫描。fetch 失败时带 error 返回，调用方不得覆盖旧快照。"""
        fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
        try:
            await asyncio.to_thread(self._ensure_clone, force_fetch)
            scanned, skipped, entries = await asyncio.to_thread(self._collect)
            activity = await asyncio.to_thread(self._collect_activity, scanned)
        except Exception as e:
            logger.warning("[skill_catalog] scan failed: %s", e)
            return CatalogSnapshot(
                fetched_at=fetched_at,
                repo_url=self.repo_url,
                branches_scanned=[],
                branches_skipped=list(self.skip_branches),
                error=str(e)[:300],
            )
        return CatalogSnapshot(
            fetched_at=fetched_at,
            repo_url=self.repo_url,
            branches_scanned=scanned,
            branches_skipped=skipped,
            entries=entries,
            recent_commits=activity[0],
            open_branches=activity[1],
        )

    def _ensure_clone(self, force_fetch: bool) -> None:
        os.makedirs(os.path.dirname(self.bare_dir) or ".", exist_ok=True)
        if not os.path.isdir(os.path.join(self.bare_dir, "refs")):
            self._git(
                ["clone", "--bare", self.repo_url, self.bare_dir],
                cwd=None,
                timeout=self.fetch_timeout_sec,
            )
            return
        if force_fetch:
            self._git(
                ["fetch", "--all", "--prune"],
                cwd=self.bare_dir,
                timeout=self.fetch_timeout_sec,
            )

    def _collect(self) -> tuple[list[str], list[str], list[CatalogEntry]]:
        raw = self._git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=self.bare_dir,
            timeout=30,
        )
        heads = [line.strip() for line in raw.splitlines() if line.strip()]
        skip = set(self.skip_branches)
        scanned: list[str] = []
        skipped = sorted(skip & set(heads))
        # 配置里写了、仓库里没有的跳过项也告诉用户，避免「以为扫过了」
        skipped = sorted(set(skipped) | (skip - set(heads)))
        entries: list[CatalogEntry] = []
        for branch in heads:
            if branch in skip:
                continue
            scanned.append(branch)
            entries.extend(self._scan_branch(branch))
        entries.sort(key=lambda e: (e.branch, e.name, e.path))
        return scanned, skipped, entries

    def _scan_branch(self, branch: str) -> list[CatalogEntry]:
        listed = self._git(
            ["ls-tree", "-r", "--name-only", branch],
            cwd=self.bare_dir,
            timeout=30,
        )
        commit, committed_at, author = self._commit_meta(branch)
        found: list[CatalogEntry] = []
        for path in listed.splitlines():
            if not is_skill_path(path):
                continue
            text = self._git(
                ["show", f"{branch}:{path}"],
                cwd=self.bare_dir,
                timeout=30,
            )
            parsed = parse_skill_frontmatter(text, fallback_name_from_path(path))
            if parsed is None:
                continue
            found.append(CatalogEntry(
                name=parsed["name"],
                description=parsed["description"],
                version=parsed["version"],
                branch=branch,
                commit=commit,
                committed_at=committed_at,
                path=path,
                blob_url=blob_url(self.repo_url, branch, path),
                missing=parsed["missing"],
                out_of_scope=parsed["out_of_scope"],
                owner=parsed["owner"],
                autonomy=parsed["autonomy"],
                tools=parsed["tools"],
                success_criteria=parsed["success_criteria"],
                escalate_to=parsed["escalate_to"],
                review_by=parsed["review_by"],
                manual_status=parsed["status"],
                author=author,
            ))
        return found

    def _collect_activity(self, scanned_branches: list[str]) -> tuple[list[dict], list[str]]:
        """活动证据：全库近 N 天提交（含在飞分支，不止 master）+ 分支在飞清单。

        master 可能长期停更（实测有停在数月的仓库），活动以最新分支为准。
        任何一步失败只丢该块证据，不让整次扫描失败。
        """
        commits: list[dict] = []
        try:
            since = (datetime.now(TZ) - timedelta(days=self.activity_days)).strftime("%Y-%m-%d")
            # --all 覆盖在飞分支；--since 由 git 按提交时间过滤
            raw = self._git(
                ["log", "--all", f"--since={since}", "--date=short",
                 "--format=%h%x1f%an%x1f%ad%x1f%s"],
                cwd=self.bare_dir,
                timeout=30,
            )
            for line in raw.splitlines():
                parts = line.strip().split("\x1f")
                if len(parts) == 4:
                    commits.append({
                        "sha": parts[0], "author": parts[1],
                        "date": parts[2], "subject": parts[3],
                    })
        except Exception as e:
            logger.warning("[skill_catalog] recent commits unavailable: %s", e)

        open_branches = [b for b in scanned_branches if b != "master"]
        return commits, open_branches

    def _commit_meta(self, branch: str) -> tuple[str, str, str]:
        """分支头 commit 的 sha / 时间 / 提交人。"""
        raw = self._git(
            ["log", "-1", "--format=%H%x09%cI%x09%an", branch],
            cwd=self.bare_dir,
            timeout=30,
        ).strip()
        parts = raw.split("\t")
        if len(parts) < 3:
            return parts[0][:12] if parts else "", "", ""
        return parts[0][:12], parts[1].strip(), parts[2].strip()

    def _git(self, args: list[str], cwd: str | None, timeout: int) -> str:
        # 内网 GitLab 走系统代理会失败。与 merge-check 同一条红线。
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            # stdin 必须断掉：本模块也跑在 MCP stdio 子进程里，那里的 stdin 是
            # JSON-RPC 管道。子进程继承这个管道后，Windows 下 git 起手就卡死
            # （实测 `git --version` 20s 超时，同一命令 DEVNULL 后 0.03s 返回）。
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "git failed").strip()
            raise RuntimeError(f"git {' '.join(args[:3])} 失败: {err[:240]}")
        return result.stdout
