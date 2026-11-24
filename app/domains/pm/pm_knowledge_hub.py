"""PM Knowledge & People Map Retriever - 知识库与人物协作地图智能穿透引擎"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pm_hub.knowledge")

# handlers → agents → app → feishu-agent-platform → project
# 原来多往上跳了一层，指到 project 的父目录，于是 knowledge-base / people-map
# / inbox / SOP 全部查不到，检索接口一直返回空列表。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
KB_ROOT = os.path.join(PROJECT_ROOT, "knowledge-base")
PEOPLE_MAP_FILE = os.path.join(PROJECT_ROOT, "people-map", "README.md")
INBOX_DIR = os.path.join(PROJECT_ROOT, "inbox")
DAILY_LOG_DIR = os.path.join(PROJECT_ROOT, "daily-log")
SOP_DIR = os.path.join(PROJECT_ROOT, "SOP")


def query_people_map(query: str) -> Dict[str, Any]:
    """在人物地图与协作矩阵中搜索责任人、模块与AB角"""
    if not os.path.exists(PEOPLE_MAP_FILE):
        return {"found": False, "message": "未找到 people-map/README.md 文件"}

    with open(PEOPLE_MAP_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    matched_sections: List[str] = []
    current_category = "通用"
    
    # 关键字匹配
    query_lower = query.lower()
    
    table_headers = ""
    for line in lines:
        if line.startswith("### "):
            current_category = line.replace("###", "").strip()
        elif line.startswith("| 姓名") or line.startswith("| 姓名/公司"):
            table_headers = line
        elif line.startswith("|") and not line.startswith("|---"):
            if any(k in line.lower() for k in query_lower.split()):
                matched_sections.append(f"【{current_category}】\n{table_headers}\n{line}")

    if not matched_sections:
        # 如果没有表格行精确匹配，尝试在整体文本段落中搜索
        paragraphs = content.split("\n\n")
        for p in paragraphs:
            if any(k in p.lower() for k in query_lower.split()) and len(p.strip()) > 10:
                matched_sections.append(p.strip())

    return {
        "found": len(matched_sections) > 0,
        "query": query,
        "results": matched_sections[:5],
        "total_matches": len(matched_sections),
    }


def search_knowledge_base(query: str, layer: Optional[str] = None) -> List[Dict[str, Any]]:
    """在 L1/L2/L3 及 SOP 目录下进行多层级全文关键词匹配与相关段落提取"""
    query_tokens = [q.strip() for q in query.split() if q.strip()]
    if not query_tokens:
        return []

    target_dirs = []
    if layer:
        target_dirs.append(os.path.join(KB_ROOT, layer))
    else:
        # KB_ROOT 本身就包含 L1/L2/L3。三个子目录再列一遍会让同一篇文档
        # 被扫两次，结果里出现重复条目（实测「产品线与UI需求规则」排了两行）。
        target_dirs.extend([KB_ROOT, SOP_DIR])

    matched_docs: List[Dict[str, Any]] = []

    for d in target_dirs:
        if not os.path.exists(d):
            continue
        for root, _, files in os.walk(d):
            for file in files:
                if not file.endswith(".md"):
                    continue
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, PROJECT_ROOT)

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        text = f.read()
                except Exception:
                    continue

                # 计算匹配命中分
                score = 0
                title_hit = any(tok.lower() in file.lower() for tok in query_tokens)
                if title_hit:
                    score += 5

                paragraphs = text.split("\n\n")
                matched_snippets = []

                for p in paragraphs:
                    p_clean = p.strip()
                    if not p_clean:
                        continue
                    hit_count = sum(1 for tok in query_tokens if tok.lower() in p_clean.lower())
                    if hit_count > 0:
                        score += hit_count
                        if len(matched_snippets) < 3:
                            matched_snippets.append(p_clean[:300] + ("..." if len(p_clean) > 300 else ""))

                if score > 0:
                    matched_docs.append({
                        "file_name": file,
                        "rel_path": rel_path,
                        "score": score,
                        "snippets": matched_snippets,
                    })

    # 按相关度从高到低排序
    matched_docs.sort(key=lambda x: x["score"], reverse=True)
    return matched_docs[:5]


def search_notes(query: str, days: int = 30) -> List[Dict[str, Any]]:
    """在 daily-log 和 inbox 里找以前记过的东西。

    为什么需要这个
    -------------
    `pm_capture_note` 能把用户口述的东西写进 daily-log，
    但 `search_knowledge_base` 只扫 knowledge-base/ 和 SOP/ ——
    写进去的从来没入口读得出来。

    节点很具体：上周他记了「王五下周休假，别催她」，这周问「王五怎么没回」，
    机器人一脸茫然发消息去催。写和读必须是一对，缺一半等于没记。

    这里是「写」的对面：一个唯一的读取口，覆盖两条落点。
    """
    tokens = [t for t in (query or "").split() if t.strip()]
    if not tokens:
        return []

    cutoff = date.today() - timedelta(days=max(1, days))
    out: List[Dict[str, Any]] = []

    for root in (DAILY_LOG_DIR, INBOX_DIR):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            if mtime.date() < cutoff:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue

            hits = []
            for line in text.splitlines():
                low = line.lower()
                n = sum(1 for t in tokens if t.lower() in low)
                if n:
                    hits.append((n, line.strip()))
            if not hits:
                continue

            hits.sort(key=lambda x: -x[0])
            out.append({
                "file": os.path.relpath(path, PROJECT_ROOT),
                "when": mtime.strftime("%Y-%m-%d"),
                "score": sum(h[0] for h in hits[:3]),
                "snippets": [h[1][:200] for h in hits[:3]],
            })

    # 命中多的优先；同分时新的优先
    out.sort(key=lambda x: (x["score"], x["when"]), reverse=True)
    return out[:5]


def capture_to_inbox(raw_content: str, source: str = "飞书群聊速记", tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """将碎片化经验、未分类问题或业务闪念存入 inbox/ 快存区"""
    os.makedirs(INBOX_DIR, exist_ok=True)
    now = datetime.now()
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    file_name = f"inbox_{ts_str}.md"
    file_path = os.path.join(INBOX_DIR, file_name)

    tag_str = ", ".join(tags) if tags else "未分类"
    doc_body = f"""---
title: "快存速记 - {now.strftime('%Y-%m-%d %H:%M')}"
status: pending
source: {source}
tags: [{tag_str}]
created_at: {now.isoformat()}
---

### 原始记录内容
{raw_content}

---
*待 PMO 周期归档：视内容价值归档至 L1概念库 / L2流程库 / L3经验库 / 废弃*
"""

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(doc_body)

    return {
        "success": True,
        "file_name": file_name,
        "file_path": file_path,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }