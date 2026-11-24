"""MCP 工具面 — 三类能力以 stdio MCP server 暴露给 dsh。

负责：工具定义、参数校验（含飞书三红线硬拒）、调用下层实现、JSON 回执整形。
不负责：业务逻辑（`app/domains/`、`app/protocols/` 提供）与路由/提示词（dsh 侧）。
调用方：dsh 按 `config.toml [projects.mcp]` 的命令以子进程方式挂载。
"""

from __future__ import annotations

SERVER_MODULES: tuple[str, ...] = (
    "app.mcp.feishu_server",
    "app.mcp.domains_server",
    "app.mcp.order_agent_server",
)
"""可挂载的 server 模块名（启动命令见 `[projects.mcp]`，此处只做清单，不 import）。"""

__all__ = ["SERVER_MODULES"]
