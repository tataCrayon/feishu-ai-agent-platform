"""把 bot/skills 中白名单内的技能同步到 dsh profile 的技能目录。

为什么需要这一层：`bot/skills/` 混着业务能力与工具技巧，而 dsh 的技能目录**就是路由候选集**
（实测：放进工具型技能会让它与业务 agent 抢路由）。所以 dsh 侧只看一份白名单，
而不是直接指向源目录。

为什么用「整体替换」而不是增量同步：源目录里的技能可能被 capability 覆盖安装、改名或删除，
增量同步会留下孤儿目录，而孤儿目录仍然参与路由。整体替换的代价是 Chokidar 会看到
一批删除+新增，但 dsh 每次发现都重读 catalog，不影响正确性。

用法：
    python deploy/dsh/sync_skills.py                # 按白名单同步
    python deploy/dsh/sync_skills.py --check        # 只校验，不写（CI/门禁用）
    python deploy/dsh/sync_skills.py --add NAME     # 追加一条到白名单再同步

失败语义：返回非零退出码并在 stderr 说明原因（缺技能 / 白名单格式错 / 目标不可写）。
不抛未捕获异常——它会被 capability 热插拔链路以子进程方式调用。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
SOURCE_DIR = PROJECT_DIR / "bot" / "skills"
AGENTS_DIR = PROJECT_DIR / "bot" / "agents"
PROFILE_DIR = PROJECT_DIR / "deploy" / "dsh" / "agentbiz"
ALLOWLIST = PROFILE_DIR / "skills.allowlist"
TARGET_DIR = PROFILE_DIR / "skills"

# 技能入口文件名（dsh 只认同目录下的 SKILL.md；flat 形式不支持，见 dsh-skill-filesystem README）
SKILL_ENTRY = "SKILL.md"


def read_allowlist(path: Path = ALLOWLIST) -> list[str]:
    """读白名单，忽略空行与 `#` 注释（行尾注释也剥掉）。"""
    if not path.exists():
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def validate(names: list[str], source: Path = SOURCE_DIR) -> list[str]:
    """返回问题列表；空列表表示可以同步。"""
    problems: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            problems.append(f"白名单重复：{name}")
            continue
        seen.add(name)
        if (source / name / SKILL_ENTRY).is_file():
            continue
        manifest = AGENTS_DIR / name / "agent.yaml"
        if not manifest.is_file():
            problems.append(
                f"白名单条目既没有 {source / name / SKILL_ENTRY}，也没有 {manifest}"
            )
    return problems


def _normalize_entry(skill_dir: Path) -> None:
    """把入口文件名统一成大写的 SKILL.md。

    dsh 只认 `SKILL.md`，而本仓有技能用小写 `skill.md`（Windows 上两者等价，
    拷到 Linux 就只有一个能命中），生成物必须显式规范化而不依赖文件系统大小写。
    """
    upper = skill_dir / SKILL_ENTRY
    if upper.is_file():
        return
    for candidate in skill_dir.iterdir():
        if candidate.is_file() and candidate.name.lower() == SKILL_ENTRY.lower():
            candidate.rename(upper)
            return


def _render_agent_skill(name: str) -> str:
    """把 agent.yaml 的 description 渲染成技能入口文件。

    这些「能力型 agent」本来就没有 SKILL.md 正文——它们的能力在执行侧（MCP 工具或
    handler）。这里只借 dsh 的技能目录把名字与描述放进路由候选集，正文写清
    「调用对应工具完成」，避免模型去猜。
    """
    import re

    manifest = AGENTS_DIR / name / "agent.yaml"
    text = manifest.read_text(encoding="utf-8")
    desc = re.search(r"^description:\s*\"?(.+?)\"?\s*$", text, re.M)
    if not desc:
        raise ValueError(f"{manifest} 缺少 description，无法作为路由候选")
    body = [
        "---",
        f"name: {name}",
        f"description: {desc.group(1).strip()}",
        "---",
        "",
        f"# {name}",
        "",
        "该能力由同名 MCP 工具或进程内执行入口承担。收到匹配的请求时，",
        "直接调用对应工具完成，不要凭记忆作答。",
        "",
    ]
    return chr(10).join(body)


def sync(names: list[str], source: Path = SOURCE_DIR, target: Path = TARGET_DIR) -> None:
    """整体替换目标目录。先写临时目录再换名，失败时旧目录仍在。"""
    staging = target.with_name(target.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for name in names:
        if (source / name).is_dir():
            shutil.copytree(source / name, staging / name)
            _normalize_entry(staging / name)
        else:
            skill_dir = staging / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / SKILL_ENTRY).write_text(_render_agent_skill(name), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 dsh profile 的业务技能白名单")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘")
    parser.add_argument("--add", metavar="NAME", help="追加一条到白名单后同步")
    args = parser.parse_args(argv)

    if args.add:
        if not (SOURCE_DIR / args.add / SKILL_ENTRY).is_file():
            print(f"源目录没有这个技能：{SOURCE_DIR / args.add}", file=sys.stderr)
            return 2
        with ALLOWLIST.open("a", encoding="utf-8") as fh:
            fh.write(f"{args.add}\n")

    names = read_allowlist()
    problems = validate(names)
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1

    if args.check:
        print(f"白名单校验通过：{len(names)} 项")
        return 0

    try:
        sync(names)
    except OSError as e:
        print(f"同步失败：{e}", file=sys.stderr)
        return 1
    print(f"已同步 {len(names)} 个技能到 {TARGET_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
