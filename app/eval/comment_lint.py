"""注释纪律检查 — 拦截「变更日志式注释」，不是查风格。

为什么需要它：代码注释里写「2026-11-03 用户要求改成 X」「上一版是 Y，现改为 Z」，
对下一个人（或 AI）**没有信息量**——他要的是"为什么必须这样"，不是"什么时候谁说的"。
这类内容属于 git log / CHANGELOG / 设计稿，写进代码只会让注释随代码腐化。

检查三类（全部是「注释」行，不含 docstring 里的普通说明）：
1. 行注释/块注释里出现日期戳 `2026-11-03` 这类绝对日期
2. 注释里出现「用户指出/要求/反馈/指定」「上一版/原来是/现在改成」
3. `TODO/XXX` 没有责任人（裸 TODO 会烂在代码里，见规范 §2）

跑法：
    python -m app.eval.comment_lint app/             # 全量
    python -m app.eval.comment_lint app/domains/x    # 目录
    python -m app.eval.comment_lint app/x.py --strict  # 有任何命中即 exit 1

约定：测试文件里的「为什么这么测」说明**不算**（它们解释的是断言意图，且常需要
指明回归场景）；只查 `app/` 下非 test 的源码。

**门禁里不跑它**：`verify_spec.py` 的 AC-8.1 只跑 ruff，注释纪律靠 CLAUDE.md「闸 1.5」
人工收尾 + 本脚本随时可跑。挂进门禁的代价是每次改动都要清历史遗留，而历史遗留本身
不影响行为——拦截点放在新写的注释上更划算。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 绝对日期戳：YYYY-MM-DD / YYYY.MM.DD / YYYY年M月D日
_DATE_RE = re.compile(r"20\d{2}[-./年]\d{1,2}[-./月]\d{1,2}")
# 「谁说的」类叙述
_WHO_RE = re.compile(r"用户(?:指出|要求|反馈|指定|提出|明确|第二轮|两次)|上一版|原来是|现在改成")
# 裸待办标记（缺 @人 / 工单号）
_TODO_RE = re.compile(r"\b(TODO|XXX|FIXME)\b(?!\s*[（(]?\s*(?:@|#|[A-Z]+-\d+))")
_COMMENT_RE = re.compile(r"(?P<indent>\s*)#(?P<body>.*)$")


def lint_file(path: Path) -> list[tuple[int, str, str]]:
    """→ [(行号, 命中规则, 该行原文)]。只扫 `#` 注释；字符串里的 # 不管（误报代价高）。"""
    out: list[tuple[int, str, str]] = []
    in_doc = False
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw
        stripped = line.strip()
        # 粗略跳过模块/函数 docstring 区（三引号内不按行注释处理，避免误报）
        if stripped.startswith('"""') or stripped.startswith("'''"):
            if stripped.count('"""') % 2 == 1 or stripped.count("'''") % 2 == 1:
                in_doc = not in_doc
            continue
        if in_doc:
            continue
        m = _COMMENT_RE.match(line)
        if not m:
            continue
        body = m.group("body")
        if _DATE_RE.search(body):
            out.append((no, "日期戳", body.strip()[:100]))
        elif _WHO_RE.search(body):
            out.append((no, "谁提的需求", body.strip()[:100]))
        elif _TODO_RE.search(body):
            out.append((no, "裸待办标记（缺 @人/工单号）", body.strip()[:100]))
    return out


def iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts
                  and not p.name.startswith("test_")
                  and "tests" not in p.parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--strict", action="store_true", help="有命中即 exit 1")
    ap.add_argument("--max", type=int, default=0, help="允许的命中上限（默认 0）")
    args = ap.parse_args(argv)

    hits = 0
    for raw in args.paths:
        root = Path(raw)
        if not root.exists():
            print(f"跳过不存在的路径：{raw}")
            continue
        for f in iter_files(root):
            for no, rule, text in lint_file(f):
                hits += 1
                print(f"{f}:{no}: [{rule}] {text}")
    print(f"\n共 {hits} 处（上限 {args.max}）")
    return 1 if hits > args.max else 0


if __name__ == "__main__":
    sys.exit(main())
