#!/usr/bin/env python3
"""Prompt 索引生成器 — 从 frontmatter 派生 `bot/prompts/INDEX.md` 的表格区。

**单源是各 prompt 的 frontmatter**；INDEX.md 是派生物，手工改表格必然腐化
（实测：手工维护时期 INDEX 里的版本号、golden 用例数全错，且无人发现）。

用法：
    python app/eval/prompt_index.py            # 重写 INDEX.md 的生成区
    python app/eval/prompt_index.py --check    # 只校验，不一致 exit 1（门禁用）

生成区由 `<!-- BEGIN:generated -->` / `<!-- END:generated -->` 标记包裹，
标记之外的内容（规范说明、加载机制示例）仍由人维护。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = PROJECT_DIR / "bot" / "prompts"
INDEX_PATH = PROMPTS_DIR / "INDEX.md"
GOLDEN_DIR = PROMPTS_DIR / "_golden"

BEGIN = "<!-- BEGIN:generated -->"
END = "<!-- END:generated -->"

# 分区顺序与标题（顺序即阅读顺序，勿随意调换）
SECTIONS: list[tuple[str, str, str]] = [
    ("router", "路由引擎（router/）", "编排层运行时加载，路径由 `PromptLoader.load_router_*` 硬引用"),
    ("agents", "Agent 角色提示词（agents/）", "由 `agent.yaml` 的 `prompt_ref` 单源引用"),
    ("replies", "回复模板（replies/）", ""),
    ("shared", "公共基座（shared/）", "经 `depends_on`（带 `@版本` pin）注入到引用方正文末尾"),
]
SKIP = {"INDEX.md", "CHANGELOG.md"}


def _load_meta(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    import yaml

    try:
        return yaml.safe_load(text[3 : text.index("\n---", 3)]) or {}
    except Exception as exc:  # frontmatter 坏了不要静默跳过——它就是单源
        raise SystemExit(f"frontmatter 解析失败: {path}: {exc}") from exc


def _collect() -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {k: [] for k, _, _ in SECTIONS}
    for path in sorted(PROMPTS_DIR.rglob("*.md")):
        rel = path.relative_to(PROMPTS_DIR).as_posix()
        if path.name in SKIP or rel.startswith("_golden/"):
            continue
        top = rel.split("/", 1)[0] if "/" in rel else "replies"
        if top not in buckets:
            continue
        meta = _load_meta(path)
        buckets[top].append(
            {
                "rel": rel,
                "ver": str(meta.get("version", "?")),
                "tok": meta.get("token_estimate"),
                "dep": list(meta.get("depends_on") or []),
                "desc": " ".join(str(meta.get("description", "")).split()),
            }
        )
    return buckets


def _golden_rows() -> list[dict]:
    rows = []
    if not GOLDEN_DIR.is_dir():
        return rows
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "file": path.name,
                "layer": "行为级" if "intent" in path.stem else "静态",
                "ref": data.get("ref", "?"),
                "n": len(data.get("cases", [])),
                "ver": data.get("version", "?"),
            }
        )
    return rows


def render() -> str:
    buckets = _collect()
    out: list[str] = [BEGIN, ""]
    out.append("> **本区由 `python app/eval/prompt_index.py` 生成，勿手工编辑表格。**")
    out.append("> 单源 = 各 prompt 的 frontmatter（version / description / depends_on / token_estimate）。")
    out.append("> 改 prompt 后运行该脚本；`prompt_eval.py` 会校验本区是否与 frontmatter 一致。")
    out.append("")

    for key, title, note in SECTIONS:
        files = buckets.get(key) or []
        if not files:
            continue
        out.append(f"## {title}")
        out.append("")
        if note:
            out.append(f"> {note}")
            out.append("")
        if key == "shared":
            out.append("| 文件 | 版本 | 用途 |")
            out.append("|------|------|------|")
            for f in files:
                out.append(f"| `{f['rel']}` | v{f['ver']} | {f['desc']} |")
        else:
            out.append("| 文件 | 版本 | Token | depends_on | 用途 |")
            out.append("|------|------|-------|------------|------|")
            for f in files:
                tok = f"~{f['tok']}" if f["tok"] else "—"
                dep = "<br>".join(f"`{d}`" for d in f["dep"]) or "—"
                out.append(f"| `{f['rel']}` | v{f['ver']} | {tok} | {dep} | {f['desc']} |")
        out.append("")

    out.append("## Golden 测试数据集")
    out.append("")
    out.append("`_golden/` — 提示词变更门禁的评估数据集。`prompt_eval.py`（静态 + 结构，score < 80% 阻断）"
               "与 `router_intent_eval.py`（行为级，score < 85% 阻断）双层门禁。")
    out.append("")
    out.append("| 套件 | 层 | ref | 用例数 | 数据集版本 |")
    out.append("|------|---|-----|:---:|------|")
    for g in _golden_rows():
        out.append(f"| `{g['file']}` | {g['layer']} | {g['ref']} | {g['n']} | {g['ver']} |")
    out.append("")
    out.append(f"> 共 {len(_golden_rows())} 个套件。")
    out.append("")
    out.append(END)
    return "\n".join(out) + "\n"


def apply(check: bool) -> int:
    if not INDEX_PATH.exists():
        print(f"INDEX 不存在: {INDEX_PATH}", file=sys.stderr)
        return 1
    text = INDEX_PATH.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"INDEX 缺少生成区标记 {BEGIN} / {END} — 先手动加上再运行", file=sys.stderr)
        return 1
    head = text[: text.index(BEGIN)]
    tail = text[text.index(END) + len(END) :]
    fresh = head + render() + tail.lstrip("\n")

    if check:
        if fresh == text:
            print("INDEX 生成区与 frontmatter 一致")
            return 0
        print(
            "INDEX 生成区已过期：frontmatter 变了但没重跑 `python app/eval/prompt_index.py`",
            file=sys.stderr,
        )
        import difflib

        diff = list(
            difflib.unified_diff(
                text.splitlines(), fresh.splitlines(), "INDEX.md(当前)", "INDEX.md(应为)", lineterm=""
            )
        )
        print("\n".join(diff[:60]), file=sys.stderr)
        return 1

    INDEX_PATH.write_text(fresh, encoding="utf-8")
    print(f"已生成 {INDEX_PATH.relative_to(PROJECT_DIR)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 frontmatter 生成 prompt 索引表格")
    ap.add_argument("--check", action="store_true", help="只校验不写回，不一致 exit 1")
    args = ap.parse_args()
    return apply(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
