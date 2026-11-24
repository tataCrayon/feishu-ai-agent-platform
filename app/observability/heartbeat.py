#!/usr/bin/env python3
"""
心跳巡检脚本 — 机械步骤自动化
===============================
将 HEARTBEAT.md 中不需要 AI 判断的步骤脚本化，心跳从 AI 执行 6 步
变成「脚本执行 4 步 + AI 补充 2 步（摘要更新+内容提炼）」。

用法：
  python app/observability/heartbeat.py              # 完整巡检
  python app/observability/heartbeat.py --json       # JSON 输出（供 AI 消费）
  python app/observability/heartbeat.py --step 0     # 单步执行

AI 只负责需要判断的事：摘要更新、短期记忆维护、晋升内容提炼。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys

# 修复 Windows GBK 控制台编码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── 路径常量 ────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # feishu-agent-platform/
MEMORIES_DIR = PROJECT_DIR / ".agent" / "memories"
GLOBAL_DIR = MEMORIES_DIR / "_global"
USERS_DIR = MEMORIES_DIR / "users"
GROUPS_DIR = MEMORIES_DIR / "groups"
PENDING_DIR = PROJECT_DIR / "bot" / "todos" / "pending"
DONE_DIR = PROJECT_DIR / "bot" / "todos" / "done"
INDEX_FILE = MEMORIES_DIR / "INDEX.md"
PROMOTION_FILE = MEMORIES_DIR / "PROMOTION_QUEUE.md"
SESSION_LAST = MEMORIES_DIR / "SESSION_LAST.md"
SHORT_TERM = MEMORIES_DIR / "SHORT_TERM.md"
EVAL_DIR = PROJECT_DIR / ".agent" / "eval"
REPORTS_DIR = EVAL_DIR / "reports"
FRONTMATTER_SPEC = MEMORIES_DIR / "FRONTMATTER_SPEC.md"
LOGS_DAILY = PROJECT_DIR / "logs" / "daily"
TOKEN_MONITOR = PROJECT_DIR / "app" / "observability" / "token_monitor.py"
ARCHIVED_DIR = GLOBAL_DIR / ".archived"

TZ = timezone(timedelta(hours=8))  # CST
REPLY_TIMEOUT_MIN = 10  # 分钟
EXPIRE_DAYS = 90
ARCHIVE_DAYS = 180

today = datetime.now(TZ).strftime("%Y-%m-%d")
is_monday = datetime.now(TZ).weekday() == 0


# ══════════════════════════════════════════════════════════════
# Step 0: Token 统计
# ══════════════════════════════════════════════════════════════

def run_token_command(args: list[str]) -> str:
    """调用 token_monitor，处理 Windows 编码问题"""
    try:
        r = subprocess.run(
            [sys.executable, str(TOKEN_MONITOR)] + args,
            capture_output=True, timeout=30,
            cwd=str(PROJECT_DIR),
            encoding="utf-8",
            errors="replace",
        )
        return r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        return f"ERROR: {e}"


def step_token_stats() -> dict:
    """调用 token_monitor.py 执行 scan + summarize"""
    scan_out = run_token_command(["scan"])
    summarize_out = run_token_command(["summarize", "--date", today])

    results = {"scan": scan_out, "summarize": summarize_out}

    # 读取今日 token 汇总
    daily_json = LOGS_DAILY / f"{today}.json"
    token_summary = {}
    if daily_json.exists():
        try:
            token_summary = json.loads(daily_json.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {"results": results, "token_summary": token_summary}


# ══════════════════════════════════════════════════════════════
# Step 2: lark-cli 登录态（easyprd / 案例镜像全部依赖它）
# ══════════════════════════════════════════════════════════════

# refresh token 有效期内，随便一次 lark-cli 调用都会自动续期；超过这个天数
# 没人用过，下次调用就得重新扫码。留 2 天余量提醒。
_LARK_REFRESH_WARN_DAYS = 5
_LARK_TOKEN_WARN_HOURS = 2


def step_lark_auth() -> dict:
    """查 lark-cli 登录态：user token 是否将过期、refresh 是否将失效。

    为什么要在心跳里查：所有 PRD 读写、妙记、案例镜像都走 `--as user`，
    token 过期后失败形态是 `invalid token`（不是缺 scope），
    看起来像代码问题。整周没人跑 lark-cli 时 refresh token 也会失效，
    那才需要人工重新扫码——提前一周知道，比当天排查省事。
    """
    out: dict[str, Any] = {"ok": False, "checked": False}
    try:
        # Windows 上 npm 全局包只有 .cmd 可执行（裸名 subprocess 找不到）
        exe = shutil.which("lark-cli.cmd") or shutil.which("lark-cli") or "lark-cli"
        r = subprocess.run(
            [exe, "auth", "status", "--json"],
            capture_output=True, timeout=30, cwd=str(PROJECT_DIR),
            encoding="utf-8", errors="replace",
        )
        raw = (r.stdout or "").strip()
        start = raw.find("{")
        data = json.loads(raw[start:]) if start >= 0 else {}
    except FileNotFoundError:
        return {"ok": False, "checked": False, "error": "未找到 lark-cli（未安装或不在 PATH）"}
    except Exception as e:
        return {"ok": False, "checked": False, "error": f"{type(e).__name__}: {e}"[:200]}

    out["checked"] = True
    user = ((data.get("identities") or {}).get("user") or {})
    bot = ((data.get("identities") or {}).get("bot") or {})
    out["user"] = user.get("userName", "")
    out["token_status"] = user.get("tokenStatus", "")
    out["bot_status"] = bot.get("status", "")
    problems: list[str] = []

    def _parse(ts: str) -> datetime | None:
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TZ)
        except Exception:
            return None

    now = datetime.now(TZ)
    access = _parse(str(user.get("expiresAt") or ""))
    refresh = _parse(str(user.get("refreshExpiresAt") or ""))
    if access is not None:
        hours = (access - now).total_seconds() / 3600
        out["access_expires_in_hours"] = round(hours, 1)
    if refresh is not None:
        days = (refresh - now).total_seconds() / 86400
        out["refresh_expires_in_days"] = round(days, 1)
        if days <= 0:
            problems.append("refresh token 已失效 → 需人工重新扫码（lark-cli auth login）")
        elif days < _LARK_REFRESH_WARN_DAYS:
            problems.append(
                f"refresh token 仅剩 {days:.1f} 天 → 跑一次任意 lark-cli 命令即可续期")
    if not user.get("scope"):
        problems.append("user 未登录 → PRD 读写/妙记/案例镜像全部不可用")
    elif access is not None and (access - now).total_seconds() / 3600 < _LARK_TOKEN_WARN_HOURS \
            and str(user.get("tokenStatus")) != "needs_refresh":
        # 正常情况会自动续期，列出来只是让"刚过期那一小时"有据可查
        out["note"] = "access token 即将过期，下次调用会自动续期"

    out["ok"] = not problems
    out["problems"] = problems
    return out


# ══════════════════════════════════════════════════════════════
# Step 3: Pending 超时检查
# ══════════════════════════════════════════════════════════════

def parse_pending_file(path: Path) -> dict | None:
    """解析 pending 文件"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = {}
        for line in raw.strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        return data
    except Exception:
        return None


def step_pending_check() -> dict:
    """检查 pending 目录，标记超时和需关注的条目"""
    if not PENDING_DIR.exists():
        return {"pending_count": 0, "overdue": [], "ok": []}

    now = datetime.now(TZ)
    overdue = []
    ok = []

    for f in sorted(PENDING_DIR.glob("*.md")):
        data = parse_pending_file(f)
        if not data:
            continue

        created_at = data.get("created_at", "")
        try:
            created_ts = int(created_at) / 1000
            created_dt = datetime.fromtimestamp(created_ts, tz=TZ)
        except (ValueError, TypeError):
            continue

        elapsed_min = (now - created_dt).total_seconds() / 60
        entry = {
            "file": f.name,
            "chat_id": data.get("chat_id", ""),
            "sender": data.get("sender", ""),
            "msg": data.get("msg", "")[:100],
            "elapsed_min": round(elapsed_min, 1),
            "created_at": created_at,
        }

        if elapsed_min >= REPLY_TIMEOUT_MIN:
            overdue.append(entry)
        else:
            ok.append(entry)

    return {
        "pending_count": len(overdue) + len(ok),
        "overdue": overdue,
        "ok": ok,
        "overdue_count": len(overdue),
    }


# ══════════════════════════════════════════════════════════════
# Step 4: 晋升队列预筛选
# ══════════════════════════════════════════════════════════════

def step_promotion_queue() -> dict:
    """解析 PROMOTION_QUEUE.md，提取 ≥2 次的条目供 AI 提炼内容"""
    if not PROMOTION_FILE.exists():
        return {"entries": [], "promotable": [], "empty": True}

    content = PROMOTION_FILE.read_text(encoding="utf-8")
    entries = []
    promotable = []

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line.startswith(">"):
            continue
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        try:
            entry = {
                "date": parts[0],
                "topic": parts[1],
                "count": int(parts[2]),
                "context": parts[3] if len(parts) > 3 else "",
                "raw_line": line,
            }
            entries.append(entry)
            if entry["count"] >= 2:
                promotable.append(entry)
        except (ValueError, IndexError):
            continue

    return {
        "entries": entries,
        "promotable": promotable,
        "promotable_count": len(promotable),
        "empty": len(entries) == 0,
    }


# ══════════════════════════════════════════════════════════════
# Step 5: 记忆健康巡检（周一执行）
# ══════════════════════════════════════════════════════════════

def parse_frontmatter(filepath: Path) -> dict | None:
    """解析记忆文件的 frontmatter"""
    try:
        content = filepath.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        end = content.find("---", 3)
        if end == -1:
            return None
        fm_text = content[3:end].strip()
        fm = {}
        for line in fm_text.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        return fm
    except Exception:
        return None


def scan_memory_files() -> list[Path]:
    """扫描所有记忆文件"""
    files = []
    for d in [GLOBAL_DIR, USERS_DIR, GROUPS_DIR]:
        if d.exists():
            files.extend(d.rglob("*.md"))
    # 排除 .archived 和模板
    files = [f for f in files if ".archived" not in str(f) and "_TEMPLATE" not in str(f)]
    return files


def step_consistency_check() -> dict:
    """对比 INDEX.md 与实际文件一致性"""
    issues = []

    # 读 INDEX.md 中的索引条目
    index_entries: set[str] = set()
    if INDEX_FILE.exists():
        content = INDEX_FILE.read_text(encoding="utf-8")
        for line in content.split("\n"):
            # 匹配表格行中的文件名
            m = re.search(r'`?(_global|users|groups)/[^`\s]+\.md`?', line)
            if m:
                # 提取完整路径
                path = m.group(1).strip("`")
                # 从路径中提取文件名
                index_entries.add(Path(path).name)

    # 实际文件（扫描一次校验索引覆盖，结果由下方 all_md 消费）
    _actual_files = {f.name for f in scan_memory_files()}
    # 也从子目录查
    all_md: set[str] = set()
    for d in [GLOBAL_DIR, USERS_DIR, GROUPS_DIR]:
        if d.exists():
            for f in d.rglob("*.md"):
                if ".archived" not in str(f) and "_TEMPLATE" not in str(f):
                    all_md.add(f.name)

    orphan_index = index_entries - all_md  # INDEX 有但文件不存在
    unindexed = all_md - index_entries      # 文件存在但 INDEX 无

    if orphan_index:
        issues.append(f"INDEX 孤立条目: {', '.join(sorted(orphan_index))}")
    if unindexed:
        issues.append(f"未索引文件: {', '.join(sorted(unindexed))}")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "orphan_index": sorted(orphan_index),
        "unindexed": sorted(unindexed),
    }


def step_format_validate() -> dict:
    """校验所有记忆文件的 frontmatter 格式"""
    required = ["name", "description", "type", "updated"]
    invalid = []
    missing_fields: dict[str, list] = {}

    for fp in scan_memory_files():
        fm = parse_frontmatter(fp)
        if fm is None:
            invalid.append({"file": str(fp.relative_to(PROJECT_DIR)), "issue": "无 frontmatter 或格式错误"})
            continue

        missing = [f for f in required if f not in fm]
        if missing:
            rel = str(fp.relative_to(PROJECT_DIR))
            invalid.append({"file": rel, "issue": f"缺少字段: {missing}"})
            missing_fields[rel] = missing

    return {
        "ok": len(invalid) == 0,
        "invalid_count": len(invalid),
        "invalid": invalid,
    }


def step_expiration_check() -> dict:
    """检查过期记忆"""
    expired = []
    for_archive = []

    for fp in scan_memory_files():
        fm = parse_frontmatter(fp)
        if not fm or "updated" not in fm:
            continue

        try:
            updated = datetime.strptime(fm["updated"], "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            continue

        days_since = (datetime.now(TZ) - updated).days
        rel = str(fp.relative_to(PROJECT_DIR))

        if days_since > ARCHIVE_DAYS:
            for_archive.append({"file": rel, "days_since": days_since, "updated": fm["updated"]})
        elif days_since > EXPIRE_DAYS:
            # 检查是否已标记过期
            content = fp.read_text(encoding="utf-8")
            if "此记忆最后更新于" not in content:
                expired.append({"file": rel, "days_since": days_since, "updated": fm["updated"]})

    return {
        "expired_count": len(expired),
        "archive_count": len(for_archive),
        "expired": expired,
        "for_archive": for_archive,
    }


def _content_similarity(text1: str, text2: str) -> float:
    """简单的 TF 重叠度计算（不需要 AI）"""
    def tokenize(t: str) -> set[str]:
        # 提取中文 2-gram + 英文单词
        words = set(re.findall(r'[a-zA-Z_]{3,}', t.lower()))
        # 中文 2-gram
        chars = re.sub(r'[^一-鿿]', '', t)
        for i in range(len(chars) - 1):
            words.add(chars[i:i+2])
        return words

    t1 = tokenize(text1)
    t2 = tokenize(text2)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def step_duplicate_detect(threshold: float = 0.6) -> dict:
    """检测重复记忆文件"""
    files = [f for f in scan_memory_files() if f.parent == GLOBAL_DIR]  # 只检查 _global/
    duplicates = []

    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            try:
                t1 = files[i].read_text(encoding="utf-8")
                t2 = files[j].read_text(encoding="utf-8")
            except Exception:
                continue
            sim = _content_similarity(t1, t2)
            if sim >= threshold:
                duplicates.append({
                    "file1": str(files[i].relative_to(PROJECT_DIR)),
                    "file2": str(files[j].relative_to(PROJECT_DIR)),
                    "similarity": round(sim, 2),
                })

    return {
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "ok": len(duplicates) == 0,
    }


def step_health_check() -> dict:
    """完整健康巡检（周一执行）"""
    if not is_monday:
        return {"skipped": True, "reason": "非周一，跳过健康巡检"}

    consistency = step_consistency_check()
    format_result = step_format_validate()
    expiration = step_expiration_check()
    duplicates = step_duplicate_detect()

    # 自动修复：标记过期文件
    for item in expiration["expired"]:
        fp = PROJECT_DIR / item["file"]
        if fp.exists():
            content = fp.read_text(encoding="utf-8")
            warning = f"⚠️ 此记忆最后更新于 {item['updated']}，可能已过期\n"
            if not content.startswith(warning):
                fp.write_text(warning + content, encoding="utf-8")

    # 自动修复：归档超过 180 天的文件
    for item in expiration["for_archive"]:
        fp = PROJECT_DIR / item["file"]
        if fp.exists():
            ARCHIVED_DIR.mkdir(parents=True, exist_ok=True)
            dest = ARCHIVED_DIR / fp.name
            fp.rename(dest)

    return {
        "skipped": False,
        "consistency": consistency,
        "format": format_result,
        "expiration": expiration,
        "duplicates": duplicates,
        "auto_fixed": {
            "marked_expired": len(expiration["expired"]),
            "archived": len(expiration["for_archive"]),
        },
    }


# ══════════════════════════════════════════════════════════════
# Step 6: 周报生成（周一执行）
# ══════════════════════════════════════════════════════════════

def step_generate_report(health: dict, token: dict) -> dict:
    """生成周报 Markdown（周一执行）"""
    if not is_monday:
        return {"skipped": True, "reason": "非周一，跳过周报生成"}

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{today}_health.md"

    # 统计记忆数量
    global_count = len(list(GLOBAL_DIR.rglob("*.md"))) if GLOBAL_DIR.exists() else 0
    users_count = len(list(USERS_DIR.rglob("*.md"))) if USERS_DIR.exists() else 0
    groups_count = len(list(GROUPS_DIR.rglob("*.md"))) if GROUPS_DIR.exists() else 0

    ts = token.get("token_summary", {})
    total_tokens = ts.get("total_tokens", 0)

    # 准备格式状态
    con_ok = health.get('consistency', {}).get('ok')
    fmt_ok = health.get('format', {}).get('ok')
    fmt_count = health.get('format', {}).get('invalid_count', 0)
    exp_count = health.get('expiration', {}).get('expired_count', 0)
    arch_count = health.get('expiration', {}).get('archive_count', 0)
    dup_count = health.get('duplicates', {}).get('duplicate_count', 0)

    lines = [
        f"# 系统健康报告 — {today}",
        "",
        "## 记忆统计",
        "| 指标 | 数量 |",
        "|------|:---:|",
        f"| 公共记忆（_global/） | {global_count} |",
        f"| 用户画像（users/） | {users_count} |",
        f"| 群画像（groups/） | {groups_count} |",
        f"| 过期记忆（>90天已标记） | {exp_count} |",
        f"| 归档记忆（>180天已归档） | {arch_count} |",
        "",
        "## Token 用量",
        f"| 今日总消耗 | {total_tokens:,} token |",
        "",
        "## 巡检结果",
        f"- 一致性：{'✅ OK' if con_ok else '⚠️ 发现问题'}",
        f"- 格式校验：{'✅ OK' if fmt_ok else f'⚠️ {fmt_count} 个文件'}",
        f"- 过期标记：{exp_count} 个",
        f"- 重复检测：{dup_count} 组",
        "",
    ]

    # 如果有需关注的问题
    issues = []
    if not health.get("consistency", {}).get("ok"):
        issues.extend(health["consistency"].get("issues", []))
    if not health.get("format", {}).get("ok"):
        issues.append(f"格式问题: {health['format']['invalid_count']} 个文件需修复")
    if health.get("duplicates", {}).get("duplicate_count", 0) > 0:
        issues.append(f"重复内容: {health['duplicates']['duplicate_count']} 组需合并")

    if issues:
        lines.append("## ⚠️ 需关注")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {"skipped": False, "report_path": str(report_path), "lines": len(lines)}


# ══════════════════════════════════════════════════════════════
# Step 1-2 的辅助：提供 AI 需要的数据
# ══════════════════════════════════════════════════════════════

def step_context_snapshot() -> dict:
    """提供当前上下文快照，供 AI 判断是否需要更新 SESSION_LAST / SHORT_TERM"""
    session_info = {}
    if SESSION_LAST.exists():
        try:
            session_info["session_last_size"] = len(SESSION_LAST.read_text(encoding="utf-8"))
        except Exception:
            session_info["session_last_size"] = 0
    if SHORT_TERM.exists():
        try:
            st_content = SHORT_TERM.read_text(encoding="utf-8")
            session_info["short_term_size"] = len(st_content)
            # 粗略估算 token 数（中文 ~1.5, 英文 ~1.3）
            chinese = len(re.findall(r'[一-鿿㐀-䶿]', st_content))
            other = len(st_content) - chinese
            session_info["short_term_tokens_est"] = int(chinese * 1.5 + other * 0.4)
        except Exception:
            session_info["short_term_size"] = 0

    return session_info


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

def run_all() -> dict:
    """执行全部自动化步骤"""
    output: dict[str, Any] = {
        "timestamp": datetime.now(TZ).isoformat(),
        "date": today,
        "is_monday": is_monday,
    }

    # Step 0: Token 统计
    output["step0_token"] = step_token_stats()

    # Step 2: lark-cli 登录态
    output["step2_lark_auth"] = step_lark_auth()

    # Step 3: Pending 检查
    output["step3_pending"] = step_pending_check()

    # Step 4: 晋升队列预筛选
    output["step4_promotion"] = step_promotion_queue()

    # Step 5: 健康巡检（周一）
    output["step5_health"] = step_health_check()

    # Step 6: 周报（周一）
    output["step6_report"] = step_generate_report(
        output["step5_health"],
        output["step0_token"],
    )

    # 上下文快照（供 AI 判断 Step 1-2）
    output["context_snapshot"] = step_context_snapshot()

    # ── 需要 AI 处理的事项 ──
    ai_tasks = []

    # lark-cli 登录态异常 → AI 直接提示用户重新授权（不要自己去扫码）
    la = output["step2_lark_auth"]
    if la.get("checked") and not la.get("ok"):
        ai_tasks.append({
            "task": "lark_relogin",
            "detail": "lark-cli 登录态异常：" + "；".join(la.get("problems") or [])[:160],
            "data": la,
        })

    # Pending 超时 → AI 需检查是否已回复并代发
    if output["step3_pending"]["overdue"]:
        ai_tasks.append({
            "task": "pending_reply_check",
            "detail": f"{len(output['step3_pending']['overdue'])} 条超时 pending 需 AI 检查回复状态",
            "data": output["step3_pending"]["overdue"],
        })

    # 晋升队列 → AI 需提炼内容
    if output["step4_promotion"]["promotable"]:
        ai_tasks.append({
            "task": "promote_memories",
            "detail": f"{len(output['step4_promotion']['promotable'])} 条话题待晋升，需 AI 提炼内容写入长期记忆",
            "data": output["step4_promotion"]["promotable"],
        })

    # 会话摘要 → AI 需判断是否需要更新 SESSION_LAST
    ai_tasks.append({
        "task": "update_session_context",
        "detail": "AI 检查 SESSION_LAST.md / SHORT_TERM.md 是否需要更新",
        "data": output["context_snapshot"],
    })

    # 健康巡检修复 → AI 需处理格式问题
    if not output["step5_health"].get("skipped") and not output["step5_health"].get("format", {}).get("ok"):
        ai_tasks.append({
            "task": "fix_memory_format",
            "detail": f"{output['step5_health']['format']['invalid_count']} 个文件格式需修复",
            "data": output["step5_health"]["format"]["invalid"],
        })

    output["ai_tasks"] = ai_tasks
    return output


def format_output(result: dict, json_fmt: bool = False) -> str:
    """格式化输出"""
    if json_fmt:
        return json.dumps(result, ensure_ascii=False, indent=2)

    lines = ["=" * 50, "🫀 心跳巡检 — 自动化脚本", "=" * 50, ""]

    # Token
    ts = result["step0_token"].get("token_summary", {})
    if ts:
        lines.append(f"📊 Token 用量: {ts.get('total_tokens', 0):,} token ({ts.get('sessions', 0)} 会话)")

    # lark-cli 登录态
    la = result.get("step2_lark_auth") or {}
    if la.get("checked"):
        mark = "✅" if la.get("ok") else "⚠️"
        detail = (f"refresh 剩 {la['refresh_expires_in_days']} 天"
                  if "refresh_expires_in_days" in la else la.get("error", ""))
        lines.append(f"{mark} lark-cli: {la.get('user') or '未登录'} / {detail}")
    elif la.get("error"):
        lines.append(f"⚠️ lark-cli: {la['error']}")

    # Pending
    p = result["step3_pending"]
    lines.append(f"📋 Pending: {p['pending_count']} 条，超时 {len(p['overdue'])} 条")

    # 晋升
    pm = result["step4_promotion"]
    if not pm["empty"]:
        lines.append(f"📈 晋升队列: {len(pm['entries'])} 条，≥2次可晋升 {pm['promotable_count']} 条")

    # 健康
    h = result["step5_health"]
    if not h.get("skipped"):
        lines.append(f"🏥 健康巡检: 一致性={'✅' if h['consistency']['ok'] else '⚠️'}, "
                     f"格式={'✅' if h['format']['ok'] else '⚠️'}, "
                     f"过期标记={h['expiration']['expired_count']}, "
                     f"归档={h['expiration']['archive_count']}")

    # 周报
    r = result["step6_report"]
    if not r.get("skipped"):
        lines.append(f"📝 周报: {r.get('report_path', 'N/A')}")

    # AI 任务
    lines.append("")
    lines.append("-" * 50)
    lines.append(f"🤖 AI 需处理 {len(result['ai_tasks'])} 项：")
    for t in result["ai_tasks"]:
        lines.append(f"  [{t['task']}] {t['detail']}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="心跳巡检脚本")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出（供 AI 消费）")
    parser.add_argument("--step", type=int, choices=[0, 2, 3, 4, 5, 6], help="执行单步")
    args = parser.parse_args()

    if args.step is not None:
        steps = {
            0: step_token_stats,
            2: step_lark_auth,
            3: step_pending_check,
            4: step_promotion_queue,
            5: step_health_check,
            6: lambda: step_generate_report(step_health_check(), step_token_stats()),
        }
        result = steps[args.step]()
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else format_output(result, json_fmt=False))
    else:
        result = run_all()
        print(format_output(result, json_fmt=args.json))


if __name__ == "__main__":
    main()
