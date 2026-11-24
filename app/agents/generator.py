"""Agent Config Generator — generates per-agent support files.

Reads from AgentRegistry (directory-based discovery under bot/agents/)
and generates:
1. bot/agents/{name}/CLAUDE.md — agent role definition / documentation
2. bot/agents/{name}/.claude/settings.json — MCP isolation settings

NOTE: cc-connect now has a SINGLE project (dev-bot). Sub-agents (db/deploy/
config/daily) route through the `[role=xxx]` prefix and do NOT get their own
[[projects]] section in config.toml — so TOML generation was removed.

Usage:
    python -m app.agents.generator                   # Generate all
    python -m app.agents.generator db-agent          # Generate specific
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.agents.registry import AgentDef, AgentRegistry

logger = logging.getLogger("agents.generator")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/agents/ -> feishu-agent-platform/
AGENTS_DIR = PROJECT_DIR / "bot" / "agents"


class AgentConfigGenerator:
    """Generates per-agent MCP settings + CLAUDE.md from Registry."""

    def __init__(self, registry: AgentRegistry):
        self._registry = registry

    # ── Settings Generation ──

    @staticmethod
    def _expand_env(value: str) -> str:
        """展开 "${ENV_NAME}" 占位符 — 凭据不入库（agent.yaml 安全红线）。"""
        def _sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, value)

    def generate_settings_json(self, agent: AgentDef) -> dict:
        """Generate .claude/settings.json for MCP isolation（env 占位符展开）。"""
        if not agent.mcp_servers:
            return {}
        servers: dict[str, dict[str, Any]] = {}
        for name, cfg in agent.mcp_servers.items():
            cfg = dict(cfg)
            env_cfg = cfg.get("env")
            if isinstance(env_cfg, dict):
                cfg["env"] = {
                    k: self._expand_env(v) if isinstance(v, str) else v
                    for k, v in env_cfg.items()
                }
            servers[name] = cfg
        return {"mcpServers": servers}

    # ── CLAUDE.md Generation ──

    def generate_claude_md(self, agent: AgentDef) -> str:
        """Generate CLAUDE.md for an agent."""
        mcp_list = "\n".join(
            f"- {name}" for name in agent.mcp_servers
        ) if agent.mcp_servers else "- 无 MCP 工具"

        # 使用单源 prompt（prompt_ref 优先），避免 CLAUDE.md 与 .md 双源漂移
        prompt = agent.prompt()

        return f"""# {agent.display_name}

> {agent.description}

## 职责
{prompt}

## 可用 MCP
{mcp_list}
"""

    # ── File Writing ──

    def write_settings_files(self, agent_names: list[str] | None = None) -> int:
        """Write bot/agents/{name}/.claude/settings.json for each agent."""
        agents = self._resolve_agents(agent_names)
        written = 0

        for agent in agents:
            settings = self.generate_settings_json(agent)
            if not settings:
                continue

            agent_dir = AGENTS_DIR / agent.name
            settings_dir = agent_dir / ".claude"
            settings_dir.mkdir(parents=True, exist_ok=True)

            settings_file = settings_dir / "settings.json"
            try:
                settings_file.write_text(
                    json.dumps(settings, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info(f"Written: {settings_file}")
                written += 1
            except Exception as e:
                logger.error(f"Failed to write {settings_file}: {e}")

        return written

    def write_claude_md_files(self, agent_names: list[str] | None = None) -> int:
        """Write bot/agents/{name}/CLAUDE.md for each agent."""
        agents = self._resolve_agents(agent_names)
        written = 0

        for agent in agents:
            agent_dir = AGENTS_DIR / agent.name
            agent_dir.mkdir(parents=True, exist_ok=True)

            claude_md_file = agent_dir / "CLAUDE.md"
            if not claude_md_file.exists():
                try:
                    claude_md_file.write_text(
                        self.generate_claude_md(agent),
                        encoding="utf-8",
                    )
                    logger.info(f"Written: {claude_md_file}")
                    written += 1
                except Exception as e:
                    logger.error(f"Failed to write {claude_md_file}: {e}")
            else:
                logger.info(f"Skipped (exists): {claude_md_file}")

        return written

    # ── Helpers ──

    def _resolve_agents(self, agent_names: list[str] | None) -> list[AgentDef]:
        """Resolve target agents (all, or explicit names)."""
        if agent_names:
            agents = [self._registry.get(n) for n in agent_names]
            return [a for a in agents if a is not None]
        return self._registry.list()


def main():
    """CLI entry point: python -m app.agents.generator

    Generates CLAUDE.md + .claude/settings.json for each agent.
    """
    import sys

    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")  # 凭据占位符 ${ENV} 展开的取值来源
    logging.basicConfig(level=logging.INFO)

    registry = AgentRegistry()
    registry.load()

    generator = AgentConfigGenerator(registry)

    agent_names = sys.argv[1:] if len(sys.argv) > 1 else None

    settings_written = generator.write_settings_files(agent_names)
    claude_md_written = generator.write_claude_md_files(agent_names)

    print("\n=== Agent Config Generator ===")
    print(f"  Settings files written: {settings_written}")
    print(f"  CLAUDE.md files written: {claude_md_written}")


if __name__ == "__main__":
    main()