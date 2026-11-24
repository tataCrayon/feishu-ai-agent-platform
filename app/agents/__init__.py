"""Agent Registry module — centralized agent definitions and dynamic dispatch.

Provides:
- AgentDef: dataclass for a single agent definition
- AgentRegistry: loads agent definitions from bot/agents/*/agent.yaml
- AgentConfigGenerator: auto-generates CLAUDE.md, MCP settings
"""
from app.agents.generator import AgentConfigGenerator
from app.agents.registry import AgentDef, AgentRegistry

__all__ = [
    "AgentConfigGenerator",
    "AgentDef",
    "AgentRegistry",
]