"""dsh 内核升级 — 改版本、重装、校验补丁仍能合并，再交给路由回归。

为什么需要这一层：dsh 还处在预发布节奏（实测每周 1-2 个版本），而升级真正会坏
的只有三处，全都不会以「报错」的形式出现：

1. `cordis.patch.yml` 引用的插件 id / 配置 schema 变了 → dsh 只在 stderr 打一行
   `N entries did not activate`，进程照常起来，能力静默缺失；
2. `dsh-base` 默认开启的插件集变了 → 个人配置重新进会话（隔离被悄悄打开）；
3. ACP 报文形状变了 → 会话能建，但工具挂不上或图片发不进去。

所以升级不是 `npm install` 就完事，必须**逐条比对合成后的配置**。本脚本把能机器
判定的三条做成断言，剩下的（行为）交给 `verify_routing.py` 跑真实 golden。

用法：
    python deploy/dsh/upgrade.py --check                 # 只看有没有新版本
    python deploy/dsh/upgrade.py --to 0.2.2-rc.1         # 升级到指定版本并校验
    python deploy/dsh/upgrade.py --to 0.2.2-rc.1 --dry-run   # 只打印将要做的事
    python deploy/dsh/upgrade.py --verify                # 不动版本，只跑配置断言

退出码：0 全部通过；1 有条目未通过（已回滚 package.json）；2 环境不可用。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PACKAGE_JSON = PROJECT_DIR / "package.json"
PATCH = PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "cordis.patch.yml"
BIN = PROJECT_DIR / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
DSH_HOME = PROJECT_DIR.parent / ".dsh-bot"


# ── 版本 ────────────────────────────────────────────────────────────────


def pinned_version() -> str:
    """本仓钉的 dsh 版本（`package.json` 是唯一事实源）。"""
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return str(data["dependencies"]["@deepseek-ai/dsh"])


def installed_version() -> str | None:
    """`node_modules` 里实际装的版本；没装返回 None。"""
    manifest = PROJECT_DIR / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not manifest.is_file():
        return None
    return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "")


def latest_version() -> str | None:
    """npm 上的最新版本；网络不可用返回 None（不让它阻断升级流程）。"""
    try:
        out = subprocess.run(
            [_npm(), "view", "@deepseek-ai/dsh", "version"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = out.stdout.strip()
    return version or None


# ── 配置断言（能在机器上判的部分）────────────────────────────────────────


def _dump_config() -> str:
    """让 dsh 合成 profile + patch 后打印配置树（不启服务）。"""
    env = {
        "DSH_HOME": str(DSH_HOME),
        "DSH_BOT_SKILLS": (PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "skills").as_posix(),
        "DSH_AGENTS_HOME": (DSH_HOME / "agents").as_posix(),
    }
    import os

    out = subprocess.run(
        [shutil.which("node") or "node", str(BIN), "--profile", "agentbiz",
         "--patch", str(PATCH), "--dump-config"],
        capture_output=True, text=True, timeout=180, check=False,
        cwd=str(PROJECT_DIR), env={**os.environ, **env},
    )
    return out.stdout + "\n" + out.stderr


def check_patch(text: str) -> list[str]:
    """逐条断言「隔离与凭据」的关键配置还在合成结果里。

    这三条任何一条失效都不会报错，只会让线上行为悄悄变样，所以必须是断言而不是
    「看一眼」。新增这类红线时往这里加一条。
    """
    problems: list[str] = []

    if "did not activate" in text:
        # 插件没激活 = 它下面的配置全部没生效，必须先看清是哪一条
        lines = [ln.strip() for ln in text.splitlines() if "did not activate" in ln]
        problems.append(f"有插件未激活（{lines[0][:80]}…）——先看 stderr 的 Error 行")

    if not re.search(r"agent-instructions[^\n]*\n(?:.*\n){0,4}?\s*disabled:\s*true", text):
        problems.append("agent-instructions 未禁用：个人 ~/.agents/AGENTS.md 会重新注入会话")

    if not re.search(r"includeDefaultRoots:\s*false", text):
        problems.append("skill-filesystem.includeDefaultRoots 不是 false：个人技能会进路由候选集")

    if "apiKeyEnv: AIGW_API_KEY" not in text:
        problems.append("缺少 apiKeyEnv: AIGW_API_KEY：网关凭据断供，第一次 prompt 就报 no credential")

    if not re.search(r"runnerCommand:", text):
        problems.append("sandbox.runnerCommand 缺失：win32 下会多注册 diagnose-windows-sandbox-acl 技能")

    expected = pinned_version()
    if installed_version() != expected:
        # dump-config 不打印版本号，只能拿安装清单对：装错版本时其它断言可能全过，
        # 但行为已不是钉住的那份。
        problems.append(
            f"node_modules 里的 dsh 是 {installed_version() or '未安装'}，与 package.json 的 {expected} 不一致"
        )

    return problems


# ── 主流程 ──────────────────────────────────────────────────────────────


def _npm() -> str:
    """npm 的可执行路径。

    Windows 上只有 `npm.cmd`，`subprocess.run(["npm", ...])` 不经过 shell，
    找不到它只会抛 WinError 2——看起来像「升级脚本坏了」，其实是路径问题。
    """
    for candidate in ("npm.cmd", "npm"):
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("找不到 npm：确认 Node.js 已安装且在 PATH 里")


def _run(cmd: list[str], *, timeout: int = 900) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(PROJECT_DIR), timeout=timeout, check=False).returncode


def upgrade(target: str, *, dry_run: bool = False) -> int:
    current = pinned_version()
    print(f"当前钉住: {current}   目标: {target}")
    if current == target and installed_version() == target:
        print("已是目标版本，只做配置校验")
        return verify()

    if dry_run:
        print("[dry-run] 将修改 package.json 并执行 npm install")
        return 0

    original = PACKAGE_JSON.read_text(encoding="utf-8")
    data = json.loads(original)
    data["dependencies"]["@deepseek-ai/dsh"] = target
    PACKAGE_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 版本号只在 package.json 与 README 顶部出现两处，升级后要保持一致
    readme = PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        readme.write_text(
            re.sub(r"当前基线：`dsh [^`]+`", f"当前基线：`dsh {target}`", text),
            encoding="utf-8",
        )

    try:
        rc = _run([_npm(), "install", "--no-audit", "--no-fund"])
        if rc != 0:
            raise RuntimeError(f"npm install 退出码 {rc}")
        installed = installed_version()
        if installed != target:
            raise RuntimeError(f"装完的版本是 {installed}，不是 {target}")
        print(f"已安装 dsh {installed}")
        return verify()
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        PACKAGE_JSON.write_text(original, encoding="utf-8")
        print(f"升级失败，package.json 已回滚: {e}", file=sys.stderr)
        return 1


def verify() -> int:
    """在现有安装上跑配置断言。不改任何文件。"""
    if not BIN.is_file():
        print("未安装 dsh：先跑 npm install", file=sys.stderr)
        return 2
    text = _dump_config()
    problems = check_patch(text)
    for p in problems:
        print(f"  ✗ {p}")
    print(f"\n配置断言 {len(problems)} 项未通过" if problems else "\n配置断言全部通过 ✓")
    if problems:
        print("\n未通过时先跑 `--dump-config` 看完整合成结果，再决定是改 patch 还是回退版本。")
        return 1
    print("下一步：python deploy/dsh/verify_routing.py（真实路由回归，分段跑更快）")
    return 0


def check() -> int:
    pinned, installed, latest = pinned_version(), installed_version(), latest_version()
    print(f"package.json: {pinned}")
    print(f"node_modules: {installed or '未安装'}")
    print(f"npm 最新:    {latest or '查询失败（网络不可用）'}")
    if latest and latest != pinned:
        print(f"\n有新版本可用：{latest}（升级：--to {latest}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh 内核升级与配置校验")
    parser.add_argument("--check", action="store_true", help="只看版本，不改动")
    parser.add_argument("--verify", action="store_true", help="只跑配置断言")
    parser.add_argument("--to", metavar="VERSION", help="升级到指定版本")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args(argv)

    if args.check:
        return check()
    if args.verify:
        return verify()
    if not args.to:
        parser.error("需要 --to <version>，或用 --check / --verify")
    return upgrade(args.to, dry_run=args.dry_run)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
