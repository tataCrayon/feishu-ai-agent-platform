"""skill_catalog 扫描的子进程纪律：git 必须脱离父进程的 stdin。

守的是一个只在 MCP 子进程里出现的静默超时：catalog 工具跑在 capabilities
MCP server 里，那个进程的 stdin 是 JSON-RPC 管道；git 继承它之后在 Windows 上
起手就卡死（实测 `git --version` 20s 超时，同一命令 DEVNULL 后 0.03s 返回），
工具报 "Request timed out"，用户看到的是「目录服务挂了」。
"""

from __future__ import annotations

import subprocess

from app.domains.skill_catalog.scan import CatalogScanner


def test_git_child_process_does_not_inherit_stdin(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # 替身：原样收下 subprocess.run 的所有参数
        captured.update(kwargs)
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scanner = CatalogScanner(
        repo_url="http://example.invalid/skills.git",
        bare_dir=str(tmp_path),
        skip_branches=[],
    )
    scanner._git(["fetch", "--all", "--prune"], cwd=str(tmp_path), timeout=5)

    assert captured["cmd"][0] == "git"
    assert captured["stdin"] is subprocess.DEVNULL, (
        "git 继承了父进程 stdin：在 MCP stdio server 里会卡死到超时"
    )
