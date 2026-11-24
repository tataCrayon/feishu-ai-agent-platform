#!/usr/bin/env python3
"""
平台能力安全守卫
=================
统一安全过滤、权限验证、审计日志。

用法：
  python app/guardrails/security_guard.py --check-sql "DROP TABLE users"
  python app/guardrails/security_guard.py --user ou_xxx --command "/deploy run" --json
  python app/guardrails/security_guard.py --audit-log --read-today
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

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
WHITELIST_FILE = PROJECT_DIR / "bot" / "todos" / "whitelist.json"
AUDIT_DIR = PROJECT_DIR / "logs" / "audit"

TZ = timezone(timedelta(hours=8))

# ══════════════════════════════════════════════════════════════
# SQL 安全引擎
# ══════════════════════════════════════════════════════════════

DANGEROUS_PATTERNS = [
    (r'\bDROP\s+(TABLE|DATABASE|INDEX|VIEW|PROCEDURE|FUNCTION|SCHEMA)\b', 'DROP'),
    (r'\bDELETE\s+FROM\b', 'DELETE'),
    (r'\bUPDATE\s+\w+\s+SET\b', 'UPDATE'),
    (r'\bINSERT\s+INTO\b', 'INSERT'),
    (r'\bALTER\s+(TABLE|DATABASE|SCHEMA)\b', 'ALTER'),
    (r'\bTRUNCATE\s+(TABLE\s+)?\w+\b', 'TRUNCATE'),
    (r'\bCREATE\s+(TABLE|DATABASE|INDEX|SCHEMA)\b', 'CREATE'),
    (r'\bGRANT\s+', 'GRANT'),
    (r'\bREVOKE\s+', 'REVOKE'),
    (r'\bREPLACE\s+INTO\b', 'REPLACE'),
    (r'\bRENAME\s+TABLE\b', 'RENAME'),
    (r'\bCALL\s+\w+\s*\(', 'CALL'),
    (r'\bEXEC\s+\w+', 'EXEC'),
    (r'\bEXECUTE\s+\w+', 'EXECUTE'),
]

# 敏感列名 → 脱敏方法
SENSITIVE_COLUMNS = {
    'password': 'mask',
    'passwd': 'mask',
    'secret': 'mask',
    'token': 'partial',
    'phone': 'phone_mask',
    'mobile': 'phone_mask',
    'id_card': 'id_card_mask',
    'idcard': 'id_card_mask',
    'identity': 'id_card_mask',
    'email': 'email_mask',
    'bank_card': 'bank_card_mask',
}


def _sql_normalize(sql: str) -> str:
    """去除注释、多余空白，便于匹配"""
    sql = re.sub(r'--.*$', '', sql, flags=re.MULTILINE)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    return ' '.join(sql.split()).strip().upper()


def check_sql(sql: str, write_allowed: bool = False) -> dict:
    """检查 SQL 安全性，返回 {ok, violations, masked_sql}"""
    if not sql or not sql.strip():
        return {"ok": False, "violations": [{"pattern": "EMPTY", "matched": "空 SQL"}], "masked_sql": ""}

    normalized = _sql_normalize(sql)
    violations = []

    # 如果允许写操作，放宽检查（仍拦截 DROP/TRUNCATE）
    skip_patterns = set()
    if write_allowed:
        skip_patterns = {'DROP', 'TRUNCATE', 'ALTER', 'CREATE', 'GRANT', 'REVOKE', 'RENAME', 'CALL', 'EXEC', 'EXECUTE'}

    for pattern, label in DANGEROUS_PATTERNS:
        if label in skip_patterns:
            continue
        m = re.search(pattern, normalized, re.IGNORECASE)
        if m:
            violations.append({"pattern": label, "matched": m.group(0)})

    return {
        "ok": len(violations) == 0,
        "violations": violations,
        "blocked": len(violations) > 0,
        "write_allowed": write_allowed,
    }


def mask_row(row: dict, columns: list[str]) -> dict:
    """对敏感列脱敏"""
    if not row or not columns:
        return row
    masked = dict(row)
    for col in columns:
        lower = col.lower()
        rule = None
        for sensitive, method in SENSITIVE_COLUMNS.items():
            if sensitive in lower:
                rule = method
                break
        if rule and col in masked and masked[col] is not None:
            val = str(masked[col])
            if rule == 'mask':
                masked[col] = '***'
            elif rule == 'phone_mask':
                masked[col] = val[:3] + '****' + val[-4:] if len(val) >= 11 else '****'
            elif rule == 'id_card_mask':
                masked[col] = val[:3] + '**********' + val[-4:] if len(val) >= 18 else '****'
            elif rule == 'email_mask':
                parts = val.split('@')
                masked[col] = parts[0][:2] + '***@' + parts[1] if len(parts) == 2 else '***'
            elif rule == 'bank_card_mask':
                masked[col] = '****' + val[-4:] if len(val) >= 8 else '****'
            elif rule == 'partial':
                masked[col] = val[:4] + '...' if len(val) > 8 else '***'
    return masked


# ══════════════════════════════════════════════════════════════
# 权限引擎
# ══════════════════════════════════════════════════════════════

def load_whitelist() -> dict:
    """加载白名单配置"""
    if WHITELIST_FILE.exists():
        try:
            return json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"users": {}, "groups": {}, "level_permissions": {}}


def get_user_level(sender_id: str, chat_id: str = "") -> int:
    """获取用户权限级别"""
    wl = load_whitelist()
    users = wl.get("users", {})

    if sender_id in users:
        return users[sender_id].get("level", 0)

    # 群默认级别
    groups = wl.get("groups", {})
    if chat_id in groups:
        return groups[chat_id].get("level", 0)

    return 0  # 默认游客


def get_permissions(level: int) -> dict:
    """获取指定级别的权限配置"""
    wl = load_whitelist()
    return wl.get("level_permissions", {}).get(str(level), {})


def check_command_permission(sender_id: str, command: str, args: str = "", chat_id: str = "") -> dict:
    """检查用户是否有权限执行某个命令"""
    level = get_user_level(sender_id, chat_id)
    perms = get_permissions(level)

    # 提取基础命令名（去掉参数）
    base_cmd = command.lstrip("/").split()[0].lower()

    # 安全检查
    checks = []

    if base_cmd in ("query", "db"):
        write_ok = perms.get("sql_write", False)
        if args:
            sql_check = check_sql(args, write_allowed=write_ok)
            checks.append({"type": "sql_safety", "result": sql_check})

    if base_cmd.startswith("deploy"):
        if "run" in base_cmd and not perms.get("deploy_write", False):
            return {
                "allowed": False,
                "reason": f"L{level} ({perms.get('label', '游客')}) 无发布执行权限，仅管理员可触发发布",
                "level": level,
                "label": perms.get("label", "游客"),
                "checks": checks,
            }
        if not perms.get("deploy_read", False) and base_cmd not in ("deploy",):
            return {
                "allowed": False,
                "reason": f"L{level} ({perms.get('label', '游客')}) 无发布查看权限",
                "level": level,
                "label": perms.get("label", "游客"),
                "checks": checks,
            }

    if base_cmd.startswith("config"):
        if "update" in base_cmd and not perms.get("config_write", False):
            return {
                "allowed": False,
                "reason": f"L{level} ({perms.get('label', '游客')}) 无配置修改权限",
                "level": level,
                "label": perms.get("label", "游客"),
                "checks": checks,
            }
        if not perms.get("config_read", False):
            return {
                "allowed": False,
                "reason": f"L{level} ({perms.get('label', '游客')}) 无配置查看权限",
                "level": level,
                "label": perms.get("label", "游客"),
                "checks": checks,
            }

    if base_cmd == "whitelist" and level < 2:
        return {
            "allowed": False,
            "reason": "仅管理员可管理白名单",
            "level": level,
            "label": perms.get("label", "游客"),
            "checks": checks,
        }

    return {
        "allowed": True,
        "level": level,
        "label": perms.get("label", "游客"),
        "sql_max_rows": perms.get("sql_max_rows", 20),
        "checks": checks,
    }


# ══════════════════════════════════════════════════════════════
# 白名单管理
# ══════════════════════════════════════════════════════════════

def add_user(admin_id: str, target_id: str, target_name: str, level: int, chat_id: str = "") -> dict:
    """管理员添加用户到白名单"""
    if get_user_level(admin_id, chat_id) < 2:
        return {"ok": False, "reason": "仅管理员可管理白名单"}

    if level >= 2:
        return {"ok": False, "reason": "不能添加其他管理员（仅管理员本人可为 L2）"}

    wl = load_whitelist()
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    wl.setdefault("users", {})[target_id] = {
        "name": target_name,
        "level": level,
        "added": today,
    }
    wl["updated"] = today

    WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WHITELIST_FILE.write_text(json.dumps(wl, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "user": target_id, "name": target_name, "level": level}


def list_users(admin_id: str, chat_id: str = "") -> dict:
    """管理员查看白名单"""
    if get_user_level(admin_id, chat_id) < 2:
        return {"ok": False, "reason": "仅管理员可查看白名单"}

    wl = load_whitelist()
    users = wl.get("users", {})
    groups = wl.get("groups", {})

    user_list = [
        {"id": uid, "name": info.get("name", "?"), "level": info.get("level", 0), "added": info.get("added", "?")}
        for uid, info in users.items()
    ]
    return {"ok": True, "users": user_list, "groups": groups}


# ══════════════════════════════════════════════════════════════
# 审计日志
# ══════════════════════════════════════════════════════════════

def write_audit_log(entry: dict):
    """写入审计日志（由 system prompt 中 AI 调用）"""
    date = datetime.now(TZ).strftime("%Y-%m-%d")
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = AUDIT_DIR / f"{date}.jsonl"

    entry["ts"] = datetime.now(TZ).isoformat()
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_log(date_str: str | None = None) -> list[dict]:
    """读取审计日志"""
    if date_str is None:
        date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    log_file = AUDIT_DIR / f"{date_str}.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def audit_stats(days: int = 7) -> dict:
    """审计日志统计"""
    now = datetime.now(TZ)
    by_user: dict[str, int] = {}
    by_command: dict[str, int] = {}
    total = 0
    errors = 0

    for i in range(days):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        for entry in read_audit_log(date):
            total += 1
            user = entry.get("user_name", entry.get("user", "?"))
            by_user[user] = by_user.get(user, 0) + 1
            cmd = entry.get("command", "?")
            by_command[cmd] = by_command.get(cmd, 0) + 1
            if entry.get("result") != "ok":
                errors += 1

    return {
        "days": days,
        "total": total,
        "errors": errors,
        "top_users": sorted(by_user.items(), key=lambda x: -x[1])[:10],
        "top_commands": sorted(by_command.items(), key=lambda x: -x[1])[:10],
    }


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="平台能力安全守卫")
    sub = parser.add_subparsers(dest="action")

    p_sql = sub.add_parser("check-sql", help="SQL 安全检查")
    p_sql.add_argument("sql", type=str, help="要检查的 SQL 语句")
    p_sql.add_argument("--write", action="store_true", help="允许写操作")

    p_perm = sub.add_parser("check-perm", help="权限验证")
    p_perm.add_argument("--user", type=str, required=True, help="发送者 open_id")
    p_perm.add_argument("--command", type=str, required=True, help="命令（如 /deploy run demo）")
    p_perm.add_argument("--chat", type=str, default="", help="群 chat_id")
    p_perm.add_argument("--json", action="store_true", help="JSON 输出")

    p_audit = sub.add_parser("audit", help="审计日志")
    p_audit.add_argument("--read-today", action="store_true", help="读今日日志")
    p_audit.add_argument("--stats", action="store_true", help="统计最近 7 天")
    p_audit.add_argument("--days", type=int, default=7, help="统计天数")

    p_wl = sub.add_parser("whitelist", help="白名单管理")
    sub_wl = p_wl.add_subparsers(dest="wl_action")

    p_add = sub_wl.add_parser("add", help="添加用户")
    p_add.add_argument("--admin", type=str, required=True, help="管理员 open_id")
    p_add.add_argument("--user", type=str, required=True, help="目标 open_id")
    p_add.add_argument("--name", type=str, required=True, help="用户名")
    p_add.add_argument("--level", type=int, choices=[0, 1], default=1, help="权限级别")

    p_list = sub_wl.add_parser("list", help="查看白名单")
    p_list.add_argument("--admin", type=str, required=True, help="管理员 open_id")

    args = parser.parse_args()

    if args.action == "check-sql":
        result = check_sql(args.sql, write_allowed=args.write)
        print("✅ SQL 安全" if result["ok"] else "⛔ SQL 被拦截")
        if not result["ok"]:
            for v in result["violations"]:
                print(f"  违规: {v['pattern']} → \"{v['matched']}\"")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == "check-perm":
        result = check_command_permission(args.user, args.command, args.command, args.chat)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("✅ 允许" if result["allowed"] else f"⛔ 拒绝: {result['reason']}")
            if "checks" in result:
                for c in result["checks"]:
                    if c["type"] == "sql_safety" and not c["result"]["ok"]:
                        for v in c["result"]["violations"]:
                            print(f"  SQL违规: {v['pattern']}")

    elif args.action == "audit":
        if args.stats:
            stats = audit_stats(args.days)
            print(f"审计统计 ({args.days}天)")
            print("=" * 40)
            print(f"总调用: {stats['total']}, 异常: {stats['errors']}")
            print("\nTOP 用户:")
            for name, count in stats["top_users"]:
                print(f"  {name}: {count}")
            print("\nTOP 命令:")
            for cmd, count in stats["top_commands"]:
                print(f"  {cmd}: {count}")
        else:
            entries = read_audit_log()
            print(f"今日审计日志 ({len(entries)} 条):")
            for e in entries[:20]:
                print(f"  {e.get('ts', '?')[:19]} | {e.get('user_name', '?')} | {e.get('command', '?')} | {e.get('result', '?')}")

    elif args.action == "whitelist":
        if args.wl_action == "add":
            result = add_user(args.admin, args.user, args.name, args.level)
            print("✅ 已添加" if result["ok"] else f"⛔ {result['reason']}")
        elif args.wl_action == "list":
            result = list_users(args.admin)
            if result["ok"]:
                for u in result["users"]:
                    level_label = {0: "游客", 1: "授权用户", 2: "管理员"}.get(u["level"], "?")
                    print(f"  {u['name']} ({u['id']}) — L{u['level']} {level_label}")
            else:
                print(f"⛔ {result['reason']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
