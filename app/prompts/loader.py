"""Prompt Manager — 集中管理所有系统提示词。

从 `bot/prompts/` 目录加载提示词，支持：
- YAML frontmatter 版本元数据解析
- 按名称/分类加载
- 缓存（避免重复 I/O）
- 加载失败时回退到代码内嵌默认值
- PromptMeta / PromptArtifact 结构化数据（含 content_hash）
- 版本别名解析（stable/beta/latest）
- 依赖注入（depends_on 拼接公共基座）
- 主动缓存失效（invalidate）

目录结构：
    bot/prompts/
    ├── INDEX.md
    ├── CHANGELOG.md
    ├── shared/               # 公共基座，可被多个 Agent 引用
    ├── _golden/              # Golden 测试数据集
    ├── router/
    │   ├── system_prompt.md          # 意图路由（单文件，历史版本由 git 承载）
    │   ├── decompose_system_prompt.md
    │   └── direct_answer_system_prompt.md
    ├── agents/
    │   ├── dev-bot.md
    │   ├── db-agent.md
    │   └── ...
    └── replies/
        └── help_reply.md
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger("prompts.loader")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/prompts/ -> feishu-agent-platform/
PROMPTS_DIR = PROJECT_DIR / "bot" / "prompts"


class PromptTemplateError(Exception):
    """Prompt 加载失败时抛出。"""


# ── 结构化数据模型 ────────────────────────────────────────────────


@dataclass
class PromptMeta:
    """Prompt 元数据（从 YAML frontmatter 解析）。"""

    version: str = "0.0.0"
    name: str = ""
    description: str = ""
    author: str = ""
    created: str = ""
    updated: str = ""
    tags: list[str] = field(default_factory=list)
    cache_hint: str = "high-frequency"  # high-frequency / low-frequency / never-cache
    depends_on: list[str] = field(default_factory=list)
    changelog: list[dict] = field(default_factory=list)
    token_estimate: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> PromptMeta:
        """从解析后的 frontmatter 字典构建，忽略未知字段。"""
        valid_keys = {k for k in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**kwargs)


@dataclass
class PromptArtifact:
    """单个 Prompt 的运行时产物。

    Attributes:
        meta: 版本元数据。
        content: 正文（去除 frontmatter，已注入依赖）。
        path: 源文件路径（相对 PROMPTS_DIR）。
        content_hash: 正文的 SHA-256 前 16 位，作为缓存键和评估主键。
    """

    meta: PromptMeta
    content: str
    path: str
    content_hash: str


# ── 加载器 ────────────────────────────────────────────────────────


class PromptLoader:
    """从 bot/prompts/ 加载系统提示词。

    使用 LRU 缓存，支持 content_hash 追踪、版本别名、依赖注入。
    """

    # 版本别名集合
    _aliases: ClassVar[set[str]] = {"stable", "beta", "latest"}

    # ── 公共接口（兼容旧版） ──

    @classmethod
    def load(cls, path: str, fallback: str = "") -> str:
        """按相对路径加载 prompt 文件内容（含 depends_on 依赖注入）。

        与 load_artifact 对齐：frontmatter 声明 depends_on 的 prompt（如
        agents/*.md 依赖 shared/safety-rules）在运行时同样拼接公共基座，
        避免「注入逻辑只在 artifact 路径生效」的单源缺口。

        Args:
            path: 相对 bot/prompts/ 的路径，如 "router/system_prompt.md"。
            fallback: 加载失败时返回的默认内容。

        Returns:
            Prompt 文件正文（去除 frontmatter + 注入依赖），失败返回 fallback。
        """
        file_path = cls._resolve(path)
        body = cls._load_impl(file_path, fallback)

        # depends_on 注入（加载失败/缓存 fallback 时跳过 — 无 frontmatter 可读）
        meta = cls._read_frontmatter(file_path)
        deps = meta.get("depends_on") or []
        if body and body != fallback and deps:
            deps_list = [d for d in deps if isinstance(d, str)]
            if deps_list:
                return cls._inject_dependencies(body, deps_list)
        return body

    @classmethod
    def load_router_system_prompt(cls) -> str:
        """加载路由引擎系统提示词。运行时唯一真源是 router/system_prompt.md。"""
        return cls.load("router/system_prompt.md", fallback=_DEFAULT_ROUTER_PROMPT)

    @classmethod
    def load_router_decompose_prompt(cls) -> str:
        """加载任务分解引擎提示词。运行时唯一真源是 router/decompose_system_prompt.md。"""
        return cls.load("router/decompose_system_prompt.md", fallback=_DEFAULT_DECOMPOSE_PROMPT)

    @classmethod
    def load_agent_prompt(cls, agent_name: str) -> str:
        """加载指定 Agent 的系统提示词。

        Args:
            agent_name: Agent 名称，如 "dev-bot" / "db-agent"。

        Returns:
            Prompt 正文，加载失败时返回空字符串。
        """
        return cls.load(f"agents/{agent_name}.md", fallback="")

    @classmethod
    def load_help_reply(cls) -> str:
        """加载 help_handler 回复文本模板。"""
        return cls.load("replies/help_reply.md", fallback=_DEFAULT_HELP_REPLY)

    @classmethod
    def index(cls) -> dict[str, str]:
        """返回所有可用 prompt 的索引（路径 → 版本）。"""
        result: dict[str, str] = {}
        if not PROMPTS_DIR.exists():
            return result
        for file in sorted(PROMPTS_DIR.rglob("*.md")):
            name = file.name
            # 跳过 INDEX/CHANGELOG、别名文件、golden 目录
            if name in ("INDEX.md", "CHANGELOG.md") or name in cls._aliases:
                continue
            if "_golden" in str(file):
                continue
            meta = cls._read_frontmatter(file)
            result[str(file.relative_to(PROMPTS_DIR))] = meta.get("version", "?")
        return result

    # ── 新接口（Phase A/B） ──

    @classmethod
    def load_artifact(cls, ref: str, version: str = "stable") -> PromptArtifact | None:
        """加载结构化 PromptArtifact。

        支持三种版本引用：
        - 别名：stable / beta / latest（符号链接或自动发现）
        - 显式版本：1.2.0 → v1.2.0.md
        - latest：目录内 semver 最高版本

        Args:
            ref: 相对 bot/prompts/ 的路径（不含文件名），如 "router/system"。
            version: 版本引用，默认 "stable"。

        Returns:
            PromptArtifact，加载失败时返回 None。
        """
        cache_key = f"{ref}@{version}"
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        try:
            file_path = cls._resolve_artifact_path(ref, version)
            if not file_path or not file_path.exists():
                logger.warning("Prompt artifact not found: %s@%s", ref, version)
                return None

            content = file_path.read_text(encoding="utf-8")
            meta_dict = cls._read_frontmatter(file_path)
            meta = PromptMeta.from_dict(meta_dict)
            body = cls._strip_frontmatter(content)

            # 依赖注入
            if meta.depends_on:
                body = cls._inject_dependencies(body, meta.depends_on)

            # 相对路径
            rel_path = str(file_path.relative_to(PROMPTS_DIR))

            artifact = PromptArtifact(
                meta=meta,
                content=body,
                path=rel_path,
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
            )
            cls._cache[cache_key] = artifact
            return artifact
        except Exception as e:
            logger.warning("Failed to load artifact %s@%s: %s", ref, version, e)
            return None

    @classmethod
    def get_prompt_version(cls, path: str) -> str:
        """获取指定 prompt 的版本号。

        Args:
            path: 相对 bot/prompts/ 的路径。

        Returns:
            版本号字符串，如 "1.2.0"，未找到时返回 "?",
        """
        if not path:
            return "?"
        _, meta = cls.load_with_version(path)
        return meta.get("version", "?")

    @classmethod
    def load_with_version(cls, path: str, fallback: str = "") -> tuple[str, dict]:
        """加载 prompt 正文 + 版本元数据。

        Args:
            path: 相对 bot/prompts/ 的路径。
            fallback: 加载失败时返回的默认正文。

        Returns:
            (body, meta) — body 为正文，meta 为 frontmatter 字典（含 version/updated 等）。
            加载失败时 meta 为空字典。
        """
        try:
            file_path = cls._resolve(path)
            if not file_path.exists():
                logger.warning("Prompt file not found: %s", file_path)
                return fallback, {}
            content = file_path.read_text(encoding="utf-8")
            meta = cls._read_frontmatter(file_path)
            body = cls._strip_frontmatter(content)
            return body, meta
        except Exception as e:
            logger.warning("Failed to load prompt with version %s: %s", path, e)
            return fallback, {}

    @classmethod
    def invalidate(cls, ref: str = "", version: str = "") -> int:
        """主动清除缓存条目。

        Args:
            ref: 路径前缀。为空时清除全部缓存。
            version: 版本号。为空时清除该路径下所有版本。

        Returns:
            清除的缓存条目数。
        """
        if not ref and not version:
            count = len(cls._cache)
            cls._cache.clear()
            cls._load_impl.cache_clear()
            logger.info("Cleared all prompt caches (%d entries)", count)
            return count

        keys = list(cls._cache.keys())
        to_remove = []
        for key in keys:
            if version:
                key_ref, key_ver = key.split("@", 1) if "@" in key else (key, "")
                if key_ref == ref and key_ver == version:
                    to_remove.append(key)
            elif ref:
                if key.startswith(ref):
                    to_remove.append(key)

        for key in to_remove:
            cls._cache.pop(key, None)

        if to_remove:
            logger.info("Invalidated %d cache entries for %s@%s", len(to_remove), ref, version)
        return len(to_remove)

    # ── 内部实现 ──

    _cache: ClassVar[dict[str, PromptArtifact]] = {}

    @classmethod
    @lru_cache(maxsize=128)
    def _load_impl(cls, file_path: Path, fallback: str) -> str:
        """实际读取文件内容，带缓存。"""
        if not file_path.exists():
            logger.warning("Prompt file not found: %s — using fallback", file_path)
            return fallback

        try:
            content = file_path.read_text(encoding="utf-8")
            return cls._strip_frontmatter(content)
        except Exception as e:
            logger.warning("Failed to read prompt %s: %s — using fallback", file_path, e)
            return fallback

    @classmethod
    def _resolve_artifact_path(cls, ref: str, version: str) -> Path | None:
        """解析版本别名到实际文件路径。

        支持两种组织方式：
        1. 目录 + 版本文件：ref 指向目录（保留能力；当前仓内 prompt 已全部单文件化，无在用目录）
        2. 单文件：ref 直接指向文件（`shared/safety-rules` → `shared/safety-rules.md`）
        """
        base = PROMPTS_DIR / ref

        # 情形 2：单文件模式（ref 直接指向 .md 文件）
        if base.suffix == ".md" and base.exists():
            return base
        single_file = base.parent / f"{base.name}.md"
        if single_file.exists():
            return single_file

        # 情形 1：目录 + 版本文件
        if not base.is_dir():
            return None

        if version in cls._aliases:
            if version == "latest":
                return cls._find_latest(base)
            # 别名 → 符号链接或直接文件
            alias_file = base / f"{version}.md"
            if alias_file.exists():
                return alias_file.resolve()
            # 降级：查找稳定链接
            stable_file = base / "stable.md"
            if stable_file.exists():
                return stable_file.resolve()
            return None

        # 显式版本号
        vfile = base / f"v{version}.md"
        if vfile.exists():
            return vfile

        # 降级：直接文件名
        direct = base / f"{version}.md"
        if direct.exists():
            return direct

        return None

    @classmethod
    def _find_latest(cls, base: Path) -> Path | None:
        """在目录内查找 semver 最高版本的文件。"""
        if not base.exists():
            return None
        pat = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.md$")
        versions = []
        for f in base.iterdir():
            m = pat.match(f.name)
            if m:
                versions.append(((int(m[1]), int(m[2]), int(m[3])), f))
        if not versions:
            return None
        return max(versions)[1]

    @classmethod
    def _inject_dependencies(cls, body: str, depends_on: list[str]) -> str:
        """注入 depends_on 声明的公共基座 Prompt。

        依赖条目支持版本 pin：`shared/output-format@1.0.0`（agent-dev-standards
        的 prompt-management 规范）。pin 不匹配实际文件版本时告警——单文件模式下
        没有可切换的旧版本可加载，但 pin 至少让漂移在日志里可见，而不是静默。
        依赖按顺序拼接在正文末尾，用 <!-- depends_on: {ref} --> 注释标记。
        """
        sections = [body]
        for dep in depends_on:
            ref, sep, pin = dep.partition("@")
            dep_artifact = cls.load_artifact(ref, version=pin if sep else "stable")
            if not dep_artifact:
                logger.warning("Dependency not found: %s", dep)
                continue
            if sep and pin and dep_artifact.meta.version != pin:
                logger.warning(
                    "Dependency version pin drift: %s declared @%s but file is %s — "
                    "bump the pin or hold the dependency back",
                    ref, pin, dep_artifact.meta.version,
                )
            sections.append(f"\n<!-- depends_on: {ref} -->\n{dep_artifact.content}")
        return "\n".join(sections)

    @staticmethod
    def _resolve(rel_path: str) -> Path:
        """解析相对路径，保证安全（不允许越界访问）。"""
        base = PROMPTS_DIR.resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            raise PromptTemplateError(f"Prompt path escapes prompts dir: {rel_path}")
        return target

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """去掉 YAML frontmatter（--- 包裹的头部）。"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) == 3:
                return parts[2].strip()
        return content.strip()

    @staticmethod
    def _read_frontmatter(file_path: Path) -> dict:
        """读取 YAML frontmatter 元数据（name/version/changes 等）。"""
        try:
            content = file_path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return {}
            parts = content.split("---", 2)
            if len(parts) < 3:
                return {}
            import yaml
            return yaml.safe_load(parts[1]) or {}
        except Exception:
            return {}


# ── 代码内嵌默认值（fallback，加载失败时使用） ──────────────────

_DEFAULT_ROUTER_PROMPT = (
    "你是飞书机器人的意图识别与路由引擎。\n"
    "分析用户消息，调用与之对应的路由工具。工具表以本次请求注入的为准，不要发明工具名。\n"
    "不确定时用 direct_answer，不要把请求塞给某个综合业务 Agent。\n"
    "简单问候直接用 direct_answer。"
)

_DEFAULT_DECOMPOSE_PROMPT = (
    "你是任务分解引擎。将复杂请求拆解为 JSON 格式子任务列表：\n"
    '{"tasks": [{"agent": "子Agent名", "query": "查询", "depends_on": -1}]}\n'
    "如果请求不复杂，返回 {\"tasks\": []}。最多 5 个子任务。\n"
    "agent 名只能来自本次注入的清单，不要使用已下线的命令式入口。"
)

_DEFAULT_HELP_REPLY = (
    "{greeting}！我是中台核心组数字助手。\n"
    "能力清单以 /help 为准（由当前注册表生成）。\n"
    "这份回复是帮助文案加载失败时的兜底，不列举命令，避免把已下线的入口说成还能用。"
)


# ── 便捷函数 ──


def load_prompt(path: str, fallback: str = "") -> str:
    """便捷函数：加载单个 prompt。"""
    return PromptLoader.load(path, fallback=fallback)