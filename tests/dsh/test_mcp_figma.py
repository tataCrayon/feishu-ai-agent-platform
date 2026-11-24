"""figma MCP 装配单测：凭据只能走 env、`${VAR}` 要展开、开关默认语义。

为什么钉这三条：Figma MCP 的 token 若落到 args 就会出现在进程列表与日志里；
`${VAR}` 不展开会被当成字面量传给子进程（表现为「服务起不来」而不是报错）。
"""

from __future__ import annotations

from app.config import DshConfig, MCPConfig
from app.dsh.mcp_specs import build_dsh_supervisor_config


def _figma_spec(monkeypatch, *, enabled: bool = True) -> dict:
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test_token")
    cfg = MCPConfig()
    cfg.figma.enabled = enabled
    cfg.figma.command = "/usr/bin/node"
    cfg.figma.args = ["/pkg/bin.js", "--stdio"]
    cfg.figma.env = {"FIGMA_API_KEY": "${FIGMA_API_KEY}"}
    sup = build_dsh_supervisor_config(DshConfig(), cfg, [])
    specs = [s for s in sup.mcp_servers if s["name"] == "figma"]
    assert len(specs) == 1
    return specs[0]


def test_figma_env_is_expanded_into_entries(monkeypatch) -> None:
    spec = _figma_spec(monkeypatch)
    env = {e["name"]: e["value"] for e in spec["env"]}
    assert env["FIGMA_API_KEY"] == "figd_test_token"     # ${VAR} 已展开
    assert env["PYTHONPATH"]                            # 内建条目没被挤掉


def test_token_never_appears_in_args(monkeypatch) -> None:
    spec = _figma_spec(monkeypatch)
    assert all("figd_" not in str(a) for a in spec["args"]), \
        "token 不能进命令行（会出现在进程列表与日志里）"


def test_undefined_env_var_expands_to_empty(monkeypatch) -> None:
    """未定义的变量置空，不把 `${X}` 字面量传给子进程。"""
    monkeypatch.delenv("FIGMA_API_KEY", raising=False)
    cfg = MCPConfig()
    cfg.figma.enabled = True
    cfg.figma.env = {"FIGMA_API_KEY": "${FIGMA_API_KEY}"}
    sup = build_dsh_supervisor_config(DshConfig(), cfg, [])
    spec = next(s for s in sup.mcp_servers if s["name"] == "figma")
    env = {e["name"]: e["value"] for e in spec["env"]}
    assert env["FIGMA_API_KEY"] == ""


def test_disabled_figma_is_not_mounted(monkeypatch) -> None:
    cfg = MCPConfig()
    cfg.figma.enabled = False
    sup = build_dsh_supervisor_config(DshConfig(), cfg, [])
    assert all(s["name"] != "figma" for s in sup.mcp_servers)
