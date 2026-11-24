"""PM 工作台 Handler 的注册壳 — 实现在 `app/domains/pm/pm_hub_handler.py`。

为什么要一个只有几行的转发文件：`app/agents/handlers/discovery.py` 只扫**本目录**的
`*_handler.py`，而 PM 的 41 个模块按架构分层放在 `app/domains/pm/`（与
`prd_watch` / `cmdb_watch` 同级）。让 discovery 扫到它的唯一方式是这里有一个
同名壳，而不是把整个能力包倒进 handlers 目录。

装配规则与其它 Handler 一致：`build(config, ctx)` 由 discovery 调用；
未配置 `[projects.pm]` 时返回 None，注册表里就没有这项能力。
"""

from __future__ import annotations

from app.domains.pm.pm_hub_handler import PMHubAgentHandler, build

__all__ = ["PMHubAgentHandler", "build"]
