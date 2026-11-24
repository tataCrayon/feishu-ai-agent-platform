"""MCP 规格构造的契约测试 — agent.yaml 的声明要变成 ACP 能直接吃的形态。

守的是四个曾经踩空的地方：env 必须是条目数组而不是字典、`${VAR}` 要展开、
npm 提供的命令在 Windows 上要换成 node 入口、同名 server 只挂一次。
这些错了都不会报错，只会表现为「模型说没有这个工具」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.dsh.supervisor import (
    DshSupervisor,
    SupervisorConfig,
    _expand_env,
    _resolve_command,
)


def _supervisor(*servers: dict) -> DshSupervisor:
    return DshSupervisor(SupervisorConfig(mcp_servers=tuple(servers)))


def test_env_expands_placeholder_from_process_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_USER", "alice")
    entries = _expand_env({"NACOS_USERNAME": "${TEST_MCP_USER}", "NACOS_HOST": "h:8848"})
    assert {e["name"]: e["value"] for e in entries} == {
        "NACOS_USERNAME": "alice",
        "NACOS_HOST": "h:8848",
    }


def test_env_missing_placeholder_becomes_empty(monkeypatch) -> None:
    monkeypatch.delenv("TEST_MCP_MISSING", raising=False)
    entries = _expand_env({"PASSWORD": "${TEST_MCP_MISSING}"})
    # 留字面量 `${...}` 会被当密码发出去，失败信息更难读
    assert entries == [{"name": "PASSWORD", "value": ""}]


def test_empty_command_uses_this_interpreter() -> None:
    command, prefix = _resolve_command("")
    assert command == sys.executable
    assert prefix == []


def test_npx_resolves_to_node_entrypoint() -> None:
    command, prefix = _resolve_command("npx")
    if command == sys.executable:  # 机器上没有 node 时退化成原样返回
        return
    assert Path(command).name.lower().startswith("node")
    assert prefix and prefix[0].endswith("npx-cli.js")


def test_spec_shape_matches_acp_contract() -> None:
    sup = _supervisor(
        {"name": "domains", "args": ["-m", "app.mcp.domains_server"],
         "env": {"PYTHONPATH": "/repo"}},
        {"name": "mysql", "command": "npx", "args": ["-y", "easy-mysql-mcp"],
         "env": {"MYSQL_HOST": "h"}},
    )
    for spec in sup.mcp_server_specs():
        assert isinstance(spec["env"], list)
        assert all(set(e) == {"name", "value"} for e in spec["env"])
        assert Path(spec["command"]).is_absolute()


def test_specs_keep_agent_order_and_allow_filtering() -> None:
    sup = _supervisor(
        {"name": "a", "env": {}},
        {"name": "b", "env": {}},
    )
    assert [s["name"] for s in sup.mcp_server_specs()] == ["a", "b"]
    assert [s["name"] for s in sup.mcp_server_specs("b")] == ["b"]


def test_npm_style_command_keeps_original_args_after_prefix() -> None:
    sup = _supervisor({"name": "n", "command": "npx", "args": ["-y", "pkg"], "env": {}})
    args = sup.mcp_server_specs()[0]["args"]
    assert args[0].endswith("npx-cli.js")
    assert args[1:] == ["-y", "pkg"]


def test_expand_env_ignores_non_dict() -> None:
    assert _expand_env(None) == []
    assert _expand_env(["x"]) == []


def test_pythonpath_survives_expansion() -> None:
    spec = _supervisor({"name": "p", "env": {"PYTHONPATH": os.getcwd()}}).mcp_server_specs()[0]
    assert {e["name"]: e["value"] for e in spec["env"]}["PYTHONPATH"] == os.getcwd()


def test_cwd_is_mirrored_into_pythonpath() -> None:
    """cwd 不决定 `-m` 找模块的路径，dsh 也不把 cwd 透传给子进程。

    实证（2026-11-06）：外部仓 `-m order_agent.mcp_server` + cwd 指向该仓，
    子进程实际仍在 bot 仓库根下启动，报 No module named order_agent，
    session/new 直接 Internal error。只有 cwd 同时进 PYTHONPATH 才起得来。
    """
    cwd = str(Path(os.getcwd()) / "peer-repo")
    spec = _supervisor(
        {"name": "peer", "args": ["-m", "order_agent.mcp_server"], "cwd": cwd},
    ).mcp_server_specs()[0]
    values = [e["value"] for e in spec["env"] if e["name"] == "PYTHONPATH"]
    assert len(values) == 1                      # 同名只出现一条：dsh 折叠成 map
    assert cwd in values[0].split(os.pathsep)


def test_cwd_merge_keeps_existing_pythonpath_entries() -> None:
    """已有 PYTHONPATH（仓库根）不能被 cwd 挤掉——同一 map 键写两条会互相覆盖。"""
    cwd = str(Path(os.getcwd()) / "peer-repo")
    spec = _supervisor(
        {"name": "p", "cwd": cwd, "env": {"PYTHONPATH": "/repo"}},
    ).mcp_server_specs()[0]
    values = [e["value"] for e in spec["env"] if e["name"] == "PYTHONPATH"]
    assert len(values) == 1
    parts = values[0].split(os.pathsep)
    assert "/repo" in parts and cwd in parts


def test_no_cwd_leaves_pythonpath_untouched() -> None:
    """本仓模块形态的 server（cwd 为空）不该被改动。"""
    spec = _supervisor(
        {"name": "domains", "args": ["-m", "app.mcp.domains_server"],
         "env": {"PYTHONPATH": "/repo"}},
    ).mcp_server_specs()[0]
    assert {e["name"]: e["value"] for e in spec["env"]} == {"PYTHONPATH": "/repo"}
