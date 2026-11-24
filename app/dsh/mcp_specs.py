"""dsh 会话的 MCP 工具面装配 — 配置 → ACP `mcpServers` 规格。

负责：把 `[projects.mcp]` 的内建 server 与各 `agent.yaml` 自己声明的数据源
汇总成一份无重复的规格表，路径解析成绝对路径。
不负责：拉起进程（supervisor）、协议报文（adapter）。
调用方：`app/main.py` 启动 dsh 执行面、`deploy/dsh/verify_routing.py` 回归。

## 为什么单独一层

这段逻辑原本在 `app/main.py` 里，被 `deploy/dsh/verify_routing.py` 与 MCP 侧
反向 import——「谁需要工具面」和「谁拉进程」本来是两件事，混在主入口里就只能
靠 import 主入口拿到，于是 `app/mcp/` 依赖了 `app/main`（分层表里没有这一条）。
搬到这里之后依赖方向是单向的：入口 → dsh，MCP → dsh。

## 两条来源都要收

`[projects.mcp]` 是本仓自己实现的能力面（飞书 / 业务域 / 能力执行入口）；
各 agent 的 `agent.yaml` 是它自己声明的数据源。前者漏挂
等于能力没有手，后者漏挂等于把数据源摘掉——两种都表现为「模型说没有这个工具」，
不会报错。

失败语义：不抛。配置缺项按「不挂载」处理（能力缺席好过挂一个必然失败的子进程）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.dsh.supervisor import SupervisorConfig

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

__all__ = [
    "DISABLED_AGENT_SERVERS",
    "absolute_path",
    "build_dsh_supervisor_config",
    "dsh_mcp_specs",
]

# 停用能力的 MCP 不进会话：能力已下线时还挂着它的数据源，等于把工具暴露给
# 一个「不该再用」的能力面，模型仍可能绕过路由直接调用。
DISABLED_AGENT_SERVERS: frozenset[str] = frozenset({"db-agent"})

# 本仓内建 server 的挂载顺序与配置字段名（顺序稳定，便于对账）。
_BUILTIN_SERVERS = ("feishu", "domains", "capabilities", "order_agent", "figma")


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_value(value: str) -> str:
    """展开 `${VAR}`（未定义置空，不把字面量传下去）。"""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def absolute_path(path: str) -> Path:
    """相对路径按仓库根解析（dsh 的 session/new 强校验 cwd 必须绝对）。"""
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_DIR / candidate)


def build_dsh_supervisor_config(
    dsh_cfg: Any, mcp_cfg: Any = None, agents: list[Any] | None = None
) -> SupervisorConfig:
    """DshConfig → SupervisorConfig：空路径回落仓库内默认，凭据经 env 传递。

    `dsh_home` / `workspace` 留空表示用默认值（仓库同级 `.dsh-bot` 与仓库根），
    与 `SupervisorConfig` 的默认一致。
    """
    dsh_home = (
        absolute_path(dsh_cfg.dsh_home) if dsh_cfg.dsh_home else SupervisorConfig().dsh_home
    )
    workspace = absolute_path(dsh_cfg.workspace) if dsh_cfg.workspace else PROJECT_DIR
    env: dict[str, str] = {}
    api_key = dsh_cfg.resolved_api_key
    if api_key:
        env["AIGW_API_KEY"] = api_key
    model = (dsh_cfg.provider, dsh_cfg.model) if dsh_cfg.provider and dsh_cfg.model else None
    return SupervisorConfig(
        profile=dsh_cfg.profile or "agentbiz",
        dsh_home=dsh_home,
        workspace=workspace,
        model=model,
        env=env,
        mcp_servers=tuple(dsh_mcp_specs(mcp_cfg, agents)),
    )


def dsh_mcp_specs(mcp_cfg: Any, agents: list[Any] | None = None) -> list[dict[str, Any]]:
    """配置里的 stdio server + 各 agent 声明的数据源 → ACP mcpServers 规格。

    环境变量直接展开成实际值：dsh 只按自己声明的字段拼子进程环境，不继承
    bot 进程的 env，也不做 `${VAR}` 替换——不展开的话，MCP 子进程拿到的会是
    `${EXAMPLE_USERNAME}` 这样的字面量，登录失败却看不出原因。
    `PYTHONPATH` 显式指向仓库根：工具子进程不一定从仓库根启动，靠它自己猜
    `app.*` 的位置会 ImportError，表现为「工具根本没注册上」而不是报错。
    """
    if mcp_cfg is None:
        return []
    specs: list[dict[str, Any]] = []
    for name in _BUILTIN_SERVERS:
        cfg = getattr(mcp_cfg, name, None)
        if cfg is None or not cfg.enabled:
            continue  # 能力缺席好过挂一个必然失败的子进程
        # env 是 ACP 的 name/value 条目数组（不是字典）：传 dict 会被当成空列表，
        # 工具子进程就看不到 PYTHONPATH。配置里的 env 先合并进来，凭据从这里进
        # 子进程而不是命令行（命令行会出现在进程列表与日志里）。
        spec: dict[str, Any] = {
            "name": name,
            "args": list(cfg.args),
            "env": [{"name": "PYTHONPATH", "value": str(PROJECT_DIR)}],
        }
        for key, value in (getattr(cfg, "env", None) or {}).items():
            spec["env"].append({"name": str(key),
                                "value": _expand_value(str(value))})
        if cfg.command:
            spec["command"] = cfg.command
        if cfg.cwd:
            spec["cwd"] = str(absolute_path(cfg.cwd))
            # dsh 不把 spec 的 cwd 传给 stdio 子进程（实测：外部仓的
            # `-m order_agent.mcp_server` 在 bot 仓库根下启动，报 No module named
            # order_agent，session/new 直接 Internal error）。所以把该目录并进
            # 已有的 PYTHONPATH（**同一个名字只出现一次**：dsh 收条目数组但按
            # 名字折叠成 map，同名条目后写的会整条覆盖前面的，两条各自写会把
            # 仓库根挤掉）。cwd 字段保留，给认这个字段的其他客户端用。
            path_entries = [str(PROJECT_DIR), str(absolute_path(cfg.cwd))]
            spec["env"] = [{"name": "PYTHONPATH", "value": os.pathsep.join(path_entries)}]
        specs.append(spec)

    seen = {str(spec["name"]) for spec in specs}
    for agent in agents or []:
        if getattr(agent, "name", "") in DISABLED_AGENT_SERVERS:
            continue
        for name, raw in (getattr(agent, "mcp_servers", None) or {}).items():
            if name in seen or not isinstance(raw, dict):
                continue
            # 同名 server 只挂一次：agent.yaml 里同一个 MCP 在多个 agent 间复用
            # （如三处都写同一个名字），重复挂载会让工具出现两份、模型来回挑。
            seen.add(name)
            spec = dict(raw)
            spec["name"] = name
            specs.append(spec)
    return specs
