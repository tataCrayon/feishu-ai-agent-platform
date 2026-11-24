#!/usr/bin/env python3
"""
AI 咨询订阅管理引擎
====================
管理订阅 campaign，心跳到期检查，触发采集。

用法：
  python app/domains/ai_consultant/ai_campaign.py list
  python app/domains/ai_consultant/ai_campaign.py add --name "Claude Code动态" --keywords "claude code" --interval 7
  python app/domains/ai_consultant/ai_campaign.py remove --id anthropic-claude-code
  python app/domains/ai_consultant/ai_campaign.py run           # 执行所有到期订阅
  python app/domains/ai_consultant/ai_campaign.py due           # 查看到期订阅
"""

import argparse
import json
import re
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
AI_DIR = PROJECT_DIR / "bot" / "ai"
CAMPAIGNS_FILE = AI_DIR / "campaigns.json"
CONSULTANT = PROJECT_DIR / "app" / "domains" / "ai_consultant" / "ai_consultant.py"

TZ = timezone(timedelta(hours=8))

# 预置来源
DEFAULT_SOURCES = {
    "anthropic": {
        "name": "Anthropic 动态",
        "keywords": ["anthropic", "claude", "claude code"],
        "interval_days": 7,
    },
    "openai": {
        "name": "OpenAI 动态",
        "keywords": ["openai", "gpt", "chatgpt"],
        "interval_days": 7,
    },
    "google": {
        "name": "Google DeepMind 动态",
        "keywords": ["gemini", "deepmind"],
        "interval_days": 7,
    },
    "simon-willison": {
        "name": "Simon Willison 博客",
        "keywords": ["simon willison", "llm", "claude"],
        "interval_days": 14,
    },
    "lilian-weng": {
        "name": "Lilian Weng 博客",
        "keywords": ["lilian weng", "agent", "llm"],
        "interval_days": 14,
    },
}


def load_campaigns() -> dict:
    if CAMPAIGNS_FILE.exists():
        try:
            return json.loads(CAMPAIGNS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": "1.0", "campaigns": []}


def save_campaigns(data: dict):
    AI_DIR.mkdir(parents=True, exist_ok=True)
    CAMPAIGNS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_id(name: str) -> str:
    return re.sub(r'[^\w\-]', '-', name.lower()).strip('-')[:40]


def add_campaign(name: str, keywords: list[str], interval: int, source: str = "personal-blog",
                 subscribers: list[str] | None = None) -> dict:
    """新增订阅"""
    data = load_campaigns()
    cid = make_id(name)

    # 检查是否已存在
    if any(c["id"] == cid for c in data["campaigns"]):
        return {"ok": False, "reason": f"订阅已存在: {cid}"}

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    campaign = {
        "id": cid,
        "name": name,
        "source": source,
        "keywords": keywords,
        "interval_days": interval,
        "last_checked": "",
        "next_check": today,  # 立即到期，首次 run 即采集
        "status": "active",
        "subscribers": subscribers or [],
    }
    data["campaigns"].append(campaign)
    save_campaigns(data)
    return {"ok": True, "id": cid, "campaign": campaign}


def remove_campaign(cid: str) -> dict:
    data = load_campaigns()
    before = len(data["campaigns"])
    data["campaigns"] = [c for c in data["campaigns"] if c["id"] != cid]
    if len(data["campaigns"]) == before:
        return {"ok": False, "reason": f"未找到订阅: {cid}"}
    save_campaigns(data)
    return {"ok": True, "removed": cid}


def list_campaigns() -> dict:
    data = load_campaigns()
    return {"ok": True, "campaigns": data["campaigns"]}


def due_campaigns() -> list[dict]:
    """找出到期订阅"""
    data = load_campaigns()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    due = []
    for c in data["campaigns"]:
        if c.get("status") != "active":
            continue
        if not c.get("next_check") or c["next_check"] <= today:
            due.append(c)
    return due


def run_due(limit: int = 3) -> dict:
    """执行到期订阅：搜索关键词 → 沉淀 → 更新 next_check"""
    due = due_campaigns()
    if not due:
        return {"ok": True, "ran": 0, "message": "无到期订阅"}

    executed = []
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    for c in due[:limit]:
        results = _search_keywords(c)
        executed.append({
            "id": c["id"],
            "name": c["name"],
            "results": results,
        })

    # 更新 next_check（所有 due 都更新，即使未采集到）
    data = load_campaigns()
    today_dt = datetime.now(TZ)
    for c in data["campaigns"]:
        if c["id"] in [d["id"] for d in due[:limit]]:
            c["last_checked"] = today
            c["next_check"] = (today_dt + timedelta(days=c.get("interval_days", 7))).strftime("%Y-%m-%d")
    save_campaigns(data)

    return {
        "ok": True,
        "ran": len(executed),
        "due_total": len(due),
        "executed": executed,
    }


def _search_keywords(campaign: dict) -> list[dict]:
    """对订阅关键词执行搜索"""
    results = []
    for kw in campaign.get("keywords", [])[:2]:  # 每个订阅最多搜 2 个关键词
        try:
            import subprocess
            r = subprocess.run(
                [sys.executable, str(CONSULTANT), "search", kw, "--limit", "3"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=60, cwd=str(PROJECT_DIR),
            )
            # 从输出解析
            out = r.stdout
            items = []
            # 简单解析标题+URL
            for line in out.split("\n"):
                line = line.strip()
                if (line.startswith(tuple("123456789")) and "." in line[:3]) or line.startswith("http"):
                    items.append(line)
            results.append({"keyword": kw, "raw": out[:500]})
        except Exception as e:
            results.append({"keyword": kw, "error": str(e)})
    return results


def main():
    parser = argparse.ArgumentParser(description="AI 咨询订阅管理")
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list", help="列出订阅")
    p_add = sub.add_parser("add", help="新增订阅")
    p_add.add_argument("--name", type=str, required=True)
    p_add.add_argument("--keywords", type=str, help="关键词，逗号分隔")
    p_add.add_argument("--interval", type=int, default=7, help="订阅周期(天)")
    p_add.add_argument("--source", type=str, default="personal-blog")
    p_add.add_argument("--subscriber", type=str, action="append", default=[])

    p_remove = sub.add_parser("remove", help="删除订阅")
    p_remove.add_argument("--id", type=str, required=True)

    p_run = sub.add_parser("run", help="执行到期订阅")
    p_run.add_argument("--limit", type=int, default=3)

    sub.add_parser("due", help="查看到期订阅")

    args = parser.parse_args()

    if args.action == "list":
        r = list_campaigns()
        if not r["campaigns"]:
            print("📭 暂无订阅")
        else:
            print(f"📡 订阅列表 ({len(r['campaigns'])})")
            print("=" * 50)
            for c in r["campaigns"]:
                status = "🟢" if c.get("status") == "active" else "⚪"
                print(f"  {status} {c['id']}")
                print(f"    名称: {c['name']} | 周期: {c.get('interval_days','?')}天")
                print(f"    关键词: {', '.join(c.get('keywords', []))}")
                print(f"    下次检查: {c.get('next_check', '?')} | 订阅人: {len(c.get('subscribers', []))}")

    elif args.action == "add":
        keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
        r = add_campaign(args.name, keywords, args.interval, args.source, args.subscriber)
        if r["ok"]:
            print(f"✅ 已新增订阅: {r['id']}")
        else:
            print(f"⛔ {r['reason']}")

    elif args.action == "remove":
        r = remove_campaign(args.id)
        print(f"✅ 已删除: {r['removed']}" if r["ok"] else f"⛔ {r['reason']}")

    elif args.action == "run":
        r = run_due(args.limit)
        if r["ok"]:
            print(f"✅ 执行完成: {r['ran']} 个订阅 (共 {r.get('due_total', 0)} 个到期)")
            for e in r.get("executed", []):
                print(f"  ▶ {e['name']} ({e['id']})")
                for res in e.get("results", []):
                    print(f"    · {res.get('keyword', '?')}: {res.get('raw', '')[:100]}")
        else:
            print(f"⛔ {r.get('message', '执行失败')}")

    elif args.action == "due":
        due = due_campaigns()
        if not due:
            print("📭 今日无到期订阅")
        else:
            print(f"📡 到期订阅 ({len(due)}):")
            for c in due:
                print(f"  ▶ {c['id']} ({c['name']}) — 关键词: {', '.join(c.get('keywords', []))}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()