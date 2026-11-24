#!/usr/bin/env python3
"""
AI 咨询采集与检索脚本
======================
搜索头部 AI 厂商/大佬博客，摘要沉淀到 .notes/ai-consulting/，检索咨询知识库。

依赖 firecrawl CLI（firecrawl-search Skill）用于搜索+抓取。

用法：
  python app/domains/ai_consultant/ai_consultant.py search "Claude Code best practices"   # 搜索并列出结果
  python app/domains/ai_consultant/ai_consultant.py digest <url> --source anthropic --topic claude-code  # 抓取+摘要沉淀
  python app/domains/ai_consultant/ai_consultant.py query "Claude Code"                  # 检索知识库
  python app/domains/ai_consultant/ai_consultant.py list-topics                          # 列出所有已沉淀主题
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parents[3]
AI_DIR = PROJECT_DIR / ".notes" / "ai-consulting"
BLOG_DIR = AI_DIR / "blog-digest"
COMPANY_DIR = AI_DIR / "company-news"
TOPIC_DIR = AI_DIR / "topics"
INDEX_FILE = AI_DIR / "index.md"
FIRE_DIR = PROJECT_DIR / ".firecrawl"

TZ = timezone(timedelta(hours=8))

# 来源映射
SOURCE_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google DeepMind",
    "personal": "个人博客",
    "personal-blog": "个人博客",
}


# ══════════════════════════════════════════════════════════════
# 搜索
# ══════════════════════════════════════════════════════════════

def _shell_quote(args: list[str]) -> str:
    """将参数安全组合为 shell 命令（处理含空格的参数）"""
    quoted = []
    for a in args:
        if ' ' in a or '"' in a or any(c in a for c in '()&|;<>*?[]$#!'):
            quoted.append(f'"{a}"')
        else:
            quoted.append(a)
    return " ".join(quoted)


def run_firecrawl(args: list[str]) -> tuple[str, str]:
    """调用 firecrawl CLI（优先 firecrawl，回退 npx firecrawl-cli）"""
    # Windows 下 npx 是 .CMD，需 shell=True；参数需正确引用
    shell_cmd = _shell_quote(["firecrawl"] + args)
    try:
        r = subprocess.run(
            shell_cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=90, cwd=str(PROJECT_DIR), shell=True,
        )
        if r.stdout.strip() or r.returncode == 0:
            return r.stdout, r.stderr
    except Exception:
        pass

    # 回退 npx firecrawl-cli（已确认可用）
    npx_cmd = _shell_quote(["npx", "-y", "firecrawl-cli"] + args)
    try:
        r = subprocess.run(
            npx_cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=180, cwd=str(PROJECT_DIR), shell=True,
        )
        return r.stdout, r.stderr
    except Exception as e:
        return "", f"firecrawl CLI 执行失败: {e}"

def search(query: str, limit: int = 5) -> dict:
    """搜索并返回结果列表"""
    FIRE_DIR.mkdir(parents=True, exist_ok=True)
    safe_q = re.sub(r'[^\w\- ]', '', query)[:40].replace(" ", "-")
    out_file = FIRE_DIR / f"search-{safe_q}.json"

    stdout, stderr = run_firecrawl([
        "search", query,
        "--limit", str(limit), "--json",
    ])

    if "未找到" in stderr or "执行失败" in stderr:
        return {"ok": False, "error": stderr}

    # firecrawl-cli 直接输出 JSON 到 stdout，优先解析它
    data = {}
    if stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = {}
    # 回退：读输出文件
    if not data and out_file.exists():
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    results = []
    web = data.get("data", {}).get("web", []) if isinstance(data, dict) else []
    for item in web:
        results.append({
            "title": item.get("title", "?"),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
        })

    # 保存一份到 .firecrawl 供追溯
    if stdout.strip():
        out_file.write_text(stdout, encoding="utf-8")

    return {"ok": True, "query": query, "results": results[:limit]}


# ══════════════════════════════════════════════════════════════
# 抓取 + 摘要沉淀
# ══════════════════════════════════════════════════════════════

def scrape(url: str) -> str:
    """抓取网页全文（markdown）"""
    FIRE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w\-]', '', url)[-40:].replace(" ", "-")
    out_file = FIRE_DIR / f"scrape-{safe}.md"

    run_firecrawl(["scrape", url, "--format", "markdown", "-o", str(out_file)])

    if out_file.exists():
        return out_file.read_text(encoding="utf-8")
    return ""

def digest(url: str, source: str = "personal-blog", topic: str = "general",
           title: str = "", summary: str = "") -> dict:
    """
    抓取单篇文章并沉淀。AI 调用时提供 title/summary 摘要。

    注：本脚本负责落盘到 .notes/ + 生成待写 Base 的 JSON。
    实际摘要由 AI 生成后调用本脚本的 --write 模式落盘。
    """
    content = scrape(url)
    if not content:
        return {"ok": False, "error": "抓取失败", "url": url}

    # 生成文件
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    # 分类目录
    if source in ("anthropic", "openai", "google"):
        target_dir = COMPANY_DIR
    elif topic and topic != "general":
        target_dir = TOPIC_DIR
    else:
        target_dir = BLOG_DIR

    target_dir.mkdir(parents=True, exist_ok=True)

    # slug
    slug_base = title or url.split("/")[-1] or "article"
    slug = re.sub(r'[^\w\-]', '-', slug_base.lower())[:40].strip('-') or "article"

    # 提取正文要点（前 500 字作为预览）
    preview = re.sub(r'\s+', ' ', content)[:500]

    md_content = f"""---
name: {slug}
description: {summary[:120] if summary else title}
metadata:
  type: project
  source: {source}
  url: {url}
  date: {today}
---

# {title or slug}

## TL;DR
{summary or "(待补充摘要，AI 生成后更新)"}

## 原文内容预览
{preview}

## 原文链接
[{title or slug}]({url})
"""

    filepath = target_dir / f"{slug}.md"
    filepath.write_text(md_content, encoding="utf-8")

    # 更新 index.md
    _append_to_index(filepath, title, source, topic, today)

    return {
        "ok": True,
        "file": str(filepath.relative_to(PROJECT_DIR)),
        "title": title or slug,
        "url": url,
        "source": source,
        "topic": topic,
        "date": today,
        "preview": preview,
    }


def _append_to_index(filepath: Path, title: str, source: str, topic: str, date: str):
    """更新 index.md 索引"""
    rel = str(filepath.relative_to(AI_DIR))
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(
            "# AI 咨询知识库索引\n\n| 标题 | 来源 | 主题 | 日期 | 文件 |\n|------|------|------|------|------|\n",
            encoding="utf-8",
        )
    content = INDEX_FILE.read_text(encoding="utf-8")
    if rel in content:
        return  # 已索引
    source_label = SOURCE_LABELS.get(source, source)
    line = f"| {title[:40]} | {source_label} | {topic} | {date} | `{rel}` |\n"
    # 插到表头后
    marker = "|------|------|------|------|------|\n"
    if marker in content:
        content = content.replace(marker, marker + line, 1)
    else:
        content += line
    INDEX_FILE.write_text(content, encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 检索
# ══════════════════════════════════════════════════════════════

def query(keyword: str, limit: int = 5) -> dict:
    """检索咨询知识库"""
    if not AI_DIR.exists():
        return {"ok": True, "results": [], "error": "咨询知识库为空"}

    results = []
    for fp in sorted(AI_DIR.rglob("*.md")):
        if fp.name == "_template.md" or fp.name == "index.md":
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception:
            continue

        # 匹配关键词（标题 + 内容）
        title_match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
        title = title_match.group(1) if title_match else fp.stem
        desc_match = re.search(r'^description:\s*(.+)', content, re.MULTILINE)
        desc = desc_match.group(1) if desc_match else ""

        if keyword.lower() in content.lower() or keyword.lower() in title.lower():
            results.append({
                "title": title,
                "file": str(fp.relative_to(PROJECT_DIR)),
                "description": desc,
                "content": content[:300],
            })

    # 按标题匹配优先排序
    results.sort(key=lambda r: 0 if keyword.lower() in r["title"].lower() else 1)
    return {"ok": True, "query": keyword, "results": results[:limit], "count": len(results)}


def list_topics() -> dict:
    """列出所有已沉淀主题"""
    topics = {}
    for sub in ["blog-digest", "company-news", "topics"]:
        d = AI_DIR / sub
        if d.exists():
            files = [f for f in d.glob("*.md") if f.name != "_template.md"]
            if files:
                topics[sub] = [f.name for f in files]
    return {"ok": True, "topics": topics}


# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AI 咨询采集与检索")
    sub = parser.add_subparsers(dest="action")

    p_search = sub.add_parser("search", help="搜索")
    p_search.add_argument("query", type=str, help="搜索主题")
    p_search.add_argument("--limit", type=int, default=5)

    p_digest = sub.add_parser("digest", help="抓取+摘要沉淀")
    p_digest.add_argument("url", type=str, help="文章 URL")
    p_digest.add_argument("--source", choices=["anthropic", "openai", "google", "personal-blog"], default="personal-blog")
    p_digest.add_argument("--topic", type=str, default="general")
    p_digest.add_argument("--title", type=str, default="")
    p_digest.add_argument("--summary", type=str, default="")

    p_query = sub.add_parser("query", help="检索知识库")
    p_query.add_argument("keyword", type=str, help="关键词")
    p_query.add_argument("--limit", type=int, default=5)

    sub.add_parser("list-topics", help="列出主题")

    args = parser.parse_args()

    if args.action == "search":
        r = search(args.query, args.limit)
        if not r.get("ok"):
            print(f"⛔ {r.get('error', '搜索失败')}")
        else:
            print(f"🔍 搜索: {r['query']}\n")
            for i, item in enumerate(r["results"], 1):
                print(f"{i}. {item['title']}")
                print(f"   {item['url']}")
                if item.get("description"):
                    print(f"   {item['description'][:100]}")
                print()

    elif args.action == "digest":
        r = digest(args.url, args.source, args.topic, args.title, args.summary)
        if r.get("ok"):
            print(f"✅ 已沉淀: {r['file']}")
            print(f"   标题: {r['title']}")
            print(f"   来源: {r['source']} | 主题: {r['topic']} | 日期: {r['date']}")
            print(f"   预览: {r['preview'][:150]}...")
        else:
            print(f"⛔ {r.get('error', '沉淀失败')}")

    elif args.action == "query":
        r = query(args.keyword, args.limit)
        if r.get("error"):
            print(r["error"])
        elif not r["results"]:
            print(f"📭 未找到与「{args.keyword}」相关的咨询内容")
        else:
            print(f"🔎 检索「{args.keyword}」({r['count']} 条):\n")
            for item in r["results"]:
                print(f"▪ {item['title']}")
                print(f"  {item['file']}")
                if item.get("description"):
                    print(f"  {item['description'][:80]}")
                print()

    elif args.action == "list-topics":
        r = list_topics()
        if r["topics"]:
            for sub, files in r["topics"].items():
                print(f"📁 {sub} ({len(files)}):")
                for f in files:
                    print(f"  - {f}")
        else:
            print("📭 暂无已沉淀内容")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()