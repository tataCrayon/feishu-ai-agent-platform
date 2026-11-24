"""查询、查重、审查、状态报告。全部是确定性文本，不调 LLM。"""

from __future__ import annotations

import re
from collections import Counter

from app.domains.skill_catalog.model import CatalogEntry, CatalogSnapshot
from app.domains.skill_catalog.status import STATUS_EMOJI, status_counts

_TOKEN_RE = re.compile(r"[a-z0-9][\w.-]{1,}|[\u4e00-\u9fff]{2,}")

# 审查只核对文件里能直接看到的项。不打通过/不通过。
_REVIEW_LABELS = {
    "owner": "具名 owner（不能写团队）",
    "out_of_scope": "明确不做",
    "review_by": "下次复审日",
    "version": "version",
}


def find_similar(snap: CatalogSnapshot, query: str, limit: int = 3) -> list[tuple[CatalogEntry, int]]:
    """用名字、description 的词重叠找最接近的 1～N 条。0 分不返回。"""
    q_tokens = _tokens(query)
    if not q_tokens:
        return []
    scored: list[tuple[CatalogEntry, int]] = []
    for entry in snap.entries:
        score = len(q_tokens & _tokens(f"{entry.name} {entry.description}"))
        # 名字被问句包含时加权，避免「nacos」只命中描述里的偶然词
        if entry.name.lower() in query.lower():
            score += 5
        if score > 0:
            scored.append((entry, score))
    scored.sort(key=lambda item: (-item[1], item[0].branch, item[0].name))
    return scored[:limit]


def review_entry(snap: CatalogSnapshot, name: str) -> list[CatalogEntry]:
    """按名字找全部分支上的同名 Skill。大小写不敏感，支持子串。"""
    needle = name.strip().lower()
    if not needle:
        return []
    exact = [e for e in snap.entries if e.name.lower() == needle]
    if exact:
        return exact
    return [e for e in snap.entries if needle in e.name.lower()]


def format_catalog(snap: CatalogSnapshot, branch: str = "") -> str:
    """按分支分组的目录。未点名分支时先给每支数量，点名后再展开。"""
    wanted = branch.strip()
    groups: dict[str, list[CatalogEntry]] = {}
    for entry in snap.entries:
        groups.setdefault(entry.branch, []).append(entry)

    lines = ["团队 Skill 目录", _footer(snap), ""]
    if wanted:
        matched = {b: items for b, items in groups.items() if wanted.lower() in b.lower()}
        if not matched:
            lines.append(f"快照里没有分支「{wanted}」。现有分支：{'、'.join(groups)}")
            return "\n".join(lines)
        for b, items in matched.items():
            lines.extend(_branch_block(b, items))
        return "\n".join(lines).rstrip()

    # 全量列表：每个能力一行五要素。分支多、能力多时也保持这个格式。
    lines.append(f"共 {len(snap.entries)} 个能力：")
    for entry in snap.entries:
        lines.append(_entry_line(entry))
    lines.append("")
    lines.append("同名出现在多个分支时不会合并，各列一行。")
    return "\n".join(lines)


def format_similar(snap: CatalogSnapshot, query: str, hits: list[tuple[CatalogEntry, int]]) -> str:
    lines = [f"立项检查：有没有接近「{query.strip()}」的现成 Skill", _footer(snap), ""]
    if not hits:
        lines.append("这份快照里没有接近项。这不是「可以立项」——只说明目录里没看到。")
        return "\n".join(lines)
    lines.append("先看这几条，再决定要不要新建。Bot 不拦截。")
    for entry, score in hits:
        lines.append(_entry_line(entry, extra=f"重叠 {score}"))
    return "\n".join(lines)


def format_review(entries: list[CatalogEntry], snap: CatalogSnapshot) -> str:
    if not entries:
        return "\n".join([
            "审查：快照里没有这个名字。",
            _footer(snap),
            "可能在被跳过的分支上，或文件没有 description。",
        ])
    lines = [
        f"审查 {entries[0].name}（对照清单打勾，不打通过/不通过）",
        _footer(snap),
        "",
    ]
    for entry in entries:
        lines.append(f"## {entry.branch} · {entry.path}")
        lines.append(f"- 链接：{entry.blob_url}")
        lines.append(f"- 提交：{entry.commit} {entry.committed_at}")
        lines.append(f"- description：{entry.description}")
        present = [label for key, label in _REVIEW_LABELS.items() if key not in entry.missing]
        absent = [label for key, label in _REVIEW_LABELS.items() if key in entry.missing]
        if present:
            lines.append("- 已有：" + "、".join(present))
        if absent:
            lines.append("- 缺少：" + "、".join(absent))
        else:
            lines.append("- 缺少：无（这四项都能从 frontmatter 读到）")
        lines.append("")
    lines.append("调用记录、自治上限、停止后找谁不在本清单里——文件里没有，就不下结论。")
    return "\n".join(lines).rstrip()


def _branch_block(branch: str, items: list[CatalogEntry]) -> list[str]:
    lines = [f"## {branch}（{len(items)}）"]
    for entry in items:
        lines.append(_entry_line(entry))
    lines.append("")
    return lines


def _entry_line(entry: CatalogEntry, extra: str = "") -> str:
    """列表行：名称、描述、分支、Git 地址、提交人。"""
    tail = f" · {extra}" if extra else ""
    author = f" · 提交人 {entry.author}" if entry.author else ""
    return (
        f"- **{entry.name}**（{entry.version or '无版本'}）{author}{tail}\n"
        f"  {_clip(entry.description, 70)}\n"
        f"  分支 {entry.branch} · {entry.blob_url}"
    )


def _footer(snap: CatalogSnapshot) -> str:
    scanned = "、".join(snap.branches_scanned) or "（无）"
    skipped = "、".join(snap.branches_skipped) or "（无）"
    return f"快照 {snap.fetched_at} · 已扫 {scanned} · 跳过 {skipped}"


# ── 资产概览（盘点问法的紧凑视图）──────────────────────────────────────

# SDLC 链分组：按能力名的确定性映射（不做 LLM，纯名单规则）。
# 顺序即展示顺序；未命中任何组的进「其他」。
_CHAIN_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("开发实施链（X-toolkit）", ("x-toolkit", "flp-coding", "flp-code-commit",
                                "flp-code-review", "flp-unit-test",
                                "flp-interface-design", "flp-readme-summary",
                                "api-documentation-generator")),
    ("设计前置链（A-toolkit）", ("a-toolkit", "flp-req-analysis",
                                "flp-changepoints-check", "flp-database-design",
                                "flp-technical-design", "flp-userstory-breakdown",
                                "flp-story-points-estimation")),
    ("测试专项", ("java-junit", "java-testing-advanced")),
    ("需求/文档", ("flp-fsdoc-fetcher", "flp-service-overview",
                   "flp-system-overview", "release-checklist")),
    ("平台运维/专项", ("nacos-guard", "mysql-guard", "ops-assistant",
                       "release-order", "feishu-fill-workdays",
                       "feishu-issue-reader", "feishu-task-creator",
                       "feishu-project-create-story", "news-sql-generator")),
    ("流程编排（R-toolkit）", ("r-toolkit",)),
]


def format_inventory_overview(snap: CatalogSnapshot) -> str:
    """团队资产盘点视图：同名多分支合并为一能力，按 SDLC 链分组，不带链接。

    与 format_catalog（94 行全量含 blob_url）的差别：聊天场景问「有哪些」
    时 ~31 行紧凑概览更合适；要链接/分支明细走二跳（点名查询单能力）。
    """
    # 同名合并：版本非空优先，其次取提交最新的一条做代表
    by_name: dict[str, CatalogEntry] = {}
    for entry in snap.entries:
        cur = by_name.get(entry.name)
        if cur is None:
            by_name[entry.name] = entry
            continue
        cur_v = (cur.version or "").lower().lstrip("v")
        new_v = (entry.version or "").lower().lstrip("v")
        if (new_v and not cur_v) or (
            bool(new_v) and bool(cur_v) and entry.committed_at > cur.committed_at
        ):
            by_name[entry.name] = entry

    names = set(by_name)
    total_branch_entries = len(snap.entries)
    lines = [
        "📚 团队能力资产盘点",
        _footer(snap),
        "",
        f"共 {len(names)} 项能力（{total_branch_entries} 个分支条目，同名多分支已合并）：",
    ]

    assigned: set[str] = set()
    for title, members in _CHAIN_GROUPS:
        hits = [by_name[m] for m in members if m in names]
        if not hits:
            continue
        assigned.update(m for m in members if m in names)
        lines.append("")
        lines.append(f"**{title}**")
        for entry in hits:
            lines.append(_overview_line(entry))

    rest = sorted(names - assigned)
    if rest:
        lines.append("")
        lines.append("**其他**")
        for name in rest:
            lines.append(_overview_line(by_name[name]))

    lines.append("")
    lines.append("看单个能力详情/链接说「查 + 名字」；立项前查重直接说需求～")
    return "\n".join(lines)


def _overview_line(entry: CatalogEntry) -> str:
    """概览行：名称（版本）+ 70 字描述，不带 blob_url（二跳再给）。"""
    ver = f"（{entry.version}）" if entry.version else ""
    return f"• **{entry.name}**{ver}：{_clip(entry.description, 70)}"


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text.lower())}


# ── 规范状态（docs/features/skill-catalog.md §4.1） ──────────────────────


def find_status_entries(snap: CatalogSnapshot, name: str) -> list[CatalogEntry]:
    """按名字找状态对象。与 review_entry 同策略：精确优先，子串兜底。"""
    return review_entry(snap, name)


def format_status_board(snap: CatalogSnapshot, limit: int = 12) -> str:
    """去留总表：五要素列表为主体，治理摘要收进脚注。"""
    entries = snap.entries
    counts = status_counts(entries)
    total = len(entries)

    lines = [
        "📚 团队 Skill 一览",
        _footer(snap),
        "",
        f"共 {total} 个："
        + " · ".join(f"{STATUS_EMOJI[s]}{s} {n}" for s, n in counts.items() if n),
        "",
    ]
    for entry in entries:
        lines.append(_entry_line(entry))

    lines.extend(_activity_block(snap))
    lines.append("")
    lines.append("看单个能力说「查 + 名字」；想新建先问「有没有现成的」。")
    return "\n".join(lines).rstrip()


def format_status_card(entries: list[CatalogEntry], snap: CatalogSnapshot) -> str:
    """单个能力：五要素 + 一句话描述。治理与规范暂不显示。"""
    if not entries:
        return "\n".join([
            "查询：快照里没有这个名字。",
            _footer(snap),
            "可能在被跳过的分支上，或文件没有 description。",
        ])
    lines = [f"📦 {entries[0].name}", _footer(snap), ""]
    for entry in entries:
        lines.append(f"**{entry.name}**（{entry.version or '无版本'}）")
        lines.append(f"- 描述：{_clip(entry.description, 100)}")
        lines.append(f"- 分支：{entry.branch}")
        lines.append(f"- Git：{entry.blob_url}")
        author = entry.author or "（该分支头提交人未取到）"
        lines.append(f"- 提交人：{author} · {entry.committed_at} · {entry.commit}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _activity_block(snap: CatalogSnapshot) -> list[str]:
    """近 30 天活跃 + 分支在飞。证据来自快照，没有就不写。"""
    lines: list[str] = []
    if snap.recent_commits:
        authors = Counter(c.get("author", "?") for c in snap.recent_commits)
        top = "、".join(f"{a}({n})" for a, n in authors.most_common(5))
        lines.append("")
        lines.append(f"🔧 近 30 天提交 {len(snap.recent_commits)} 次（含在飞分支）：{top}")
        for c in snap.recent_commits[:5]:
            lines.append(f"  - {c.get('date', '')} {c.get('author', '')} "
                         f"{_clip(c.get('subject', ''), 36)}")
    if snap.open_branches:
        lines.append("")
        lines.append(f"🌱 分支在飞（待合并 MR）：{'、'.join(snap.open_branches)}")
    return lines


def _clip(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"
