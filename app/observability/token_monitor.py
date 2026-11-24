#!/usr/bin/env python3
"""
Token 用量监控脚本
===================
从 cc-connect session 文件中提取消息文本，估算 token 消耗量。

用法：
  python token_monitor.py scan              # 增量扫描 session 文件，追加日志
  python token_monitor.py scan --full       # 全量重新扫描
  python token_monitor.py report            # 输出今日汇总报告
  python token_monitor.py report --week     # 本周汇总
  python token_monitor.py report --month    # 本月汇总
  python token_monitor.py report --detail   # 按 session 明细
  python token_monitor.py report --json     # JSON 格式输出（供脚本消费）
  python token_monitor.py watch             # 持续监控（每 10 分钟 scan 一次）

数据源：~/.cc-connect/sessions/dev-bot_*.json
日志输出：logs/eval/token-usage/
"""

import argparse
import json
import sys
import time

# 修复 Windows GBK 控制台编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 路径常量 ────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # feishu-agent-platform/
CC_CONNECT_DIR = Path.home() / ".cc-connect" / "sessions"
OUTPUT_DIR = PROJECT_DIR / "logs" / "eval" / "token-usage"
CURSOR_FILE = OUTPUT_DIR / ".cursor.json"
SUMMARY_FILE = OUTPUT_DIR / "summary.json"
TZ = timezone(timedelta(hours=8))  # CST

# 日志聚合器路径
LOGS_DIR = PROJECT_DIR / "logs"
SESSION_LOGS_DIR = LOGS_DIR / "sessions"
DAILY_LOGS_DIR = LOGS_DIR / "daily"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 找到本项目对应的 Claude sessions 目录
def _find_project_dir() -> Path | None:
    """查找本项目的 Claude sessions 目录"""
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    # 目录名格式: D--Users-devuser-wang-IdeaProjects-feishu-agent-platform
    for d in CLAUDE_PROJECTS_DIR.iterdir():
        if d.is_dir() and "feishu-agent-platform" in d.name:
            return d
    return None

# ── Token 计数器 ────────────────────────────────────────
# 优先使用 tiktoken，不可用时降级为字符估算

_tokenizer = None
_tokenizer_available = False


def _init_tokenizer():
    """尝试加载 tiktoken cl100k_base 编码器"""
    global _tokenizer, _tokenizer_available
    if _tokenizer is not None:
        return
    try:
        import tiktoken
        _tokenizer = tiktoken.get_encoding("cl100k_base")
        _tokenizer_available = True
    except Exception:
        _tokenizer_available = False


def count_tokens(text: str) -> int:
    """估算文本的 token 数"""
    _init_tokenizer()
    if _tokenizer_available:
        return len(_tokenizer.encode(text))

    # ── 降级：字符估算法 ──
    # 中文字符 ≈ 1.5 token，英文单词 ≈ 1.3 token
    # 混合文本：按字符类型分别统计后加权
    import re
    chinese_chars = len(re.findall(r'[一-鿿㐀-䶿]', text))
    # 英文单词（连续的字母/数字）
    english_words = len(re.findall(r'[a-zA-Z0-9]+', text))
    # 其他字符（标点、符号、换行等）≈ 1 token
    other = max(0, len(text) - chinese_chars - sum(len(w) for w in re.findall(r'[a-zA-Z0-9]+', text)))
    return int(chinese_chars * 1.5 + english_words * 1.3 + other * 0.5) + 1


def count_tokens_batch(texts: list[str]) -> list[int]:
    """批量估算 token 数（tiktoken 可一次编码多条）"""
    _init_tokenizer()
    if _tokenizer_available:
        return [len(tokens) for tokens in _tokenizer.encode_batch(texts)]
    return [count_tokens(t) for t in texts]


# ── Session 文件解析 ────────────────────────────────────

def find_session_files() -> list[Path]:
    """查找所有 cc-connect session 文件"""
    if not CC_CONNECT_DIR.exists():
        return []
    return sorted(CC_CONNECT_DIR.glob("dev-bot_*.json"))


def parse_session_file(filepath: Path) -> dict:
    """解析 session 文件，返回 sessions 字典"""
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sessions", {})
    except Exception as e:
        print(f"⚠️ 无法解析 {filepath}: {e}", file=sys.stderr)
        return {}


def load_cursor() -> dict:
    """加载扫描游标（记录每个 session 文件已处理到哪个消息索引）"""
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cursor(cursor: dict):
    """保存扫描游标"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 日志写入 ────────────────────────────────────────────

def daily_log_path(date: datetime | None = None) -> Path:
    """获取当日的 NDJSON 日志文件路径"""
    if date is None:
        date = datetime.now(TZ)
    return OUTPUT_DIR / f"{date.strftime('%Y-%m-%d')}.jsonl"


def append_log(entries: list[dict]):
    """追加日志条目到当日 NDJSON 文件"""
    if not entries:
        return
    path = daily_log_path()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_summary() -> dict:
    """加载累计汇总"""
    if SUMMARY_FILE.exists():
        try:
            return json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_summary(summary: dict):
    """保存累计汇总"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def update_summary(session_summaries: list[dict]):
    """将 session 汇总合并到 summary.json"""
    s = load_summary()

    # 按日期索引
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if "daily" not in s:
        s["daily"] = {}

    for ss in session_summaries:
        day = ss.get("date", today)
        if day not in s["daily"]:
            s["daily"][day] = {"sessions": 0, "total_tokens": 0, "user_tokens": 0, "assistant_tokens": 0, "total_msgs": 0}
        s["daily"][day]["sessions"] += 1
        s["daily"][day]["total_tokens"] += ss.get("total_tokens_est", 0)
        s["daily"][day]["user_tokens"] += ss.get("user_tokens", 0)
        s["daily"][day]["assistant_tokens"] += ss.get("assistant_tokens", 0)
        s["daily"][day]["total_msgs"] += ss.get("total_msgs", 0)

    save_summary(s)


# ── scan 命令 ────────────────────────────────────────────

def scan(full: bool = False):
    """扫描 session 文件，增量统计 token 消耗"""
    files = find_session_files()
    if not files:
        print("📭 未找到 session 文件 (~/.cc-connect/sessions/dev-bot_*.json)")
        return

    cursor = {} if full else load_cursor()

    # 全量扫描时清空当日日志避免重复
    if full:
        today_path = daily_log_path()
        if today_path.exists():
            today_path.unlink()

    all_entries = []
    all_session_summaries = []
    now = datetime.now(TZ)

    for fp in files:
        fkey = fp.name
        prev_count = cursor.get(fkey, 0) if not full else 0

        sessions = parse_session_file(fp)
        for sid, sdata in sessions.items():
            history = sdata.get("history", [])
            if not history:
                continue

            # 增量：只处理新消息
            if prev_count >= len(history):
                continue

            new_msgs = history[prev_count:]
            # 批量估算 token
            texts = [m.get("content", "") for m in new_msgs]
            token_counts = count_tokens_batch(texts)

            user_tokens = 0
            assistant_tokens = 0
            msg_count = 0

            for i, (msg, tc) in enumerate(zip(new_msgs, token_counts, strict=False)):
                role = msg.get("role", "unknown")
                ts = msg.get("timestamp", now.isoformat())
                entry = {
                    "ts": ts,
                    "session": sid,
                    "file": fkey,
                    "msg_role": role,
                    "msg_len": len(texts[i]),
                    "tokens_est": tc,
                }
                all_entries.append(entry)

                if role == "user":
                    user_tokens += tc
                else:
                    assistant_tokens += tc
                msg_count += 1

            # session 汇总
            if msg_count > 0:
                first_ts = new_msgs[0].get("timestamp", "")
                last_ts = new_msgs[-1].get("timestamp", "")
                duration_min = 0
                try:
                    t1 = datetime.fromisoformat(first_ts)
                    t2 = datetime.fromisoformat(last_ts)
                    duration_min = round((t2 - t1).total_seconds() / 60, 1)
                except Exception:
                    pass

                session_summary = {
                    "type": "session_summary",
                    "ts": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                    "session": sid,
                    "file": fkey,
                    "total_msgs": msg_count,
                    "total_tokens_est": user_tokens + assistant_tokens,
                    "user_tokens": user_tokens,
                    "assistant_tokens": assistant_tokens,
                    "duration_min": duration_min,
                    "agent_session_id": sdata.get("agent_session_id", ""),
                }
                all_entries.append(session_summary)
                all_session_summaries.append(session_summary)

        # 更新游标
        # 计算该文件所有 session 的总消息数
        total_msgs = sum(len(sd.get("history", [])) for sd in sessions.values())
        cursor[fkey] = total_msgs

    # 写入
    append_log(all_entries)
    save_cursor(cursor)
    update_summary(all_session_summaries)

    if all_entries:
        total_new = sum(
            e.get("total_tokens_est", 0)
            for e in all_entries
            if e.get("type") == "session_summary"
        )
        print(f"✅ 扫描完成：{len(files)} 个文件，新增 {len([e for e in all_entries if e.get('type') != 'session_summary'])} 条消息日志，{len(all_session_summaries)} 个 session 汇总")
        print(f"  本次新增 token 估算：{total_new:,}")
    else:
        print("✅ 扫描完成：无新消息")


# ── report 命令 ──────────────────────────────────────────

def read_logs(days: int = 1) -> list[dict]:
    """读取最近 N 天的日志"""
    entries = []
    now = datetime.now(TZ)
    for i in range(days):
        date = now - timedelta(days=i)
        path = daily_log_path(date)
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").strip().split("\n"):
                    if line:
                        entries.append(json.loads(line))
            except Exception:
                pass
    return entries


def report(period: str = "day", detail: bool = False, json_fmt: bool = False):
    """生成用量报告"""
    _init_tokenizer()  # 确保 tokenizer 状态正确
    if period == "day":
        days = 1
        label = datetime.now(TZ).strftime("%Y-%m-%d")
    elif period == "week":
        days = 7
        label = f"{(datetime.now(TZ) - timedelta(days=7)).strftime('%Y-%m-%d')} ~ {datetime.now(TZ).strftime('%Y-%m-%d')}"
    elif period == "month":
        days = 30
        label = f"{(datetime.now(TZ) - timedelta(days=30)).strftime('%Y-%m-%d')} ~ {datetime.now(TZ).strftime('%Y-%m-%d')}"
    else:
        days = 1
        label = datetime.now(TZ).strftime("%Y-%m-%d")

    entries = read_logs(days)
    summaries = [e for e in entries if e.get("type") == "session_summary"]

    total_tokens = sum(s.get("total_tokens_est", 0) for s in summaries)
    user_tokens = sum(s.get("user_tokens", 0) for s in summaries)
    assistant_tokens = sum(s.get("assistant_tokens", 0) for s in summaries)
    total_msgs = sum(s.get("total_msgs", 0) for s in summaries)
    session_count = len(summaries)

    # 按 session 分组
    session_groups = defaultdict(list)
    for s in summaries:
        k = s.get("session", "?")
        session_groups[k].append(s)

    if json_fmt:
        output = {
            "period": period,
            "label": label,
            "total_tokens_est": total_tokens,
            "user_tokens": user_tokens,
            "assistant_tokens": assistant_tokens,
            "total_msgs": total_msgs,
            "sessions": session_count,
            "avg_per_session": round(total_tokens / session_count) if session_count else 0,
            "tokenizer": "tiktoken" if _tokenizer_available else "char_estimation",
            "top_sessions": [
                {
                    "session": k,
                    "tokens": sum(x.get("total_tokens_est", 0) for x in v),
                    "msgs": sum(x.get("total_msgs", 0) for x in v),
                    "count": len(v),
                }
                for k, v in sorted(session_groups.items(), key=lambda x: sum(y.get("total_tokens_est", 0) for y in x[1]), reverse=True)[:10]
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 文本报告
    print(f"📊 Token 用量报告 — {label}")
    print("=" * 50)
    print(f"  总消耗：     {total_tokens:>10,} token (估算)")
    print(f"  ├─ 用户：   {user_tokens:>10,} token")
    print(f"  └─ 助手：   {assistant_tokens:>10,} token")
    print(f"  消息总数：   {total_msgs:>10,} 条")
    print(f"  会话数：     {session_count:>10,}")
    if session_count > 0:
        print(f"  平均/会话：  {total_tokens // session_count:>10,} token")
    print(f"  统计方式：   {'tiktoken (精确)' if _tokenizer_available else '字符估算'}")
    print()

    if detail and session_groups:
        print("📋 Session 明细")
        print("-" * 50)
        rows = []
        for k, v in sorted(session_groups.items(), key=lambda x: sum(y.get("total_tokens_est", 0) for y in x[1]), reverse=True):
            st = sum(x.get("total_tokens_est", 0) for x in v)
            sm = sum(x.get("total_msgs", 0) for x in v)
            sd = max((x.get("duration_min", 0) or 0) for x in v)
            rows.append((k, st, sm, sd, len(v)))

        print(f"  {'Session':<8} {'Token':>8} {'消息':>6} {'时长':>8} {'扫描次数':>8}")
        for r in rows[:20]:
            print(f"  {r[0]:<8} {r[1]:>8,} {r[2]:>6} {r[3]:>6}分 {r[4]:>7}")
        if len(rows) > 20:
            print(f"  ... 还有 {len(rows) - 20} 个 session")

    # 估算成本（参考 Claude Opus 定价：$15/1M input, $75/1M output）
    # 简化为平均 $20/1M token
    estimated_cost = total_tokens / 1_000_000 * 20
    if estimated_cost > 0.01:
        print(f"\n💵 估算成本：约 ${estimated_cost:.4f} (基于 $20/1M token)")


# ── watch 命令 ──────────────────────────────────────────

def watch(interval: int = 10):
    """持续监控模式"""
    print(f"🔍 开始持续监控（每 {interval} 分钟扫描一次）...")
    print(f"   日志目录：{OUTPUT_DIR}")
    print("   按 Ctrl+C 退出")
    try:
        while True:
            scan()
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        print("\n👋 监控已停止")


# ── summarize 命令：JSONL → 人类可读日志 ──────────────────

def _parse_jsonl(filepath: Path) -> list[dict]:
    """解析 JSONL 文件，返回事件列表"""
    events = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        print(f"⚠️ 无法解析 {filepath}: {e}", file=sys.stderr)
    return events


def _extract_session_summary(events: list[dict], session_id: str) -> dict | None:
    """从事件列表中提取会话摘要"""
    user_msgs = [e for e in events if e.get("type") == "user"]
    assistant_msgs = [e for e in events if e.get("type") == "assistant"]

    if not user_msgs and not assistant_msgs:
        return None

    # 时间范围
    timestamps = [e.get("timestamp", "") for e in events if e.get("timestamp")]
    first_ts = timestamps[0] if timestamps else ""
    last_ts = timestamps[-1] if timestamps else ""

    # 用户提问
    questions = []
    for u in user_msgs:
        content = u.get("message", {}).get("content", "")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            content = " ".join(texts)
        if content and len(content) > 3:
            questions.append(content[:200])

    # Assistant 回复
    replies = []
    for a in assistant_msgs:
        for c in a.get("message", {}).get("content", []):
            if c.get("type") == "text":
                txt = c.get("text", "")
                if txt and len(txt) > 10:
                    replies.append(txt[:300])

    # Token 汇总
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    models = set()

    for a in assistant_msgs:
        usage = a.get("message", {}).get("usage", {})
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cache_read += usage.get("cache_read_input_tokens", 0)
        total_cache_create += usage.get("cache_creation_input_tokens", 0)
        model = a.get("message", {}).get("model", "")
        if model:
            models.add(model)

    # 工具调用
    tool_calls: dict[str, int] = {}
    for a in assistant_msgs:
        for c in a.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                name = c.get("name", "?")
                tool_calls[name] = tool_calls.get(name, 0) + 1

    # 计算耗时
    duration_sec = 0
    try:
        t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        duration_sec = (t2 - t1).total_seconds()
    except Exception:
        pass

    return {
        "session_id": session_id,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "date": first_ts[:10] if first_ts else "",
        "user_msgs": len(user_msgs),
        "assistant_msgs": len(assistant_msgs),
        "questions": questions,
        "replies": replies,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read": total_cache_read,
        "total_cache_create": total_cache_create,
        "total_tokens": total_input + total_output,
        "models": sorted(models),
        "tool_calls": tool_calls,
        "duration_sec": duration_sec,
    }


def _write_session_log(summary: dict):
    """写入单次会话的 Markdown 摘要"""
    date = summary["date"] or "unknown"
    out_dir = SESSION_LOGS_DIR / date
    out_dir.mkdir(parents=True, exist_ok=True)

    # 文件名：小时分钟_摘要
    ts = summary["first_ts"]
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
        time_part = dt.strftime("%H%M")
    except Exception:
        time_part = "0000"

    first_q = summary["questions"][0][:50] if summary["questions"] else "无提问"
    # 清理文件名
    safe_q = "".join(c for c in first_q if c.isalnum() or c in " _-")[:30].strip()
    filename = f"{time_part}_{safe_q or 'session'}.md"
    filepath = out_dir / filename

    dur_min = round(summary["duration_sec"] / 60, 1)
    tools_str = ", ".join(f"{k}({v})" for k, v in sorted(summary["tool_calls"].items(), key=lambda x: -x[1])[:10])

    lines = [
        f"# 会话摘要 — {summary['date']} {time_part}",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        f"| Session | `{summary['session_id'][:8]}` |",
        f"| 时间 | {summary['first_ts']} ~ {summary['last_ts']} |",
        f"| 耗时 | {dur_min} 分钟 |",
        f"| 消息数 | 用户 {summary['user_msgs']} / 助手 {summary['assistant_msgs']} |",
        f"| 模型 | {', '.join(summary['models']) if summary['models'] else '未知'} |",
        f"| 输入 Token | {summary['total_input_tokens']:,} |",
        f"| 输出 Token | {summary['total_output_tokens']:,} |",
        f"| 缓存命中 | {summary['total_cache_read']:,} |",
        f"| **总 Token** | **{summary['total_tokens']:,}** |",
        f"| 工具调用 | {tools_str or '无'} |",
        "",
        "## 用户提问",
    ]

    for i, q in enumerate(summary["questions"][:10]):
        lines.append(f"{i+1}. {q}")
    if not summary["questions"]:
        lines.append("（无文本提问）")

    lines.append("")
    lines.append("## 助手回复")
    for i, r in enumerate(summary["replies"][:10]):
        lines.append(f"### 回复 {i+1}")
        lines.append(r)
        lines.append("")
    if not summary["replies"]:
        lines.append("（无文本回复）")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


def _write_daily_json(sessions: list[dict], date: str):
    """写入每日结构化汇总 JSON"""
    DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DAILY_LOGS_DIR / f"{date}.json"

    total_tokens = sum(s["total_tokens"] for s in sessions)
    total_input = sum(s["total_input_tokens"] for s in sessions)
    total_output = sum(s["total_output_tokens"] for s in sessions)
    # 估算成本
    cost_est = total_input / 1_000_000 * 3 + total_output / 1_000_000 * 15  # Claude Opus 定价参考

    daily = {
        "date": date,
        "sessions": len(sessions),
        "total_tokens": total_tokens,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "cost_est_usd": round(cost_est, 4),
        "sessions_detail": [
            {
                "id": s["session_id"][:8],
                "first_ts": s["first_ts"],
                "user_msgs": s["user_msgs"],
                "assistant_msgs": s["assistant_msgs"],
                "tokens": s["total_tokens"],
                "tools": s["tool_calls"],
                "models": s["models"],
                "first_question": s["questions"][0][:100] if s["questions"] else "",
            }
            for s in sessions
        ],
    }
    filepath.write_text(json.dumps(daily, ensure_ascii=False, indent=2), encoding="utf-8")
    return filepath


def summarize(session_id: str | None = None, date: str | None = None):
    """从 Claude JSONL 文件生成会话日志

    Args:
        session_id: 指定 session 文件名关键词（如 'd559ca17'）
        date: 指定日期过滤（如 '2026-07-30'）
    """
    proj_dir = _find_project_dir()
    if not proj_dir:
        print("❌ 未找到 Claude sessions 目录")
        return

    jsonl_files = sorted(proj_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"📭 未找到 JSONL 文件 ({proj_dir})")
        return

    # 过滤
    if session_id:
        jsonl_files = [f for f in jsonl_files if session_id in f.stem]
        if not jsonl_files:
            print(f"📭 未找到匹配 '{session_id}' 的 session 文件")
            return

    all_summaries = []
    for fp in jsonl_files:
        sid = fp.stem[:36]  # UUID 部分
        print(f"  解析 {fp.name} ...", end=" ", flush=True)

        events = _parse_jsonl(fp)
        summary = _extract_session_summary(events, sid)
        if summary is None or summary["total_tokens"] == 0:
            print("跳过（无有效数据）")
            continue

        # 日期过滤
        if date and summary["date"] != date:
            print(f"跳过（日期={summary['date']}）")
            continue

        # 写入 Markdown（副作用在此，返回路径暂不消费）
        _md_path = _write_session_log(summary)
        all_summaries.append(summary)
        print(f"✅ {summary['total_tokens']:,} token | {len(summary['tool_calls'])} 工具")

    if not all_summaries:
        print("📭 没有需要汇总的会话")
        return

    # 按日期分组写 daily JSON
    by_date: dict[str, list] = defaultdict(list)
    for s in all_summaries:
        by_date[s["date"]].append(s)

    for d, sessions in by_date.items():
        json_path = _write_daily_json(sessions, d)
        print(f"  📊 日汇总: {json_path}")

    print(f"\n✅ 完成：{len(all_summaries)} 个会话 → {SESSION_LOGS_DIR}")
    print(f"   Markdown 日志: {SESSION_LOGS_DIR}")
    print(f"   日汇总 JSON:   {DAILY_LOGS_DIR}")

def main():
    parser = argparse.ArgumentParser(
        description="Token 用量监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python token_monitor.py scan              # 增量扫描
  python token_monitor.py scan --full       # 全量重新扫描
  python token_monitor.py report            # 今日报告
  python token_monitor.py report --week     # 本周报告
  python token_monitor.py report --detail   # 详细明细
  python token_monitor.py report --json     # JSON 输出
  python token_monitor.py summarize         # 从 JSONL 生成人类可读日志
  python token_monitor.py summarize --session d559ca17  # 指定会话
  python token_monitor.py summarize --date 2026-07-30   # 指定日期
  python token_monitor.py watch             # 持续监控
        """,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_scan = sub.add_parser("scan", help="扫描 session 文件")
    p_scan.add_argument("--full", action="store_true", help="全量重新扫描（忽略游标）")

    p_report = sub.add_parser("report", help="生成用量报告")
    p_report.add_argument("--week", action="store_true", help="本周汇总")
    p_report.add_argument("--month", action="store_true", help="本月汇总")
    p_report.add_argument("--detail", action="store_true", help="按 session 明细")
    p_report.add_argument("--json", action="store_true", help="JSON 格式输出")

    p_watch = sub.add_parser("watch", help="持续监控")
    p_watch.add_argument("--interval", type=int, default=10, help="扫描间隔（分钟），默认 10")

    p_summarize = sub.add_parser("summarize", help="从 JSONL 生成人类可读日志")
    p_summarize.add_argument("--session", type=str, help="指定 session ID（关键词匹配）")
    p_summarize.add_argument("--date", type=str, help="指定日期过滤（YYYY-MM-DD）")

    args = parser.parse_args()

    if args.command == "scan":
        scan(full=args.full)
    elif args.command == "report":
        period = "day"
        if args.week:
            period = "week"
        elif args.month:
            period = "month"
        report(period=period, detail=args.detail, json_fmt=args.json)
    elif args.command == "summarize":
        summarize(session_id=args.session, date=args.date)
    elif args.command == "watch":
        watch(interval=args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
