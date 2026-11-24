"""架构护栏 — 把分层约定里能机器判定的几条钉成断言。

`docs/conventions.md` 写了依赖方向，但写下来的规矩不会自己生效：一次「先跑通
再说」的反向 import 就能让 `domains/` 依赖入口、让单测被迫拉起整个 bot。
这里只查**能静态判定**的四条（靠人读的特例仍靠 review）：

1. `app/mcp/` 不得 import `app.main` — 工具面是会话级子进程，不该被主入口拖起来。
2. `app/domains/` 不得 import `app.gateway/` / `app.dsh/` — 业务域不能反向依赖接入层。
3. ACP 协议细节只在 `app/dsh/` 里出现——协议升级时的改动点必须可枚举。
4. 仓库根要有 pytest 引导：`pytest.ini` + `conftest.py`（否则 `tmp_path` 不可用、
   独立脚本 `import app.*` 失败，这两条都是实测踩过的）。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "app"


def _imported_modules(path: Path) -> set[str]:
    """文件里所有 import 的模块名（含函数内延迟导入——反向依赖常藏在里面）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_mcp_does_not_import_main_entry() -> None:
    offenders = sorted(
        str(p.relative_to(PROJECT_ROOT))
        for p in _py_files(APP / "mcp")
        if any(m == "app.main" or m.startswith("app.main.") for m in _imported_modules(p))
    )
    assert offenders == [], (
        "app/mcp 依赖了主入口（反向边）。需要的运行时对象应由启动方注入，"
        f"见 docs/conventions.md L1：{offenders}"
    )


def test_domains_do_not_import_access_layers() -> None:
    forbidden = ("app.gateway", "app.dsh")
    offenders: list[str] = []
    for path in _py_files(APP / "domains"):
        hits = [m for m in _imported_modules(path) if m.startswith(forbidden)]
        if hits:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {hits}")
    assert offenders == [], (
        "业务域反向依赖了接入/执行层（domains 必须能脱离飞书与 dsh 单测）："
        f"{offenders}"
    )


def test_acp_protocol_details_confined_to_dsh_package() -> None:
    """`session/new`、`session/prompt` 这类报文文本只允许在 app/dsh/ 出现。"""
    marks = ('"session/new"', '"session/prompt"', "'session/new'", "'session/prompt'")
    offenders = [
        str(p.relative_to(PROJECT_ROOT))
        for p in _py_files(APP)
        if not p.is_relative_to(APP / "dsh")
        and any(mark in p.read_text(encoding="utf-8") for mark in marks)
    ]
    assert offenders == [], f"ACP 报文细节外泄到 dsh 包之外：{offenders}"


def test_repo_has_pytest_bootstrap() -> None:
    """`tmp_path` 在本仓 Windows 下默认不可写，必须有 ini 兜底；引导脚本同理。"""
    assert (PROJECT_ROOT / "pytest.ini").is_file(), (
        "缺 pytest.ini：默认 basetemp 指向 %TEMP% 时 tmp_path 会 WinError 5"
    )
    assert (PROJECT_ROOT / "conftest.py").is_file(), (
        "缺 conftest.py：独立运行测试时 import app.* 会失败"
    )
    ini = (PROJECT_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "--basetemp" in ini, "pytest.ini 未指定 --basetemp，tmp_path 会退回不可写的 %TEMP%"


def test_tests_live_under_tests_dir_not_in_app() -> None:
    """单测不留在 `app/` 里 — 代码目录只放运行时代码。

    同目录单测看着「就近可读」，代价是四样：打包/ship 时带进去、mypy 统计被测试
    噪音拉高、`app/` 下任何 glob（含 handler 自动发现）都要额外排除、以及
    「这个模块有没有被测」变成靠翻目录判断。
    `tests/` 内的目录结构与 `app/` 镜像（`tests/domains/prd_watch/...`），
    所以「源文件在哪」到「它的测试在哪」仍然是可推导的。
    """
    strays = sorted(str(p.relative_to(PROJECT_ROOT)) for p in APP.rglob("test_*.py"))
    assert strays == [], f"测试文件应放 tests/（按 app/ 结构镜像）：{strays}"

