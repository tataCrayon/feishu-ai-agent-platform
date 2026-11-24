"""Agent Registry — centralized agent definitions.

Each agent lives in its own directory as a self-contained config file:

    bot/agents/{name}/agent.yaml        # Agent definition (canonical)
    bot/agents/{name}/CLAUDE.md         # Role definition (generated)
    bot/agents/{name}/.claude/settings.json  # MCP isolation (generated)

Adding a new agent means creating one directory + one agent.yaml —
everything else (config generation, routing, dispatch) is automatic.

Usage:
    registry = AgentRegistry()
    registry.load()                     # Scan bot/agents/*/agent.yaml
    agents = registry.enabled()         # Only enabled agents
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.prompts.loader import PromptLoader

logger = logging.getLogger("agents.registry")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/agents/ -> feishu-agent-platform/
AGENTS_DIR = PROJECT_DIR / "bot" / "agents"
AGENT_CONFIG_NAME = "agent.yaml"

# 合法的执行通道类型（transport）
VALID_TRANSPORTS = ("handler", "cc_connect")


@dataclass
class AgentDef:
    """Single agent definition — the canonical data model for an agent.

    NOTE: Intent recognition is handled by the LLM-based RouterLLM, not by
    pattern matching. This class is a pure data model — no routing logic.
    """

    # Identity
    name: str  # e.g. "db-agent"
    display_name: str  # e.g. "数据库查询 Agent"
    description: str  # One-liner

    # Permission
    permission_level: int = 0  # 0=guest, 1=authorized, 2=admin

    # Model tier
    model: str = "sonnet"  # "haiku" | "sonnet" | "opus"

    # Runtime
    enabled: bool = True
    timeout_sec: int = 60

    # 执行通道：handler=进程内 Python Handler；cc_connect=Claude Code 桥接。
    # 两者平级，cc-connect 不再是全局默认主流程。
    transport: str = "cc_connect"

    # 专属领域声明：编排层在多意图分派时抑制其并行 extra_calls —— 专属域的
    # extra 是同一意图的冗余分派，cc-connect 会话繁忙时会拖满超时导致空返回。
    exclusive_domain: bool = False

    # 是否参与 LLM 路由：False = 不生成路由工具（路由 LLM 不可见、不可达），
    # 但 registry/enabled()/健康探测/help 之外的执行分派（如 /dev 直达）不受影响。
    # 典型：dev-bot 是维护者的远程开发通道，不对团队用户开放路由。
    routable: bool = True

    # 非空表示由 SKILL.md 生成的合成 Agent（name 形如 skill-{slug}），
    # 分发时以 [use skill: {skill_name}] 前缀触发 [CC] 原生技能加载。
    skill_name: str = ""

    # 能力版本号（单源：SKILL.md frontmatter / agent.yaml 的 version 字段），
    # 供 /help 能力清单与「xx 的版本是？」类问句回答；未声明留空。
    version: str = ""

    # cc-connect 项目名；空串 = 使用 config 的 default_project（当前单项目架构）。
    cc_project: str = ""

    # 路由工具的额外参数 schema（由各 agent.yaml 的 route_params 声明），
    # 由 build_router_tools 合入 function-calling properties。
    route_params: dict[str, Any] = field(default_factory=dict)

    # Filesystem
    work_dir: str = ""  # Path relative to project, or absolute

    # Prompt reference: bot/prompts/agents/{name}.md (canonical single source).
    # When set, system_prompt is loaded from this file; agent.yaml must NOT
    # duplicate prompt content (avoids dual-source drift).
    prompt_ref: str = ""

    # System prompt (deprecated inline copy — kept for backward compat; prefer prompt_ref)
    system_prompt: str = ""

    # MCP servers (isolated per agent)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def prompt(self) -> str:
        """Resolve the agent's system prompt from a single source of truth.

        Precedence:
        1. `prompt_ref` → load bot/prompts/agents/{name}.md via PromptLoader
           (canonical; kept in sync with the independent-Handler prompt file).
        2. Inline `system_prompt` (legacy) — logged as a drift risk if set.

        Returns:
            The resolved prompt text (empty string if unresolvable).
        """
        if self.prompt_ref:
            return PromptLoader.load_agent_prompt(self.name)
        return self.system_prompt


class AgentRegistry:
    """Centralized agent registry.

    Loads agent definitions from YAML and provides lookup and enumeration.
    Intent recognition is handled by RouterLLM (LLM-based), not by this class.

    运行时能力开关：agent_switches.json（bot/todos/）里显式 false 的 Agent
    通过 apply_switches 关闭路由（routable() 联合判定），与 agent.yaml 的
    routable 值正交——开关只做「强制下线」，不能把 yaml 里 routable=false
    的 Agent 强行上线。开关由控制台 API 动态写文件并热生效。
    """

    def __init__(self):
        self._agents: dict[str, AgentDef] = {}
        self._loaded = False
        self._switch_off: set[str] = set()

    def load(self) -> list[AgentDef]:
        """Load all agent definitions from individual agent.yaml files.

        Scans bot/agents/{name}/agent.yaml for each agent.

        构建全新 dict 后一次替换 self._agents（原子 swap）：热重载期间并发
        读到的是旧表或新表，不会出现清空后的半表窗口。

        Returns:
            List of all loaded AgentDef objects.
        """
        agents: dict[str, AgentDef] = {}

        # ── Directory-based agent discovery ──
        discovered = 0
        if not AGENTS_DIR.exists():
            logger.warning(f"Agent directory not found: {AGENTS_DIR}")
            return list(agents.values())

        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            config_file = agent_dir / AGENT_CONFIG_NAME
            if not config_file.exists():
                continue
            try:
                with open(config_file, encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                if not raw or not raw.get("name"):
                    logger.warning(f"Skipping invalid config: {config_file}")
                    continue
                agent = AgentDef(
                    name=raw["name"],
                    display_name=raw.get("display_name", raw["name"]),
                    description=raw.get("description", ""),
                    permission_level=raw.get("permission_level", 0),
                    model=raw.get("model", "sonnet"),
                    enabled=raw.get("enabled", True),
                    timeout_sec=raw.get("timeout_sec", 60),
                    transport=raw.get("transport", "cc_connect"),
                    exclusive_domain=raw.get("exclusive_domain", False),
                    routable=bool(raw.get("routable", True)),
                    skill_name=raw.get("skill_name", ""),
                    version=str(raw.get("version", "") or "").strip(),
                    cc_project=raw.get("cc_project", ""),
                    route_params=raw.get("route_params", {}),
                    work_dir=raw.get("work_dir", ""),
                    prompt_ref=raw.get("prompt_ref", ""),
                    system_prompt=raw.get("system_prompt", ""),
                    mcp_servers=raw.get("mcp_servers", {}),
                )
                if agent.transport not in VALID_TRANSPORTS:
                    logger.warning(
                        f"Agent '{agent.name}' has invalid transport "
                        f"'{agent.transport}', fallback to cc_connect"
                    )
                    agent.transport = "cc_connect"
                agents[agent.name] = agent
                discovered += 1
            except Exception as e:
                logger.warning(f"Failed to load {config_file}: {e}")

        if discovered == 0:
            logger.warning(f"No agents found in {AGENTS_DIR}")

        # 原子 swap：热重载期间并发读（list/get/enabled）只会看到旧表或新表
        self._agents = agents
        self._loaded = True
        enabled_count = sum(1 for a in self._agents.values() if a.enabled)
        logger.info(
            f"Loaded {discovered} agents ({enabled_count} enabled) "
            f"from {AGENTS_DIR}/*/{AGENT_CONFIG_NAME}"
        )
        return list(self._agents.values())

    def get(self, name: str) -> AgentDef | None:
        """Get an agent by name."""
        if not self._loaded:
            self.load()
        return self._agents.get(name)

    def register(self, agent: AgentDef) -> None:
        """注册一个运行时合成的 AgentDef（如 SKILL.md 生成的合成 Agent）。

        与 load() 的目录发现互补：load() 会清空重扫，register 只在启动期
        注入动态定义，同名时覆盖（后注册者优先）。
        """
        self._agents[agent.name] = agent
        logger.info(f"[registry] registered synthetic agent: {agent.name} "
                    f"(transport={agent.transport}, skill={agent.skill_name or '-'})")

    def list(self) -> list[AgentDef]:
        """List all registered agents."""
        if not self._loaded:
            self.load()
        return list(self._agents.values())

    def enabled(self) -> list[AgentDef]:
        """List only enabled agents."""
        return [a for a in self.list() if a.enabled]

    def routable(self) -> list[AgentDef]:
        """List enabled agents that participate in LLM routing.

        routable=False 的 Agent（如 dev-bot）不生成路由工具——路由 LLM
        看不到也到不了；但 enabled()/健康探测/help 之外的直通分派（/dev）
        不受影响。运行时开关（apply_switches）关闭的 Agent 同样排除。
        """
        agents: list[AgentDef] = self.enabled()
        return [
            a for a in agents
            if a.routable and a.name not in self._switch_off
        ]

    # ── 运行时能力开关（控制台动态控制） ──────────────────────────

    def apply_switches(self, switch_off: set[str]) -> None:
        """应用运行时开关（强制下线名单），调用方负责随后触发路由重建。

        开关是「强制 off」语义：名单内 Agent 的 routable() 判定为 False，
        不影响 yaml 的 routable/enabled 值，也不影响健康探测。
        """
        self._switch_off = set(switch_off)
        logger.info(
            "[registry] agent switches applied: %d disabled %s",
            len(self._switch_off), sorted(self._switch_off) or "[]",
        )

    @property
    def switch_off(self) -> set[str]:
        """当前被运行时开关强制下线的 Agent 名单（只读副本）。"""
        return set(self._switch_off)

    def reload(self) -> list[AgentDef]:
        """Reload from YAML (for hot-reload scenarios).

        Returns:
            Full agent list after reload（与 load() 语义一致）。
        """
        self._loaded = False
        self.load()
        return list(self._agents.values())