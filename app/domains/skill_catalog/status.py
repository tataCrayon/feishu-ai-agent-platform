"""规范状态机 — 八项治理字段齐全度 + 复审日期 → 草稿/生产/未验证/下线候选/已下线。

原则（docs/features/skill-catalog.md §4.1）：
- 人只填证据字段，状态由本模块在**渲染时**推导（快照只存事实，状态即时算），
  因此复审到期即使不重新扫描也会自动降级——「复审过期是状态变化，不是提醒」。
- 八项字段：目的(description)/明确不做(out_of_scope)/具名 owner/自治上限(autonomy)/
  允许的工具(tools)/成功标准(success_criteria)/停止后找谁(escalate_to)/下次复审日(review_by)。
- 缺任一项 = 草稿；八项全 + 复审未到期 = 生产；复审过期 ≤90 天 = 未验证；
  过期超一个复审周期（90 天）未补审 = 下线候选；frontmatter status 强制 = 已下线。
"""

from __future__ import annotations

from datetime import date

# 八项字段 → 展示名。description 是进目录的先决条件，故状态推导只查后七项。
GOVERNANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("description", "目的与触发场景"),
    ("out_of_scope", "明确不做"),
    ("owner", "具名 owner"),
    ("autonomy", "自治上限"),
    ("tools", "允许的工具"),
    ("success_criteria", "成功标准"),
    ("escalate_to", "停止后找谁"),
    ("review_by", "下次复审日"),
)

# 状态推导实际检查的字段（除 description 外的七项）
_REQUIRED_KEYS: tuple[str, ...] = tuple(k for k, _ in GOVERNANCE_FIELDS[1:])

# 复审周期：过期多久算「连续一个周期无复审动作」→ 下线候选
RETIRE_CYCLE_DAYS = 90

STATUS_DRAFT = "草稿"
STATUS_PROD = "生产"
STATUS_UNVERIFIED = "未验证"
STATUS_RETIRE_CANDIDATE = "下线候选"
STATUS_RETIRED = "已下线"

STATUS_EMOJI: dict[str, str] = {
    STATUS_PROD: "🟢",
    STATUS_DRAFT: "🟡",
    STATUS_UNVERIFIED: "🟠",
    STATUS_RETIRE_CANDIDATE: "🔴",
    STATUS_RETIRED: "⚫",
}

# frontmatter status 手工强制值里视为「已下线」的写法（归档不删除）
_RETIRED_WORDS = {"已下线", "下线", "停用", "retired", "archived", "deprecated", "offline"}


def missing_fields(entry) -> list[str]:
    """返回缺失的治理字段展示名（按八项顺序）。description 缺失时不会到这（进不了目录）。"""
    return [
        label
        for key, label in GOVERNANCE_FIELDS
        if key != "description" and not getattr(entry, key, "").strip()
    ]


def review_date_of(entry) -> date | None:
    """解析 review_by 为日期；解析不了返回 None（格式不合格，按草稿处理）。"""
    raw = entry.review_by.strip()
    if not raw:
        return None
    for candidate in (raw, raw.replace("/", "-"), raw.replace(".", "-")):
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


def derive_status(entry, today: date | None = None) -> tuple[str, str]:
    """推导规范状态。返回 (状态, 一句话依据)。

    快照只存事实，状态在读取时按今天日期推导——复审到期自动降级无需重扫。
    """
    today = today or date.today()

    manual = entry.manual_status.strip().lower()
    if manual in _RETIRED_WORDS:
        return STATUS_RETIRED, "frontmatter status 强制标记（归档不删除）"

    missing = missing_fields(entry)
    if missing:
        return STATUS_DRAFT, f"缺 {len(missing)} 项治理字段（{ '、'.join(missing[:3]) }{'…' if len(missing) > 3 else ''}）"

    rd = review_date_of(entry)
    if rd is None:
        return STATUS_DRAFT, "review_by 不是可解析的日期（YYYY-MM-DD）"

    if rd >= today:
        return STATUS_PROD, f"八项齐全，复审日 {rd.isoformat()} 未到期"
    overdue = (today - rd).days
    if overdue <= RETIRE_CYCLE_DAYS:
        return (
            STATUS_UNVERIFIED,
            f"复审日 {rd.isoformat()} 已过期 {overdue} 天——自动降级，复审后恢复",
        )
    return (
        STATUS_RETIRE_CANDIDATE,
        f"复审日 {rd.isoformat()} 过期 {overdue} 天（超一个复审周期 {RETIRE_CYCLE_DAYS} 天）"
        "——进入下线候选，由 owner 复审决定去留",
    )


def status_counts(entries) -> dict[str, int]:
    """全库状态计数（渲染去留总表用）。"""
    counts = {STATUS_PROD: 0, STATUS_DRAFT: 0, STATUS_UNVERIFIED: 0,
              STATUS_RETIRE_CANDIDATE: 0, STATUS_RETIRED: 0}
    for entry in entries:
        status, _ = derive_status(entry)
        counts[status] = counts.get(status, 0) + 1
    return counts
