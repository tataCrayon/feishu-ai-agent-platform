"""从 SKILL.md / skill.md 抽出目录需要的字段。不保留正文。"""

from __future__ import annotations

import re

import yaml

# 治理字段：解析进快照（状态推导在 status.py，渲染时按当天日期算）。
# description 是进目录的先决条件，单独校验。
_GOVERNANCE_KEYS = ("out_of_scope", "owner", "autonomy", "tools",
                    "success_criteria", "escalate_to", "review_by", "status")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_frontmatter(text: str, fallback_name: str) -> dict | None:
    """解析 frontmatter。没有 description 的文件不进目录。

    Returns:
        name/description/version/missing/治理八项/…，或 None（无法当作 Skill 目录项）。
    """
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None

    description = str(meta.get("description") or "").strip()
    if not description:
        return None
    name = str(meta.get("name") or fallback_name).strip() or fallback_name
    version = str(meta.get("version") or "").strip()

    missing = [key for key in _GOVERNANCE_KEYS[:-1]  # status 不算缺口（手工强制项）
               if not _scalar(meta.get(key)).strip()]
    if not version:
        missing.append("version")
    parsed = {
        "name": name,
        "description": _one_line(description),
        "version": version,
        "missing": missing,
    }
    for key in _GOVERNANCE_KEYS:
        parsed[key] = _scalar(meta.get(key))
    return parsed


def _scalar(value) -> str:
    """frontmatter 标量转单行字符串；tools 允许 YAML 列表（顿号连接）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(str(v).strip() for v in value if str(v).strip())
    return re.sub(r"\s+", " ", str(value)).strip()


def _one_line(text: str) -> str:
    """description 允许 YAML 折叠多行，目录里压成一行。"""
    return re.sub(r"\s+", " ", text).strip()


def is_skill_path(path: str) -> bool:
    """只认以 skill.md / SKILL.md 结尾、且不是设计模板资产的路径。"""
    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    if not lower.endswith("skill.md"):
        return False
    return "/assets/" not in lower


def fallback_name_from_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1]
