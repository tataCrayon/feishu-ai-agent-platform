"""CMDB 工单读接口客户端（只读，零写操作）。

与 `~/.claude/skills/cmdb-order-tracker/scripts/cmdb_client.py` 的关系：那边是
「人机对话用的 CLI + 知识资产」，这里是**常驻进程**里跑的一小片读能力。刻意不 import
那个脚本——skill 目录属于个人配置，随技能升级重写，bot 不能绑死它的路径。

实测结论（内网直连）：
- `OrderApi?action=detail` / `?action=data` / `?action=deploystatus`：**免 session**
  （内网 IP 白名单）。这是「新工单出现」与「任务状态迁移」两条主链路的全部依赖。
- `TotalOrderApi?action=tree`（工单列表，新工单发现的入口）：**需要 sessionid**。
- `ArcheryApi?action=status`（SQL 台账最新状态）：需要 sessionid；`action=data`
  内嵌的 `sql_status` 可能是旧值，所以有 session 时以 Archery 为准，没有就退回内嵌值。

因此 session 是**可选增强**而不是前置条件：没有 session 时仍可靠
`order_id` 顺序探测 + 内嵌状态工作，只是新工单发现要调用方给一个起点 id 和扫描条数。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("cmdb_watch.client")

DEFAULT_BASE_URL = "http://cmdb.example.com"
API_PREFIX = "/apiservices/order"
SESSION_FILE = Path.home() / ".cmdb-session"
# 账号密码：与 cmdb-assistant skill 的 cmdb-login.mjs 共用同一份，登录一次两边都能用
CREDENTIALS_FILE = Path.home() / ".cmdb.conf"
# bot 专用凭证（独立服务账号）。存在就优先用它，缺省才回落到上面那份本人账号。
BOT_CREDENTIALS_FILE = Path.home() / ".cmdb-bot.conf"
LOGIN_PATH = "/apiservices/login/"

# ── 后台续期策略 ──────────────────────────────────────────────
# CMDB 是单会话登录：同一账号再登一次，之前那条 sessionid 立即失效。而
# `~/.cmdb.conf` 存的正是**本人**账号 —— 人在浏览器里用 CMDB 时，后台一登录就把
# 浏览器踢下线。所以默认「后台不自己登」：
#   confirm（默认）— 失效只降级为 id 探测 + 告警一次，绝不调登录接口
#   true           — 失效即登录（仅当配置的是独立服务账号时才该开）
#   false          — 同 confirm，但连告警都不打
RELOGIN_ENV = "CMDB_ALLOW_RELOGIN"
# 不登录时的告警去重窗口：轮询间隔 120s，不去重会把同一句话每两分钟写一遍
_NO_RELOGIN_WARN_INTERVAL_SEC = 1800
_last_no_relogin_warn_at = 0.0
_last_no_relogin_note = ""

# ── 「刚登录就被顶」的自保 ────────────────────────────────────
# 共用本人账号时，auto 模式会进入人机互踢：bot 登录 → 人浏览器被踢 → 人重新登录 →
# bot 被踢 → bot 又登录……每轮 120s 一次，正是「浏览器老是掉线」的成因。
# 唯一能识别的信号是「**我自己**刚登录的那条会话，很快又失效了」——只有同账号在别处
# 登录才能造成它。此时说明人在用浏览器，bot 应当让位，而不是立刻抢回来。
_SELF_LOGIN_KICK_WINDOW_SEC = 3600   # 自登录后 1 小时内被顶 → 判定有人在同账号上活跃
_SELF_LOGIN_YIELD_SEC = 1800         # 让位时长：期间只降级 id 探测，不重登
_self_login_sid = ""
_self_login_at = 0.0
_yield_until = 0.0
# 会话校验唯一可信入口：它按 session 反查 UmUser。列表/读接口走内网白名单，
# 匿名也返回数据，拿它们判登录态只会假阳性（skill 里踩过，这里沿用同一口径）。
ACCOUNT_PATH = "/apiservices/account/"

# 状态码表（前端 chunk 反编译确认，见 skill 的 status-machine.md）——
# 这张表是「事件判定」的唯一依据，改它之前先核对 CMDB 前端。
SQL_STATES = {1: "未执行", 2: "执行中", 3: "执行成功", 4: "执行失败", 5: "已取消"}
DEPLOY_STATES = {0: "未执行", 1: "执行中", 2: "执行中", 3: "执行成功", 4: "执行中",
                 5: "执行成功", 6: "执行失败", 7: "执行中", 8: "执行成功",
                 9: "执行失败", -1: "执行失败"}
NACOS_STATES = {1: "未执行", 2: "执行成功", 3: "执行失败"}

SQL_RUNNING = {2}
SQL_DONE = {3, 4, 5}
DEPLOY_RUNNING = {1, 2, 4, 7}
DEPLOY_DONE = {3, 5, 6, 8, 9, -1}
NACOS_RUNNING: set[int] = set()   # NACOS 无中间态（一次性提交）
NACOS_DONE = {2, 3}

# 任务类型（订阅的「按类型过滤」用）。cron 与 nacos 状态位同构，归一组。
TASK_KINDS = ("sql", "deploy", "nacos", "cron")
KIND_LABELS = {"sql": "SQL 变更", "deploy": "应用发布",
               "nacos": "Nacos 配置", "cron": "定时任务"}


def classify(kind: str, state: int) -> str:
    """状态码 + 类型 → pending / running / done。

    单列成函数是因为两处都要用同一口径：`Task`（刚拉回来的数据）和 `TaskState`
    （落盘的上一轮基线，见 store）。两套判定一旦漂移，「已结束」的任务会被当成
    活的反复轮询，或者反过来——已完成的任务永远等不到终态。
    """
    if kind == "sql":
        return "running" if state in SQL_RUNNING else ("done" if state in SQL_DONE else "pending")
    if kind == "deploy":
        return "running" if state in DEPLOY_RUNNING else (
            "done" if state in DEPLOY_DONE else "pending")
    # nacos / cron：一次性提交，没有中间态
    return "running" if state in NACOS_RUNNING else ("done" if state in NACOS_DONE else "pending")


# ── 环境（生产 / UAT）────────────────────────────────────────
# CMDB 把生产与 UAT 做成**两套并行的接口前缀**，数据完全隔离：
#   OrderApi / TotalOrderApi / ArcheryApi      → /order/OrderDetail
#   UatOrderApi / UatTotalOrderApi / UatArcheryApi → /order/UatOrderDetail
#
# 最要命的一条：**order_id 在两套表里是两条独立的自增序列**，UAT 1924 与生产 1924
# 是两张毫无关系的单。所以环境必须是一等参数，跟着「读回来的那种工单」一起走，
# 绝不能靠「先试生产、失败再试 UAT」来猜——那会把用户对 UAT 单的关注悄悄绑到
# 一张同号的生产单上，然后给他推一堆不相干的提醒。
ENV_PROD = "prod"
ENV_UAT = "uat"
ENVS = (ENV_PROD, ENV_UAT)
ENV_LABELS = {ENV_PROD: "生产", ENV_UAT: "UAT"}
# 工单详情页路径：与前端路由一致，卡片链接靠它区分环境
ENV_ORDER_PAGES = {ENV_PROD: "OrderDetail", ENV_UAT: "UatOrderDetail"}


def normalize_env(raw: str) -> str:
    """把用户/模型给的环境写法归一。无法识别或不给 → 生产（与历史行为一致）。"""
    value = (raw or "").strip().lower()
    if value in ("uat", "测试", "测试环境", "uat环境", "uat 环境"):
        return ENV_UAT
    return ENV_PROD


def env_of_order_page(url_or_text: str) -> str:
    """从工单页链接/路径里读出环境；认不出返回空串（调用方自行决定默认）。

    `UatOrderDetail?order_id=1924` → uat；`OrderDetail?order_id=5910` → prod。
    **先判 UAT**：`uatorderdetail` 里也含 `orderdetail`，顺序反了永远判成生产。
    """
    text = (url_or_text or "").lower()
    if "uatorderdetail" in text:
        return ENV_UAT
    if "orderdetail" in text:
        return ENV_PROD
    return ""


def api_prefix(env: str) -> str:
    """环境 → 接口名前缀（`Uat` 或无）。三套 API 全走同一规则。"""
    return "Uat" if env == ENV_UAT else ""


def split_order_key(raw: str) -> tuple[str, int]:
    """`prod:5784` → (prod, 5784)。老档案里的裸 `5784` 按生产读。

    `uat:1924:ou_x` 这种带尾巴的也认——只看前两段，后面的归属由调用方解析。
    """
    parts = str(raw or "").split(":")
    if len(parts) >= 2 and parts[0] in ENVS:
        try:
            return parts[0], int(parts[1])
        except ValueError:
            return ENV_PROD, 0
    try:
        return ENV_PROD, int(parts[0])
    except (ValueError, IndexError):
        return ENV_PROD, 0


class CmdbError(Exception):
    """CMDB 读失败。`need_session` 为真表示「不是网络问题，是没登录」。"""

    def __init__(self, message: str, *, need_session: bool = False):
        super().__init__(message)
        self.need_session = need_session


def load_session_id(explicit: str = "") -> str:
    """sessionid 来源优先级：显式参数 > 环境变量 CMDB_SESSION_ID > `~/.cmdb-session`。"""
    if explicit.strip():
        return explicit.strip()
    env = os.environ.get("CMDB_SESSION_ID", "").strip()
    if env:
        return env
    return _read_session_file()


def _read_session_file() -> str:
    """只读共享会话文件（不看 env）。人工把浏览器里的 sid 同步进来时用它感知变化。"""
    try:
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_conf(path: Path) -> dict[str, str]:
    """逐行字面量解析 conf 文件，不用 dotenv/eval。

    密码里带 `$` `` ` `` `\\` 时，走 shell 展开会被静默改写（Kuboard 登录踩过
    同类坑）；解析成 dict 后改密码只改那一个文件。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip()
    return values


def _read_credentials() -> tuple[str, str]:
    """→ (user, pass)。bot 专用账号优先，缺省回落到本人账号。

    优先级是有意的：`~/.cmdb-bot.conf` 是**独立服务账号**，用它登录不顶任何人；
    `~/.cmdb.conf` 是与 skill/人共用的一份，登录会顶掉本人浏览器会话。所以只要
    bot 账号存在就绝不用本人账号。
    """
    for path in (BOT_CREDENTIALS_FILE, CREDENTIALS_FILE):
        values = _parse_conf(path)
        user, password = values.get("CMDB_USER", ""), values.get("CMDB_PASS", "")
        if user and password:
            return user, password
    return "", ""


def relogin_mode() -> str:
    """后台续期三态：`auto` / `confirm`（默认）/ `never`。"""
    raw = os.environ.get(RELOGIN_ENV, "confirm").strip().lower()
    if raw == "true":
        return "auto"
    if raw == "false":
        return "never"
    return "confirm"


def using_bot_account() -> bool:
    """凭证是不是独立服务账号（决定「自动登录」是否安全）。"""
    values = _parse_conf(BOT_CREDENTIALS_FILE)
    return bool(values.get("CMDB_USER") and values.get("CMDB_PASS"))


def _post_json(url: str, payload: dict[str, Any], timeout: int) -> tuple[Any, str]:
    """POST JSON，返回 (响应体, 原始 Set-Cookie)。

    与 `_http_json` 同一口径走裸 http.client：内网域名经系统代理会被拒成 502，
    表现为「CMDB 挂了」。
    """
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    conn = conn_cls(parts.hostname or "", parts.port, timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json",
                              "User-Agent": "feishu-agent-platform/cmdb-watch"})
        resp = conn.getresponse()
        raw = resp.read()
        cookies = resp.getheader("Set-Cookie") or ""
        if resp.status >= 400:
            raise CmdbError(f"不可达: HTTP {resp.status}")
        try:
            return json.loads(raw.decode("utf-8")), cookies
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CmdbError(f"返回非 JSON（登录失败页/网关错误页？）: {e}") from e
    except (OSError, http.client.HTTPException) as e:
        raise CmdbError(f"不可达: {e}") from e
    finally:
        conn.close()


def login(base_url: str = "", timeout: int = 20) -> str:
    """用账号密码换一个新 sessionid，探活通过后落盘并返回。

    CMDB 的接口全部返回 HTTP 200，成败只看 body 里的 `result`——拿状态码判断会把
    「密码错了」当成登录成功。登录接口回 result=true 也不代表这条 session 真能用
    （实测出现过「登录成功、立刻 UmUser」），所以**先探活再落盘**：不合格就抛错，
    不覆盖文件里那条可能还有用的会话。
    """
    global _self_login_sid, _self_login_at
    user, password = _read_credentials()
    if not user or not password:
        raise CmdbError(
            f"未配置 CMDB 账号密码（{BOT_CREDENTIALS_FILE} 或 {CREDENTIALS_FILE}）",
            need_session=True)
    base = (base_url or os.environ.get("CMDB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body, raw_cookie = _post_json(
        f"{base}{LOGIN_PATH}",
        {"action": "check", "username": user, "password": password},
        timeout,
    )
    if not isinstance(body, dict) or not body.get("result"):
        reason = (body or {}).get("errMsg") or (body or {}).get("msg") or "未知原因"
        raise CmdbError(f"CMDB 登录失败：{reason}", need_session=True)
    match = re.search(r"(?:^|[,\s])sessionid=([^;,\s]+)", raw_cookie)
    if not match:
        raise CmdbError("CMDB 登录返回成功但没拿到 sessionid cookie", need_session=True)
    session_id = match.group(1)
    if not session_valid(session_id, base, timeout=min(timeout, 8)):
        # 走到这里说明「登录接口说成功、账号接口说未登录」。常见成因是同账号在别处
        # 又登了一次把我顶掉，或该账号状态异常。落盘只会让下一个使用者踩同一个坑。
        raise CmdbError(
            f"CMDB 登录后立即失效（user={user}）：登录接口返回成功，但 /account/ 判为未登录。"
            "多为同账号在别处登录顶掉，或该账号被停用", need_session=True)
    try:
        SESSION_FILE.write_text(session_id, encoding="utf-8")
    except OSError as e:
        logger.warning("CMDB session 落盘失败（本次仍可用）: %s", e)
    # 记下「这次是我自己登的」：它若很快又失效，就说明有人在同账号上登录把我顶了
    _self_login_sid, _self_login_at = session_id, time.time()
    logger.info("CMDB 已重新登录（user=%s），sessionid 已写入 %s", user, SESSION_FILE)
    return session_id


def _note_self_login_kicked() -> bool:
    """判定「刚自己登的会话被人顶掉」；命中则进入让位期，返回 True。

    判据刻意保守：只有「我登的那条仍是文件里的当前值」且「距我登录还不到 1 小时」
    才算被顶。文件被人工改写（同步了浏览器 sid）属于正常交接，不触发让位。
    """
    global _yield_until
    if not _self_login_sid or _self_login_sid != _read_session_file():
        return False
    if time.time() - _self_login_at > _SELF_LOGIN_KICK_WINDOW_SEC:
        return False
    _yield_until = time.time() + _SELF_LOGIN_YIELD_SEC
    logger.warning(
        "CMDB 检测到「刚由后台登录的会话被顶掉」：同账号在别处（多半是浏览器）登录了。"
        "让位 %d 分钟不再重登，期间降级为 order_id 探测。"
        "想彻底消除人机互踢，请配独立服务账号 %s",
        _SELF_LOGIN_YIELD_SEC // 60, BOT_CREDENTIALS_FILE)
    return True


def _record_external_session(session_id: str) -> None:
    """采用一条**外部**（人工同步/别人写的）会话：它不是后台登录产物，不算「我登的」。"""
    global _yield_until
    _yield_until = 0.0


def _read_current_session(fallback: str = "") -> str:
    """看文件里是否有比手里这条更新的会话；有且有效则采用。返回最终该用的 sid。

    这是「给最新 session 就不该重登」的落点。必须**每次请求前**都看，不能只在
    失效后看：人把浏览器 sid 写进文件的那一刻，手里这条往往还是有效的，等它失效
    再读就晚了——中间那段要么白等，要么已经登了一次把人踢下线。
    """
    shared = _read_session_file()
    if not shared or shared == fallback:
        return fallback
    if not session_valid(shared, timeout=8):
        return fallback
    _record_external_session(shared)
    logger.info("CMDB 采用会话文件里的新 sessionid（%s...），未调用登录接口", shared[:8])
    return shared


def renew_session_if_stale(session_id: str, base_url: str = "", timeout: int = 15) -> str:
    """会话失效时按 `CMDB_ALLOW_RELOGIN` 决定要不要续；返回**新的** sessionid。

    返回空串 = 「没有续（或续失败），调用方请沿用原来的会话并降级为 id 探测」。
    绝不把入参原样返回：那会让调用方误以为「续期成功、可以重试了」。

    默认**不续**：`~/.cmdb.conf` 是本人账号，后台一登录就把浏览器踢下线，而
    CMDB 是单会话登录——「越保活越掉线」正是这么来的。失效时安静降级为 id 探测
    （功能还在，只是发现晚一轮），并把「该怎么修」写进日志，而不是自己动手。

    `auto` 模式（用户显式开启）也会做一层自保：若「后台刚登录的那条」很快被顶，
    说明人在用浏览器，此时让位一段时间而不是立刻抢回来 —— 否则就是 120s 一轮的
    人机互踢。
    """
    global _last_no_relogin_warn_at, _last_no_relogin_note

    if session_id and session_valid(session_id, base_url, timeout=8):
        return session_id

    mode = relogin_mode()
    if mode == "auto":
        if time.time() < _yield_until:
            logger.debug("CMDB 让位期内不重登（还剩 %ds）", int(_yield_until - time.time()))
            return ""
        if _note_self_login_kicked():
            return ""

    if mode != "auto":
        note = (
            f"CMDB 登录态失效，按 CMDB_ALLOW_RELOGIN={mode} 不自动登录"
            f"（登录会顶掉同账号的浏览器会话），本轮降级为 order_id 探测。"
            f"修法：用独立服务账号写入 {BOT_CREDENTIALS_FILE}"
            f"（CMDB_USER/CMDB_PASS），或把浏览器里现成的 sessionid 同步到 {SESSION_FILE}"
        )
        if mode == "never":
            # false 是「明确要求安静」：纯后台托管场合不该为一条已知状态刷日志
            logger.debug(note)
            return ""
        now = time.time()
        # 同一个原因每 30 分钟说一次就够：轮询 120s 一轮，不节流会把日志刷成墙
        if note != _last_no_relogin_note or now - _last_no_relogin_warn_at >= _NO_RELOGIN_WARN_INTERVAL_SEC:
            logger.warning(note)
            _last_no_relogin_note, _last_no_relogin_warn_at = note, now
        else:
            logger.debug("CMDB 登录态仍失效（不自动登录，告警已去重）")
        return ""

    try:
        return login(base_url, timeout=timeout)
    except CmdbError as e:
        logger.warning("CMDB 自动续期失败：%s", e)
        return ""


def session_valid(session_id: str, base_url: str = "", timeout: int = 8) -> bool:
    """探一次账号接口判断 session 是否还有效。

    必须走 `/apiservices/account/`：它会按 session 反查 UmUser，是真登录态校验。
    列表/读接口受内网 IP 白名单放行，匿名也返回数据——拿它们判断会一直「有效」，
    直到真正需要 session 的那一步才炸。
    """
    if not session_id:
        return False
    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}{ACCOUNT_PATH}?action=data"
    try:
        body = _http_json(url, {"Accept": "application/json",
                                "User-Agent": "feishu-agent-platform/cmdb-watch",
                                "Cookie": f"sessionid={session_id}"}, timeout)
    except CmdbError:
        return False
    if not isinstance(body, dict) or not body.get("result"):
        return False
    data = body.get("data")
    return isinstance(data, dict) and bool(data.get("username"))


def _to_int(value: Any, default: int = 0) -> int:
    """CMDB 的 status 有时是 int、有时是字符串（两者都实测见过）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _http_json(url: str, headers: dict[str, str], timeout: int) -> Any:
    """发 GET 并解析 JSON。

    刻意不用 `urllib.request.urlopen`：它读系统代理环境变量（本机开着 Clash，
    `HTTP_PROXY=http://127.0.0.1:7897`），内网域名 `*.example.com`
    经代理出去会被对端拒成 **502 Bad Gateway**——而 CMDB 站点本身对浏览器
    `/` 是 200，排查时极易误判成「CMDB 挂了」。内网地址必须直连。
    """
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    conn = conn_cls(parts.hostname or "", parts.port, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise CmdbError(f"不可达: HTTP {resp.status}")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CmdbError(f"返回非 JSON（入口页/网关错误页？）: {e}") from e
    except (OSError, http.client.HTTPException) as e:
        raise CmdbError(f"不可达: {e}") from e
    finally:
        conn.close()


@dataclass
class Task:
    """工单里的一条可执行任务（SQL / 发布 / NACOS / 定时任务）。"""

    kind: str                 # sql | deploy | nacos | cron
    task_id: int
    name: str
    creator: str              # CMDB 用户名（如 devuser）
    state: int                # 原始状态码
    state_label: str
    client_id: int = 0
    client_title: str = ""
    env: str = ""

    @property
    def key(self) -> str:
        """跨轮次稳定的身份：kind+id 在工单内唯一。"""
        return f"{self.kind}:{self.task_id}"

    @property
    def running(self) -> bool:
        return self._classify() == "running"

    @property
    def done(self) -> bool:
        return self._classify() == "done"

    def _classify(self) -> str:
        return classify(self.kind, self.state)


@dataclass
class Order:
    """一次 `data` 接口的规整结果（去掉了 html_content 之类的大字段）。"""

    order_id: int
    order_name: str
    tasks: list[Task] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    # 这张单属于哪个环境。取数时确定，之后卡片链接、去重键、落盘键都以它为准
    env: str = ENV_PROD

    @property
    def key(self) -> str:
        """跨环境的稳定身份：`prod:5910` / `uat:1924`。

        两套编号空间重叠，光用 order_id 当键会让 UAT 单撞上同号的生产单。
        """
        return f"{self.env}:{self.order_id}"

    def mine(self, users: set[str]) -> list[Task]:
        """命中人员名单的任务。匹配是**大小写不敏感的全等**，不做前缀/包含匹配——
        `wang` 包含匹配会把 wangwei.wang 的工单也拉进来。"""
        if not users:
            return []
        lowered = {u.lower() for u in users}
        return [t for t in self.tasks if t.creator.lower() in lowered]


class CmdbClient:
    """只读客户端。线程/事件循环无关（同步 urllib，调用方用 asyncio.to_thread 包）。"""

    def __init__(self, base_url: str = "", session_id: str = "", timeout: int = 15):
        self.base_url = (base_url or os.environ.get("CMDB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.session_id = load_session_id(session_id)
        self.timeout = timeout
        self._renew_lock = threading.Lock()

    def renew_session(self) -> bool:
        """失效时先看有没有人同步了新 sid，再按开关决定是否登录；返回手上是否有可用会话。

        **先跟随共享文件**：人在浏览器里重新登录后把 sid 写进 `~/.cmdb-session`，
        常驻进程内存里还是旧的那条（已失效）。不重读文件就会「明明文件里有新会话，
        bot 却说登录态失效」，然后按 confirm 干等着——那是把「人工同步」这条路堵死了。
        """
        with self._renew_lock:
            refreshed = _read_current_session(self.session_id)
            if refreshed != self.session_id:
                self.session_id = refreshed
                return True
            renewed = renew_session_if_stale(self.session_id, self.base_url, timeout=self.timeout)
        if renewed and renewed != self.session_id:
            self.session_id = renewed
            return True
        return bool(renewed)

    # ── HTTP ────────────────────────────────────────────────
    def _get(self, api: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{API_PREFIX}/{api}/?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": "feishu-agent-platform/cmdb-watch"}
        if self.session_id:
            headers["Cookie"] = f"sessionid={self.session_id}"
        try:
            body = _http_json(url, headers, self.timeout)
        except CmdbError as e:
            raise CmdbError(f"{api} {e}", need_session=e.need_session) from e
        if not isinstance(body, dict):
            raise CmdbError(f"{api} 返回结构异常: {type(body).__name__}")
        msg = str(body.get("msg") or "")
        if not body.get("result"):
            # 「未登陆」是配置问题不是网络问题：调用方要能据此提示补 session，而不是重试
            raise CmdbError(f"{api} 失败: {msg or body.get('errMsg') or '未知'}",
                            need_session="未登陆" in msg or "登陆" in msg)
        return body

    # ── 读能力 ──────────────────────────────────────────────
    # 每个读方法都收 `env`：接口名由 `api_prefix(env)` 拼，返回的 Order 带上环境。
    # 调用方必须**从读回来的工单**取 `.env` 再往下传，不要凭印象补——猜错环境就
    # 等于对着另一张单跟踪（两套 order_id 空间重叠，见文件抬头）。
    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        """拉一张工单的全量任务清单（免 session 的主路径）。"""
        body = self._get(f"{api_prefix(env)}OrderApi",
                         {"action": "data", "order_id": order_id})
        order = parse_order(body.get("data") or {})
        order.env = normalize_env(env)
        return order

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        """轻量状态轮询：只拿部署状态码，不拉任务清单正文。

        实测单张不同接口的体量与耗时（12 张工单采样）：`action=data` 平均
        125ms / 3~183KB（`html_content` 占大头），`action=deploystatus`
        平均 56ms / <1KB。**已跟踪工单的每轮轮询只该走这条**——任务清单
        （谁创建、什么名字）在首见时已经落盘，只有状态会变。

        **只覆盖 deploy**：这个接口实测只回 `deploy_status`（不含 SQL / NACOS /
        定时任务），`NacosApi` 也没有 status 动作。其余三类的状态迁移由
        `refresh_sec` 的全量校准兜住，最多晚一个校准周期。
        """
        body = self._get(f"{api_prefix(env)}OrderApi",
                         {"action": "deploystatus", "order_id": order_id})
        data = body.get("data") or {}
        return {"deploy_status": data.get("deploy_status") or {},
                "run_bit": bool(data.get("run_bit"))}

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        """轻量存在性探测（`action=detail` 比 `data` 便宜得多）。"""
        try:
            self._get(f"{api_prefix(env)}OrderApi",
                      {"action": "detail", "order_id": order_id})
            return True
        except CmdbError:
            return False

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        """工单列表（总工单树），**需要 session**。返回所有子工单 id，新的在前。

        列表接口不接受分页参数（实测传入被忽略，固定回一批 150 张），所以只能拿它做
        「最近 N 张」的全量集合，增量靠我们自己记住上次见到的最大 id。
        一次 143 张约 260ms / 50KB——这是**最划算**的发现方式，比逐一探测 id 便宜得多。

        UAT 走 `UatTotalOrderApi`，同一个 sessionid 两套都能读（实测）。
        """
        body = self._get(f"{api_prefix(env)}TotalOrderApi", {"action": "tree"})
        ids: list[int] = []
        for node in body.get("data") or []:
            for child in (node.get("children") or []):
                oid = _to_int(child.get("order_id"))
                if oid:
                    ids.append(oid)
        return sorted(set(ids), reverse=True)

    def sql_status(self, sql_id: int, env: str = ENV_PROD) -> int | None:
        """SQL 台账最新状态（**需要 session**）。取不到返回 None（调用方退回内嵌值）。"""
        try:
            body = self._get(f"{api_prefix(env)}ArcheryApi",
                             {"action": "status", "sql_id": sql_id})
        except CmdbError as e:
            logger.debug("[cmdb] sql %s 状态取不到（%s），退回 data 内嵌值", sql_id, e)
            return None
        return _to_int(body.get("data"), default=-99)


def parse_order(data: dict[str, Any], env: str = ENV_PROD) -> Order:
    """把 `OrderApi?action=data` 的 data 规整成 Order。

    状态取值口径：deploy / nacos 以 data 里的 `*_status` 字典为准；
    SQL 优先用内嵌 `sql_status`，缺失时看 `archery_data.workflow.status`——
    后者是审核流字符串，只在字典里没有这个 sql_id 时才当兜底。
    """
    order_id = _to_int(data.get("order_id"))
    order = Order(order_id=order_id, order_name=str(data.get("order_name") or ""),
                  env=normalize_env(env))
    deploy_status = data.get("deploy_status") or {}
    nacos_status = data.get("nacos_status") or {}
    cron_status = data.get("cron_status") or {}
    sql_status = data.get("sql_status") or {}

    for client in data.get("client") or []:
        client_id = _to_int(client.get("client_id"))
        client_title = str(client.get("client_title") or "")
        client_creator = str(client.get("creator") or "")

        for item in client.get("sql_list") or []:
            sql_id = _to_int(item.get("id"))
            state = _to_int(sql_status.get(str(sql_id), sql_status.get(sql_id, 0)))
            if not state:
                state = _sql_state_from_workflow(item.get("archery_data"))
            order.tasks.append(Task(
                kind="sql", task_id=sql_id,
                name=str(item.get("workflow_name") or item.get("db_name") or f"SQL#{sql_id}"),
                creator=str(item.get("creator") or client_creator),
                state=state, state_label=SQL_STATES.get(state, f"未知({state})"),
                client_id=client_id, client_title=client_title,
                env=str(item.get("instance_group_label") or ""),
            ))

        for item in client.get("nacos_list") or []:
            nacos_id = _to_int(item.get("id"))
            pair = nacos_status.get(str(nacos_id)) or nacos_status.get(nacos_id) or [1, ""]
            state = _to_int(pair[0] if isinstance(pair, (list, tuple)) else pair)
            order.tasks.append(Task(
                kind="nacos", task_id=nacos_id,
                name=f"{item.get('data_id') or f'NACOS#{nacos_id}'}"
                     f"（{item.get('act_type_str') or ''}）".replace("（）", ""),
                creator=str(item.get("creator") or client_creator),
                state=state, state_label=NACOS_STATES.get(state, f"未知({state})"),
                client_id=client_id, client_title=client_title,
                env=str(item.get("env") or ""),
            ))

        for item in client.get("cron_list") or []:
            cron_id = _to_int(item.get("id"))
            pair = cron_status.get(str(cron_id)) or cron_status.get(cron_id) or [1, ""]
            state = _to_int(pair[0] if isinstance(pair, (list, tuple)) else pair)
            order.tasks.append(Task(
                kind="cron", task_id=cron_id,
                name=str(item.get("name") or item.get("job_name") or f"定时任务#{cron_id}"),
                creator=str(item.get("creator") or client_creator),
                state=state, state_label=NACOS_STATES.get(state, f"未知({state})"),
                client_id=client_id, client_title=client_title,
                env=str(item.get("env") or ""),
            ))

        for item in client.get("deploy_data") or []:
            deploy_id = _to_int(item.get("deploy_id"))
            pair = deploy_status.get(str(deploy_id)) or deploy_status.get(deploy_id) or [0, ""]
            state = _to_int(pair[0] if isinstance(pair, (list, tuple)) else pair)
            order.tasks.append(Task(
                kind="deploy", task_id=deploy_id,
                name=str(item.get("job_name") or f"发布#{deploy_id}"),
                creator=str(item.get("creator") or client_creator),
                state=state, state_label=DEPLOY_STATES.get(state, f"未知({state})"),
                client_id=client_id, client_title=client_title,
                env=str(item.get("env") or ""),
            ))
    return order


def _sql_state_from_workflow(archery_data: Any) -> int:
    """archery 审核流字符串 → CMDB 状态码（只在内嵌 sql_status 缺项时兜底）。"""
    status = ""
    if isinstance(archery_data, dict):
        workflow = archery_data.get("workflow")
        if isinstance(workflow, dict):
            status = str(workflow.get("status") or "")
    return {
        "workflow_manreviewing": 1, "workflow_waiting": 2, "workflow_doing": 2,
        "workflow_finish": 3, "workflow_abort": 4, "workflow_exception": 4,
    }.get(status, 1)
