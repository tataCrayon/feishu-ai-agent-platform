"""SKILL.md 快速接入 — 标准 Claude Code Skill 一键注册为路由能力。

接入方式（丢一个目录即上线，重启生效）：

    bot/skills/{skill-name}/SKILL.md

SKILL.md 采用标准 Claude Code frontmatter 格式（name/description 必填），
feishu-agent-platform 扩展两个可选字段：permission_level / enabled。

启动期做两件事：
1. 解析 frontmatter → 合成 AgentDef 注入 AgentRegistry —— 自动获得
   路由工具（route_to_skill_xxx）、LangGraph 节点、/help 展示、decompose 参与
2. 复制同步 bot/skills/* → .claude/skills/* —— cc-connect 拉起的
   Claude Code 进程（work_dir=项目根）原生消费 SKILL.md 渐进加载

执行路径：transport=cc_connect + [use skill: {name}] 前缀触发技能加载。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.agents.registry import AgentDef, AgentRegistry

logger = logging.getLogger("agents.skills")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/agents/ -> feishu-agent-platform/
SKILLS_DIR = PROJECT_DIR / "bot" / "skills"
CLAUDE_SKILLS_DIR = PROJECT_DIR / ".claude" / "skills"

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass
class SkillDef:
    """一个 SKILL.md 技能包的解析结果。"""

    name: str  # frontmatter name（缺省取目录名），slug 归一后形如 nacos-guard
    display_name: str
    description: str
    version: str = ""  # frontmatter version（可选），help/版本问句展示用
    permission_level: int = 0
    enabled: bool = True
    source_path: Path = field(default_factory=Path)


def _slugify(name: str) -> str:
    """归一为 [a-z0-9_-]，非法字符替换为 -。"""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def parse_skill_md(path: Path) -> SkillDef | None:
    """解析单个 SKILL.md 的 frontmatter。

    name 缺省取父目录名；description 缺失视为无效（打 warning 并跳过），
    避免空描述的路由工具污染 RouterLLM 决策。
    """
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("[skills] cannot read %s: %s", path, e)
        return None

    if not content.startswith("---"):
        logger.warning("[skills] %s has no frontmatter — skipped", path)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        logger.warning("[skills] %s malformed frontmatter — skipped", path)
        return None

    meta = yaml.safe_load(parts[1]) or {}
    name = _slugify(str(meta.get("name") or path.parent.name))
    description = str(meta.get("description", "")).strip()
    if not name or not description:
        logger.warning(
            "[skills] %s missing required name/description — skipped", path
        )
        return None

    return SkillDef(
        name=name,
        display_name=str(meta.get("display_name") or name),
        description=description,
        version=str(meta.get("version", "") or "").strip(),
        permission_level=int(meta.get("permission_level", 0)),
        enabled=bool(meta.get("enabled", True)),
        source_path=path,
    )


def load_skills(skills_dir: Path = SKILLS_DIR) -> list[SkillDef]:
    """扫描 skills 目录下 */SKILL.md，返回解析成功的清单。"""
    if not skills_dir.is_dir():
        return []
    skills = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        skill = parse_skill_md(skill_md)
        if skill:
            skills.append(skill)
    logger.info("[skills] loaded %d skill(s) from %s", len(skills), skills_dir)
    return skills


def inject_into_registry(skills: list[SkillDef], registry: AgentRegistry) -> int:
    """Skill → 合成 AgentDef 注入 registry（自动获得路由工具/节点/help）。"""
    injected = 0
    for s in skills:
        if not s.enabled:
            logger.info("[skills] %s disabled — skipped", s.name)
            continue
        registry.register(AgentDef(
            name=f"skill-{s.name}",
            display_name=f"{s.display_name} 技能",
            description=s.description,
            permission_level=s.permission_level,
            transport="cc_connect",
            skill_name=s.name,
            version=s.version,
        ))
        injected += 1
    return injected


def sync_skills_to_claude(
    src_dir: Path = SKILLS_DIR, dst_dir: Path = CLAUDE_SKILLS_DIR
) -> int:
    """复制同步 skills 到 .claude/skills/（Claude Code 原生可见路径）。

    copytree(dirs_exist_ok=True) 幂等；.claude/skills 是生成物目录，
    源头只在 bot/skills/（人工编辑请改源目录）。
    """
    if not src_dir.is_dir():
        return 0
    copied = 0
    for skill_dir in sorted(src_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith(("_", ".")):
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, dst_dir / skill_dir.name, dirs_exist_ok=True)
        copied += 1
    if copied:
        logger.info("[skills] synced %d skill dir(s) → %s", copied, dst_dir)
    return copied


def setup_skills(registry: AgentRegistry) -> int:
    """启动期入口：加载 + 注入 + 同步（main.py 在 orchestrator 创建前调用一次）。"""
    try:
        skills = load_skills()
        count = inject_into_registry(skills, registry)
        sync_skills_to_claude()
        return count
    except Exception as e:  # skill 链路故障不阻断主流程启动
        logger.exception("[skills] setup failed (bot starts without skills): %s", e)
        return 0
