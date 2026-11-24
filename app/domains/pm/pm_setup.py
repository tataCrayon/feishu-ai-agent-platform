"""PM 自助配置 —— 用户在飞书对话里完成绑定与自检，不用改 `config.toml` 重启。

为什么要有这一层
----------------
换人用这套 PM 能力时，原本要做的三件事都需要动文件：填 `[projects.pm]`、
确认 lark-cli 用谁的身份登录、确认能读到他的表和群。任一步做错，表现都是
**静默的**——推送发到空目标、感知线程扫回来一堆空、雷达算出 0 个版本。
所以在对话里给一个自检入口，比写一份 README 有用。

三条设计约束
------------
1. **不写回 `config.toml`**：那是有注释、有排版的人写的文件，机器回写会把
   注释吃掉。配置只读不写；要改值仍然由人改文件。
2. **自检不发送**：`/pm 检查` 只读不写，不给他发任何消息、不写表。
3. **身份不认口述**：用户在对话里说「我是李四」不算数，只有 lark-cli 的
   登录态（`auth status` 的 `openId`）才是事实。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domains.pm.pm_config import pm_config

logger = logging.getLogger("pm_hub.setup")

# 授权待办的落盘位置：仓库根（与 `.threads.jsonl` 等运行态文件同处）。
# 落盘而不是放内存：device flow 有 10 分钟窗口，期间进程重启过一次就白扫了。
PROJECT_DIR = Path(__file__).resolve().parents[3]


@dataclass
class Check:
    """一项自检结果。`ok` 为 None 表示「查不了」（不算失败，但要如实说）。"""

    name: str
    ok: Optional[bool]
    detail: str = ""
    fix: str = ""

    def render(self) -> str:
        mark = "✅" if self.ok else ("⚠️" if self.ok is None else "❌")
        line = f"{mark} {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        if self.fix and not self.ok:
            line += f"\n   修：{self.fix}"
        return line


def _lark_cli_json(cmd: List[str], timeout: int = 45, open_id: str = "") -> Dict[str, Any]:
    """调 lark-cli 并解析 JSON。失败返回 {}，不抛——自检要能继续跑下一项。

    `open_id` 非空时走该用户自己的凭据目录（`pm_identity`），
    这样自检看到的就是「他本人登录的那个身份」，不是机器级默认身份。
    """
    from app.domains.pm.pm_identity import run_cli

    try:
        return run_cli(cmd, open_id=open_id, timeout=timeout)
    except Exception as e:
        logger.warning("lark-cli 调用失败 %s: %s", cmd[:2], e)
        return {}


def lark_user_identity(cfg=None) -> Dict[str, Any]:
    """该用户的登录态：`openId` / `userName` / `tokenStatus` / `scope`。

    这是「读消息是谁的身份」的唯一事实源。用户在对话里自报身份不算——
    读会话用的是这个 token，不是他说他是谁。

    身份按 `[projects.pm].identity.open_id` 对应的**独立凭据目录**取
    （`~/.lark-cli/pm/<open_id>/`），与机器级 `hermes/` 登录互不覆盖。
    """
    from app.domains.pm.pm_identity import user_env  # noqa: F401  说明依赖方向

    cfg = cfg or pm_config()
    who = cfg.identity.open_id
    if not who:
        data = _lark_cli_json(["auth", "status"])
        return dict((data.get("identities") or {}).get("user") or {})

    info = _lark_cli_json(["whoami"], open_id=who)
    on_behalf = info.get("onBehalfOf") or {}
    if not on_behalf:
        return {}
    return {
        "openId": on_behalf.get("openId") or "",
        "userName": on_behalf.get("userName") or "",
        "tokenStatus": info.get("tokenStatus") or "",
    }


def check_identity(cfg=None) -> Check:
    """该用户自己目录里的登录态，是否就是配置里的那个人。

    这里查的是 `~/.lark-cli/pm/<open_id>/`，**不是**机器级 `hermes/` 的登录态。
    两者互不影响：别人在这台机器上登录过 lark-cli，不该改到 PM 的读数。
    """
    cfg = cfg or pm_config()
    want = cfg.identity.open_id
    ident = lark_user_identity(cfg)
    have = str(ident.get("openId") or "")
    who = str(ident.get("userName") or "")
    if not want:
        return Check("身份已配置", False, "config.toml 里 open_id 为空",
                     "在 [projects.pm.identity] 填 open_id 与 names")
    if not have:
        return Check("身份已配置", None, f"open_id={want}，但他还没授权",
                     "发 /pm 授权 走一次登录（凭据存在他自己的目录里，不覆盖别人）")
    if have != want:
        return Check(
            "身份一致", False,
            f"配置={want} / 该目录登录={who}({have})",
            "他在自己目录里登录的是另一个人：先 /pm 退出 再 /pm 授权",
        )
    if str(ident.get("tokenStatus") or "") not in ("valid", "ready", "ok"):
        return Check("身份一致", None, f"{who}（token 需要刷新，下次调用会自动续）")
    return Check("身份一致", True, f"{who}({want})")


def check_outputs(cfg=None) -> Check:
    """能不能读到版本排期表。读不到后面的雷达、催办全是空。"""
    cfg = cfg or pm_config()
    token = cfg.outputs.spreadsheet_token
    sheet = cfg.outputs.sheet_id_versions
    if not (token and sheet):
        return Check("表已配置", False, "spreadsheet_token / sheet_id_versions 为空",
                     "在 [projects.pm.outputs] 填表 token 与 sheet id")
    from app.domains.pm.pm_hub_engine import fetch_sheet_values

    try:
        rows = fetch_sheet_values(token, f"{sheet}!A1:N3")
    except Exception as e:
        return Check("表可读", False, f"{type(e).__name__}: {str(e)[:80]}",
                     "确认这份表已共享给 lark-cli 登录的那个人")
    if not rows:
        return Check("表可读", False, "读到了 0 行（表为空或区间写错）")
    return Check("表可读", True, f"{len(rows)} 行（{sheet}）")


def check_radar(cfg=None) -> Check:
    """雷达能不能算出活跃版本。它是「这套数据到底有没有用」的最终判据。"""
    cfg = cfg or pm_config()
    if not (cfg.outputs.spreadsheet_token and cfg.outputs.sheet_id_versions):
        return Check("雷达可算", None, "表未配置，跳过")
    from datetime import date

    from app.domains.pm.pm_hub_engine import (
        evaluate_daily_radar,
        fetch_sheet_values,
        parse_stories,
    )

    try:
        vals = fetch_sheet_values(
            cfg.outputs.spreadsheet_token, f"{cfg.outputs.sheet_id_versions}!A1:N60")
        radar = evaluate_daily_radar(parse_stories(vals), today=date.today())
    except Exception as e:
        return Check("雷达可算", False, f"{type(e).__name__}: {str(e)[:80]}")
    active = radar.get("active_versions") or []
    if not active:
        return Check("雷达可算", None, "解析出 0 个活跃版本（表结构或日期列可能变了）",
                     "检查表头：业务线/需求名称/开发责任人/测试责任人/目标上线日")
    head = "、".join(
        f"{v['version']}(剩{v['workdays_left']}天)" for v in active[:4])
    return Check("雷达可算", True, f"{len(active)} 个活跃版本：{head}")


def check_push_target(cfg=None) -> Check:
    """推送目标是否可达。私聊靠 open_id、群靠 group_id，两者都可能单独缺。"""
    cfg = cfg or pm_config()
    if not cfg.identity.open_id:
        return Check("推送目标", False, "open_id 为空，私聊推送无处可去")
    parts = [f"私聊→{cfg.identity.names[0] if cfg.identity.names else cfg.identity.open_id}"]
    if cfg.push.group_id:
        parts.append(f"群→{cfg.push.group_id[:12]}…")
    else:
        parts.append("群未配置（只发私聊）")
    return Check("推送目标", True, "；".join(parts))


def check_enabled(cfg=None) -> Check:
    cfg = cfg or pm_config()
    if not cfg.enabled:
        return Check("已启用", False, "enabled = false",
                     "在 [projects.pm] 设 enabled = true 后重启 bot")
    return Check("已启用", True, "enabled = true")


def run_all(cfg=None) -> List[Check]:
    """跑全部自检。每项独立，一项失败不影响后面的。"""
    cfg = cfg or pm_config()
    return [
        check_enabled(cfg),
        check_identity(cfg),
        check_outputs(cfg),
        check_radar(cfg),
        check_push_target(cfg),
    ]


def render_report(checks: Optional[List[Check]] = None) -> str:
    """自检报告（人话，可直接贴进飞书）。"""
    checks = checks if checks is not None else run_all()
    ok = sum(1 for c in checks if c.ok is True)
    bad = sum(1 for c in checks if c.ok is False)
    head = f"PM 配置自检：{ok} 项通过" + (f"，{bad} 项要修" if bad else "，全部就绪")
    lines = [head, ""]
    lines += [c.render() for c in checks]
    if bad:
        lines += ["", "改完 config.toml 后重启 bot 生效；身份不一致时先 lark-cli auth login。"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 对话入口 —— 给 pm_daemon 的 `_COMMANDS` 用
# ══════════════════════════════════════════════════════════════════════

PKG_PROMPT = (
    "【PM 工作台 · 接入指引】\n\n"
    "这套能力要跑起来，缺三样东西：\n\n"
    "1. **你是谁** —— 发 `/pm 授权`，我给你一个二维码，扫码授权后回我「授权好了」。\n"
    "   凭据存在你自己的目录里，不覆盖这台机器上别人的登录。\n\n"
    "2. **你的表在哪** —— 版本排期电子表格 + 日计划多维表格。\n"
    "   发 `/pm 表 <表格链接>`，我从链接里取 token。\n\n"
    "3. **什么时候推** —— 早间/晚间的时点，以及要不要发到群里。\n"
    "   默认 09:00 / 17:30、只发私聊，不满意再告诉我。\n\n"
    "随时发 `/pm 检查` 看还差哪一项，`/pm 用户` 看本机已授权了谁。"
)

HELP_LINES = {
    "/pm": PKG_PROMPT,
    "/pm 帮助": PKG_PROMPT,
    "/pm help": PKG_PROMPT,
}


def handle_command(text: str, sender_id: str = "") -> Optional[str]:
    """`/pm ...` 的入口。不匹配返回 None（交给后续路由）。

    支持：
        /pm 帮助      接入指引
        /pm 检查      跑自检并出报告
        /pm 我是 XX   核对 lark-cli 登录态后回「怎么填」，**不写文件**
        /pm 表 <链接> 从链接解析 token 并回该填哪一行，**不写文件**
    """
    t = (text or "").strip()
    low = t.lower()
    for k, v in HELP_LINES.items():
        if low == k:
            return v

    if low in ("/pm 检查", "/pm 自检", "/pm check"):
        return render_report()

    if t.startswith("/pm 我是"):
        name = t[len("/pm 我是"):].strip()
        cfg = pm_config()
        ident = lark_user_identity(cfg)
        have = str(ident.get("userName") or "")
        oid = str(ident.get("openId") or "")
        if not oid:
            return ("你还没授权，我读不到你的身份。\n发 `/pm 授权`，我发个二维码给你扫。")
        if name and name not in have and have not in name:
            return (f"注意：你说的是「{name}」，但你的凭据目录里登录的是「{have}」。\n"
                    f"读消息用的是登录那个人，推消息用的是配置里那个人——两边必须是同一个人。\n\n"
                    f"确认无误的话，配置里写：\n"
                    f"open_id = \"{oid}\"\nnames = [\"{have}\"]")
        return (f"身份核对通过：{have}（{oid}）\n\n"
                f"在 config.toml 里填：\n"
                f"[projects.pm.identity]\n  open_id = \"{oid}\"\n  names = [\"{have}\"]\n\n"
                f"我不会替你改文件——那个文件里有注释，机器回写会把它们吃掉。")

    if t.startswith("/pm 表"):
        link = t[len("/pm 表"):].strip()
        token = _token_from_link(link)
        if not token:
            return ("没从链接里认出 token。支持这两种：\n"
                    "· https://<租户>.feishu.cn/sheets/<token>?sheet=<sheet_id>\n"
                    "· https://<租户>.feishu.cn/base/<token>?table=<table_id>")
        kind = "base" if "/base/" in link else "sheets"
        if kind == "base":
            table_id = _query_param(link, "table")
            return (f"从链接解析到多维表格 token：{token}\n"
                    + (f"当前 table：{table_id}\n" if table_id else "")
                    + "\n在 config.toml 里填：\n[projects.pm.outputs]\n"
                    + f"  base_token = \"{token}\"\n"
                    + (f"  table_id = \"{table_id}\"\n" if table_id
                       else "  table_id = \"<链接里 table= 后面那段>\"\n"))
        sheet_id = _query_param(link, "sheet")
        return (f"从链接解析到电子表格 token：{token}\n"
                + (f"当前 sheet：{sheet_id}\n" if sheet_id else "")
                + "\n在 config.toml 里填：\n[projects.pm.outputs]\n"
                + f"  spreadsheet_token = \"{token}\"\n"
                + (f"  sheet_id_versions = \"{sheet_id}\"\n" if sheet_id else ""))

    if low in ("/pm 授权", "/pm 登录", "/pm auth"):
        return _begin_auth()

    if low in ("/pm 授权完成", "/pm 完成了", "/pm 好了"):
        return _complete_auth()

    if low in ("/pm 退出", "/pm 登出", "/pm logout"):
        return _logout()

    if low in ("/pm 用户", "/pm 身份列表", "/pm users"):
        return _render_users()

    if low.startswith("/pm"):
        return f"没认出这条指令。\n\n{PKG_PROMPT}"
    return None


# ══════════════════════════════════════════════════════════════════════
# 授权流程 —— device flow 分两段，中间交给用户扫码
#
# 为什么必须两段：`auth login` 不带 `--no-wait` 会阻塞轮询，用户在这期间
# 根本看不到链接。所以第一段只拿 URL 就返回，用户扫完回来说一声，第二段
# 用 device_code 收尾。device_code 落盘（600 秒有效），重启也能接上。
# ══════════════════════════════════════════════════════════════════════

_PENDING_AUTH_PATH = PROJECT_DIR / ".pm_auth_pending.json"


def _read_pending() -> Dict[str, Any]:
    try:
        return json.loads(_PENDING_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_pending(data: Dict[str, Any]) -> None:
    try:
        _PENDING_AUTH_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("写授权待办失败: %s", e)


def _begin_auth() -> str:
    """第一段：发起 device flow，回一段带链接的说明（二维码由调用方补发）。"""
    from app.domains.pm.pm_identity import start_login

    cfg = pm_config()
    who = cfg.identity.open_id
    if not who:
        return ("配置里还没有 open_id，我不知道该给谁授权。\n"
                "先让管理员在 config.toml 的 [projects.pm.identity] 填上你的 open_id。")
    try:
        info = start_login(who)
    except Exception as e:
        return f"发起授权失败：{e}\n稍后再试一次，或让管理员跑 lark-cli auth login。"

    _write_pending({
        "open_id": who,
        "device_code": info["device_code"],
        "verification_url": info["verification_url"],
        "expires_at": time.time() + int(info["expires_in"]),
    })
    minutes = max(1, int(info["expires_in"]) // 60)
    return (
        f"授权链接（{minutes} 分钟内有效）：\n"
        f"{info['verification_url']}\n\n"
        "用手机飞书扫码或点开链接，确认授权。\n"
        "**完成后回我一句「授权好了」**，我接着把登录收尾。\n\n"
        "授权范围只有读消息、表格、文档、妙记、通讯录这几项——多要一项就是多一份风险。"
    )


def _complete_auth() -> str:
    """第二段：用待办里的 device_code 收尾。"""
    from app.domains.pm.pm_identity import finish_login

    pending = _read_pending()
    code = str(pending.get("device_code") or "")
    if not code:
        return "没有进行中的授权。发 `/pm 授权` 重新开始。"
    if time.time() > float(pending.get("expires_at") or 0):
        _PENDING_AUTH_PATH.unlink(missing_ok=True)
        return "授权链接已过期（10 分钟）。发 `/pm 授权` 重新拿一个。"

    ok, msg = finish_login(str(pending.get("open_id") or ""), code)
    if ok:
        _PENDING_AUTH_PATH.unlink(missing_ok=True)
        return (f"{msg}\n\n"
                "现在可以用了：`/pm 检查` 看全套能力是否就绪。")
    return msg


def _logout() -> str:
    """退出该用户自己的登录态。只动他自己的目录，不碰机器级登录。"""
    from app.domains.pm.pm_identity import auth_home, run_cli

    who = pm_config().identity.open_id
    if not who:
        return "配置里没有 open_id，没有可退出的对象。"
    try:
        run_cli(["auth", "logout"], open_id=who, timeout=60)
    except Exception as e:
        return f"退出失败：{e}"
    return f"已退出 {who} 的登录态（目录 {auth_home(who)} 保留，重授权会复用）。"


def _render_users() -> str:
    """本机有哪些 PM 用户授权过、是不是可用。"""
    from app.domains.pm.pm_identity import list_users

    users = list_users()
    if not users:
        return "还没有任何 PM 用户授权。发 `/pm 授权` 开始。"
    lines = [f"本机已登记 {len(users)} 个 PM 凭据目录："]
    for u in users:
        mark = "✅" if u["ready"] else "⚠️"
        name = u["user_name"] or "（未登录）"
        lines.append(f"{mark} {name} · {u['open_id']}")
    return "\n".join(lines)


def _query_param(url: str, key: str) -> str:
    from urllib.parse import parse_qs, urlparse

    try:
        return (parse_qs(urlparse(url).query).get(key) or [""])[0]
    except Exception:
        return ""


def _token_from_link(link: str) -> str:
    """从飞书 sheets / base 链接里取 token。取不到返回空串，不猜。"""
    import re

    m = re.search(r"/(?:sheets|base|wiki)/([A-Za-z0-9]{16,})", link or "")
    return m.group(1) if m else ""


def now_iso() -> str:
    """当前时间（秒级 ISO），自检报告落盘时用。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")
