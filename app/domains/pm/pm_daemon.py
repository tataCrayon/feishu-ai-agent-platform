"""PM Hub Daemon - PMO 专属守护进程与群聊监听网关

分层
----
    传输层  FeishuClient           只负责收消息、回消息
    对话层  process_user_query     命令 → 意图路由 → handler 查表
    调度层  run_daemon             3 秒轮询 + 09:00 / 17:30 定时任务

对话层不再用关键字 if 链分流。历史缺陷：
"今日：1、私募周报" 不含"计划"却含"排期"，被当成排期查询吐整篇晨报；
"同步日计划：1、私募周报" 含"计划"，切词后把"同步日计划："本身录成一条待办。

现在语义理解统一交给 pm_intent_router（LLM，失败降级正则），
本文件只做「意图 → handler」查表，新增能力不必改动主流程。

定时器守护：
- 工作日 09:00：生成并推送每日晨间雷达，同步日计划到多维表格
- 工作日 17:30：推送日清复盘卡片
"""

import sys
import os
import re
import shutil
import time
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import List, Optional
import requests

PROJECT_DIR = Path(__file__).resolve().parents[3]

# 凭据在本文件 import 期就要就位，而 .env 是由 app.config 负责加载的。
# 先 `import app.config` 把 PROJECT_DIR/.env 灌进 os.environ，再读 APP_ID/SECRET。
import app.config as _app_config  # noqa: E402,F401  仅为触发 .env 加载

from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    PMO_GROUP_ID,
    PM_OPEN_ID,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm.pm_hub_sender import (
    build_evening_review_card,
    send_group_message,
)
from app.domains.pm.pm_confirm_card import send_interactive_card
from app.domains.pm.feishu_pm_sync import (
    generate_today_tasks,
    sync_tasks_to_bitable,
    fetch_existing_records,
)
from app.domains.pm.pm_todo_intake import (
    intake_structured_todos,
    planned_items as today_planned_items,
    set_status as set_planned_status,
    normalize_status as normalize_todo_status,
)
from app.domains.pm.pm_knowledge_hub import query_people_map
from app.domains.pm import pm_kb_answer as kb_answer
from app.domains.pm.pm_estimator import estimate_prd_effort
from app.domains.pm import pm_weekly_plan as weekly_plan
from app.domains.pm import pm_weekly_write as weekly_write
from app.domains.pm import pm_nudge as nudge
from app.domains.pm import pm_agent
from app.domains.pm import pm_watch
from app.domains.pm import pm_proactive
from app.domains.pm import pm_followup
from app.domains.pm import pm_kb_gaps as kb_gaps
from app.domains.pm import pm_thread
from app.domains.pm import pm_daily_triage as triage
from app.domains.pm import pm_followup_intake as intake
from app.domains.pm.pm_intent_router import (
    INTENT_CHAT,
    INTENT_DRAFT_NUDGE,
    INTENT_ESTIMATE,
    INTENT_LOG_PLAN,
    INTENT_QUERY_KB,
    INTENT_QUERY_OWNER,
    INTENT_QUERY_SCHEDULE,
    INTENT_UPDATE_STATUS,
    IntentResult,
    call_llm_text,
    route_intent,
)

# Windows 控制台/重定向文件默认走 GBK，中文日志会乱码，排障时读不了。
# 必须在 logging.basicConfig 之前切 UTF-8，否则 StreamHandler 已绑定旧编码。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# 日志配置
# Windows 下把 stderr 重定向到文件时，Python 按系统 locale(GBK) 写，中文全乱码，
# 排障时日志等于没有。所以额外挂一个显式 UTF-8 的文件 handler，
# 以后排障统一读 logs/pm_daemon.log，不看重定向输出。
_LOG_DIR = PROJECT_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(_LOG_DIR / "pm_daemon.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("pm_daemon")

TZ = timezone(timedelta(hours=8))
# 机器人自己的 open_id 运行期从 `/bot/v3/info` 取（`FeishuClient.bot_open_id`）。
# 写死会在换 app / 换租户后把「@我」判成「@了别人」—— 判定不报错，只是全面失效。
BOT_OPEN_ID = ""

# 机器人在飞书里的显示名。用来在会话清单里认出「我和它的私聊」，见 _resolve_p2p_chat。
BOT_NAME = "AgentMate"

# 飞书应用凭据只从 .env 读（项目铁律：凭据不入库，代码里不留明文）。
# 与 `app/config.py` 的 feishu 段同一个来源，避免两处各配一份、改一处忘一处。
APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 定时任务幂等性
# ----------------
# 原来「今天是否已推送」只存在进程内存里。20:00 重启一次，晚间复盘就重发一次，
# 对用户来说是打扰。所以把发过的日期落盘，重启后接着用。
_STATE_PATH = PROJECT_DIR / ".daemon_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("写状态文件失败（不影响主流程）: %s", e)


# 定时账本
# --------
# `.daemon_state.json` 是整份覆盖写，里面还混着 p2p_chat_id 这种被别的线程
# 读改写的东西。定时任务的「今天已经发过了」一旦和它共用一个文件，
# 重启或并发时就可能被冲掉：两份清单并推过一次，群里收到两遍，就是这类问题。
#
# 所以单独一个账本：只记 slot → 日期，每次读磁盘最新再写自己那一格。
_SLOTS_PATH = PROJECT_DIR / ".schedule_slots.json"


def _read_slots() -> dict:
    try:
        slots = json.loads(_SLOTS_PATH.read_text(encoding="utf-8"))
        return slots if isinstance(slots, dict) else {}
    except Exception:
        return {}


# 提测询问的「待发」账本
# ----------------------
# 节拍是硬约束：`nudge_ask` 当天跑不到就永久丢失，第二天不会补，也不告警。
# 所以命中发问日之后先落一条待发记录，**真正发出去才消**；跨日续发由
# `_flush_nudge_ask` 负责，漏发则在下一次推送里明确说一句「哪天那条没发出去」。
_NUDGE_PENDING_PATH = PROJECT_DIR / ".nudge_pending.json"


def _read_nudge_pending() -> list:
    try:
        data = json.loads(_NUDGE_PENDING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_nudge_pending(rows: list) -> None:
    try:
        _NUDGE_PENDING_PATH.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写提测询问待发账本失败: %s", e)


def _enqueue_nudge_ask(plan: list, today_str: str) -> None:
    """把「该发但还没发」的迭代落盘。同一迭代同一天只留一条。"""
    rows = _read_nudge_pending()
    have = {(r.get("iteration"), r.get("due")) for r in rows}
    for p in plan:
        key = (p.get("iteration"), p.get("due") or today_str)
        if key in have:
            continue
        rows.append({
            "iteration": p.get("iteration", ""),
            "due": p.get("due") or today_str,
            "count": p.get("count", 0),
            "queued_at": datetime.now(TZ).isoformat(timespec="seconds"),
        })
    _write_nudge_pending(rows)


def _flush_nudge_ask(feishu, today_str: str) -> list:
    """发送欠着的提测询问。返回本次没发成功的迭代名（用来提醒用户）。

    失败不抛异常：发版询问发不出去不该把整个晨间推送拖垮，但也不能静默——
    调用方把返回值拼进推送末尾，让他知道「1112 的提测询问昨天没发出去」。
    """
    rows = _read_nudge_pending()
    if not rows:
        return []

    failed: list = []
    left: list = []
    for r in rows:
        it_name = r.get("iteration", "")
        try:
            items = nudge.nudges_from_iteration([_IterRef(it_name)], today=datetime.now(TZ).date())
            if not items:
                continue  # 已经没有要问的人了，这条自然消掉
            feishu.send_user_text(PM_OPEN_ID, nudge.stage(PM_OPEN_ID, items))
            logger.info("已补发提测询问：迭代 %s（%d 条，原定 %s）",
                        it_name, len(items), r.get("due"))
        except Exception as e:
            logger.exception("补发提测询问失败（迭代 %s）: %s", it_name, e)
            failed.append(it_name)
            left.append(r)
    _write_nudge_pending(left)
    return failed


@dataclass
class _IterRef:
    """`nudges_from_iteration` 只用到 `.name`，补发时不必回飞书项目查一遍。"""

    name: str


def _claim_today(slot: str, day: str) -> bool:
    """占坑：这个 slot 今天还没做过就返回 True，并立刻落盘。

    调用方拿到 True 才执行推送。首次运行会先把 .daemon_state.json 里的
    旧字段迁过来，免得升级当天把当天已经发过的再发一遍。
    """
    slots = _read_slots()

    if not slots:
        legacy = _load_state()
        slots = {k: legacy[k] for k in ("morning", "evening", "triage") if legacy.get(k)}
        if slots:
            # 迁移完立刻落盘：否则账本文件一直不存在，每次调用都回退去读旧文件，
            # 「唯一真源」就名存实亡了。
            try:
                _SLOTS_PATH.write_text(json.dumps(slots, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning("迁移定时账本失败: %s", e)

    if slots.get(slot) == day:
        return False

    slots[slot] = day
    try:
        _SLOTS_PATH.write_text(json.dumps(slots, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("写定时账本失败（本次照常执行）: %s", e)
    return True


# 探测节流：只记在内存里，进程重启就重新探一次。
# 为什么不落盘：落盘会让「修好配置后重启」也白等一个间隔，
# 而这层只用来压日志噪音，不是业务状态。
_PROBE_LAST: dict = {}


def _probe_due(name: str, interval_sec: float) -> bool:
    """这个探测项距上次探测是否已过 `interval_sec` 秒。第一次调用恒为 True。"""
    now_ts = time.time()
    last = _PROBE_LAST.get(name, 0.0)
    if now_ts - last < interval_sec:
        return False
    _PROBE_LAST[name] = now_ts
    return True


# 备忘池账本
# --------
# 「跟」类的事（等时机、等别人回话）不进今天的待办，只记进工作簿的
# 「未开始事项」。但 backlog_items 每天都返回同一批事，不记账的话
# 他每天早上都要被问一遍「陈雨帆这条要记吗」—— 这比不提醒更烦人。
#
# 记 key → 第一次问的日期。7 天内不再问；落笔成功后表里有了，
# `Backlog.has()` 直接命中，也就自然不再问。
_BACKLOG_ASKED_PATH = PROJECT_DIR / ".backlog_asked.json"
_BACKLOG_REASK_DAYS = 7


def _backlog_asked() -> dict:
    try:
        d = json.loads(_BACKLOG_ASKED_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _backlog_mark(keys: List[str], day: date) -> None:
    d = _backlog_asked()
    for k in keys:
        d[k] = day.isoformat()
    try:
        _BACKLOG_ASKED_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("写备忘池账本失败: %s", e)


def _backlog_fresh(items: List[dict]) -> List[dict]:
    """挑出「表里还没有、最近也没问过」的那几条。

    读表失败时按「都没问过」处理 —— 宁可多问一句，也别静默丢掉一条事。
    """
    if not items:
        return []

    try:
        bl = weekly_plan.load_backlog()
    except Exception as e:
        logger.warning("读未开始事项失败，本次不做判重: %s", e)
        bl = None

    asked = _backlog_asked()
    today = date.today()
    out: List[dict] = []

    for i in items:
        task = (i.get("task") or "").strip()
        if not task:
            continue
        if bl is not None and bl.has(task):
            continue
        prev = asked.get(i.get("key") or task)
        if prev:
            try:
                if (today - date.fromisoformat(prev)).days < _BACKLOG_REASK_DAYS:
                    continue
            except Exception:
                pass  # 账本里的日期坏了就当没记过
        out.append(i)
    return out


# 单条文本消息的内容上限。飞书接口本身能塞很大，但手机上一屏读不完就等于没发。
# 一屏读不完等于没发：所以这里不截断，改成主动切片，
# 每片带 (i/n)，保证内容一定完整送到。
_MAX_TEXT_CHUNK = 2800


def split_text(text: str, limit: int = _MAX_TEXT_CHUNK) -> list:
    """按行切片，尽量不把一句话劈成两半。"""
    if not text or len(text) <= limit:
        return [text]
    chunks: list = []
    buf = ""
    for line in text.split("\n"):
        # 单行本身就超长（少见），硬切
        while len(line) > limit:
            if buf:
                chunks.append(buf.rstrip())
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit and buf:
            chunks.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


class FeishuClient:
    """飞书开放平台最小客户端。只管收发，不含任何业务判断。"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expires_at = 0

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {data}")
        self._token = data["tenant_access_token"]
        self._expires_at = time.time() + data.get("expire", 3600)
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def bot_open_id(self) -> str:
        """机器人自己的 open_id（`/bot/v3/info`）。

        用来判「这条消息 @ 的是我」。写死会把「@我」判成「@了别人」——
        判定不报错，只是全面失效，而换 app 之后 ID 就变了。
        """
        return str((self.bot_info() or {}).get("open_id") or "")

    def bot_info(self) -> dict:
        """机器人身份信息（`app_name` / `open_id` / `avatar_url` 等）。

        显示名也从这里取：在会话清单里认「我和它的私聊」靠名字匹配，
        写死名字在改应用名之后会让私聊通道整条失效（`_resolve_p2p_chat` 返回空）。
        """
        resp = requests.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers=self.headers(),
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"取 bot 身份失败: {data}")
            return {}
        return dict(data.get("bot") or {})

    def list_messages(self, chat_id: str, page_size: int = 10) -> list:
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": page_size,
            "sort_type": "ByCreateTimeDesc",
        }
        resp = requests.get(url, headers=self.headers(), params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"拉取消息失败: {data}")
            return []
        return data.get("data", {}).get("items", [])

    def reply_text(self, message_id: str, text: str):
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=self.headers(), json=body, timeout=10)
        return resp.json()

    def send_chat_text(self, chat_id: str, text: str):
        return self._send_text("chat_id", chat_id, text)

    def send_user_text(self, open_id: str, text: str):
        """给他本人发私聊，超长自动切片。

        感知到的事不能发 PMO 群 —— 那是团队群，里面还有易山武，私聊内容和
        「谁在背后问什么」不该给第三个人看。所以这类消息一律走私聊。

        返回体里的 chat_id 顺手落盘：这是「他回复我能不能收到」的关键。
        没有它就只能去 271 个会话里猜哪个 bot 是自己，猜错就整条通道是死的。
        """
        return self._send_text("open_id", open_id, text)

    def _send_text(self, id_type: str, receive_id: str, text: str):
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={id_type}"
        parts = split_text(text)
        last = {}
        for i, part in enumerate(parts, 1):
            body_text = part if len(parts) == 1 else f"({i}/{len(parts)})\n{part}"
            body = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": body_text}, ensure_ascii=False),
            }
            resp = requests.post(url, headers=self.headers(), json=body, timeout=15)
            last = resp.json()
            if last.get("code") != 0:
                logger.warning("发送失败(%s/%s): %s", i, len(parts), last)
                break

        cid = (last.get("data") or {}).get("chat_id")
        if cid:
            st = _load_state()
            if st.get("p2p_chat_id") != cid:
                st["p2p_chat_id"] = cid
                _save_state(st)
        return last


# ══════════════════════════════════════════════════════════════════════
# 对话层 —— 一条消息的生命周期
#
#     text ──► 命令层(查表，不走模型) ──► 意图路由(LLM) ──► handler ──► reply
#
# 约束：
# 1. handler 是纯函数 IntentResult -> str，不碰网络、不碰状态。
# 2. 所有「面向用户看到的文案」集中在 handler 尾部拼装，便于统一语调。
# 3. 飞书 text 消息不渲染 markdown，**加粗** 会原样显示星号，一律不用。
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ReplyContext:
    """一轮对话的上下文。handler 需要什么从这拿，不要读全局变量。

    现在只有 sender（状态回写要知道谁在改），后面加会话记忆时也挂这里，
    handler 签名不用再动。
    """
    sender: str
    text: str


COPILOT_SYSTEM_PROMPT = (
    "你是示例科技 PMO 团队专属的智能助手 AgentMate，服务于项目经理李四。\n"
    "风格：客观、务实、业务语言优先、不说废话、不堆砌客套。\n"
    "你熟知公司公募/私募/ESOP 三条产品线，以及 SOP V2.3 七道闸门"
    "（G1 需求冻结、G2 方案评审、G3 承接承诺、G4 联调就绪、G5 提测准入、G6 代码冻结、G7 发版投产）。\n"
    "回答简明、结构化，用中文。问不到的信息就说不知道，不要编。"
)

_HELP_TEXT = (
    "我是 AgentMate，可以直接发这几种：\n\n"
    "· 今日过审 —— /过审（现在就把全部会话扫一遍，清单发你私聊）\n"
    "· 接入配置 —— /pm（看还差哪几样）/pm 检查（逐项自检）\n"
    "· 计划录入 —— 今日：1、私募周报；2、story排期\n"
    "· 状态更新 —— 3 完成 / 私募周报阻塞了（回「确认」才写表）\n"
    "· 催办起草 —— 催一下 10.29 / 催办（我先拟稿，你改完说「确认」）\n"
    "· 排期查询 —— 10.29 还剩几天 / 现在在跟哪些版本\n"
    "· 负责人查询 —— WebPortal 前端找谁\n"
    "· 流程检索 —— 提测准入的 SOP 是什么\n"
    "· 工时评估 —— 评估工时：私募周报改版，报审 8 人天\n\n"
    "不用加命令前缀，正常说话就行。"
)


# ── 渲染层：把业务结构翻成人话 ────────────────────────────────────────

def _fmt_todos(todos: list) -> str:
    """待办清单。

    gate 为 None 时不加任何前缀。历史版本会把这类事项标成「日清待办」——
    那是个内部兜底常量，不是业务概念，不该出现在任何用户可见输出里。
    """
    lines: list[str] = []
    for i, t in enumerate(todos, 1):
        lines.append(f"{i}. {t.title}")
        if t.gate:
            lines.append(f"    └ {t.gate}")
    return "\n".join(lines)


def _fmt_blindspots(report: list, limit: int = 3) -> str:
    """盲区提示。全局限流，避免一条消息抖出十几个括号。"""
    flat: list[str] = []
    for item in report or []:
        flat.extend(item.get("alerts") or [])
        flat.extend(item.get("blindspots") or [])
    if not flat:
        return ""

    body = "\n".join(f"  · {x}" for x in flat[:limit])
    if len(flat) > limit:
        body += f"\n  · 另有 {len(flat) - limit} 条未展开"
    return f"\n\n⚠️ 需要提前收口\n{body}"


def _fmt_degraded(result: IntentResult) -> str:
    """降级标注。用户有权知道这次不是模型在判断，否则会误以为系统变笨了。"""
    return "\n\n（降级模式：大模型暂不可用，已按关键词解析）" if result.fallback_used else ""


# ── handler：一种意图一个纯函数 ────────────────────────────────────────

def _handle_log_plan(result: IntentResult, _ctx: ReplyContext) -> str:
    res = intake_structured_todos(result.todos, auto_sync_bitable=True)
    if not res.get("success"):
        return (
            "没识别出具体工作项，换个说法再发一次？\n"
            "例：今日：1、私募周报；2、story排期"
        )

    todos = res["todos"]
    sync = res.get("sync_result") or {}
    created = sync.get("created", 0)
    total = sync.get("total", len(todos))

    if created == total:
        tail = f"\n\n🔗 已写入多维表格「日计划」：{created}/{total}"
    else:
        tail = f"\n\n🔗 多维表格「日计划」写入 {created}/{total}，失败项需人工补录"

    return (
        f"✅ 今日计划已同步（{len(todos)} 项）\n\n"
        f"{_fmt_todos(todos)}"
        f"{_fmt_blindspots(res.get('blindspots'))}"
        f"{tail}"
        f"{_fmt_degraded(result)}"
    )


def _load_radar() -> dict:
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    return evaluate_daily_radar(parse_stories(vals), today=date.today())


def _match_version(versions: list, query: str):
    """从提问里认出用户指的是哪个版本。

    容忍 "10.29"、"10月29"、"10/29"、"1029" 四种写法。
    """
    q = (query or "").strip()
    if not q:
        return None

    direct = next((v for v in versions if v["version"] in q), None)
    if direct:
        return direct

    m = re.search(r"(\d{1,2})\s*[.\-/月]?\s*(\d{1,2})", q)
    if not m:
        return None
    key = f"{int(m.group(1))}.{int(m.group(2))}"
    return next((v for v in versions if v["version"] == key), None)


def _handle_schedule(result: IntentResult, _ctx: ReplyContext) -> str:
    """排期查询必须是「回答问题」，不是复读整篇晨报。"""
    radar = _load_radar()
    versions = radar.get("active_versions") or []
    if not versions:
        return "📅 版本计划表里没读到有效版本行，我核对下表格结构再回你。"

    hit = _match_version(versions, result.query or "")
    if hit:
        left = hit["workdays_left"]
        if left > 0:
            timing = f"距上线还有 {left} 个工作日"
        elif left == 0:
            timing = "今天就是上线日"
        else:
            timing = f"已过上线日 {abs(left)} 个工作日"
        return (
            f"📅 {hit['version']} 版本\n\n"
            f"· 目标上线：{hit['target_date']}\n"
            f"· {timing}\n"
            f"· 需求数：{hit['story_count']} 条\n"
            f"· 当前闸门：{hit['current_gate'] or '尚未进入闸门区间'}"
        )

    lines = [
        f"· {v['version']} — {v['target_date']}"
        f"（剩 {v['workdays_left']} 个工作日，{v['story_count']} 条需求）"
        for v in sorted(versions, key=lambda x: x["target_date"])
    ]
    return (
        "📅 在跟的版本\n\n"
        + "\n".join(lines)
        + "\n\n要看单版本细节，直接说版本号，比如「10.29」。"
    )


def _pretty_people(blocks: list) -> str:
    """把 markdown 表格行压成人话，一行一条。

    query_people_map 返回的是「【分类】\\n表头\\n| 数据 |」三行拼串，
    原样发到飞书就是一堆竖线。这里按表头把值对上去：
    「【研发/技术 — 前端】钟林均 · 前端开发 · WebPortal · 机构交付/Web · 向高志华汇报」
    """
    out = []
    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 3 or not lines[2].startswith("|"):
            out.append(b)
            continue
        category = lines[0].strip("【】")
        headers = [c.strip() for c in lines[1].strip("|").split("|")]
        cells = [c.strip() for c in lines[2].strip("|").split("|")]
        parts = [c for _, c in zip(headers, cells) if c and c not in ("—", "-", "待补")]
        out.append(f"【{category}】" + " · ".join(parts))
    return "\n".join(out)


def _handle_owner(result: IntentResult, _ctx: ReplyContext) -> str:
    res = query_people_map(result.query or "")
    rows = res.get("results") or []
    if rows:
        return "👥 人物协作\n\n" + _pretty_people(rows[:5])
    return "👥 没匹配到对应负责人，换个关键字试试（如：WebPortal、董泓林）。"


def _handle_kb(result: IntentResult, _ctx: ReplyContext) -> str:
    """知识库问答：检索 → 模型基于材料作答 → 附出处。

    旧实现读的是 search_knowledge_base 里不存在的 layer/title 字段
    （KeyError 直接抛），且按空格切词，中文提问一律 0 命中。两条都换掉了。
    """
    return kb_answer.render(result.query or "")


def _handle_estimate(result: IntentResult, _ctx: ReplyContext) -> str:
    text = result.query or ""
    claimed = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*人天", text)
    if m:
        claimed = float(m.group(1))

    title = re.sub(r"^(?:评估|估算|工时)\s*[:：]?\s*", "", text).strip() or text
    res = estimate_prd_effort(title=title[:60], description=text, claimed_days=claimed)
    if not res.get("success"):
        return f"📐 工时评估没跑通：{res.get('message') or res.get('error')}"

    lines = [
        f"📐 工时评估：{res['title']}",
        "",
        f"· PRD 质量：{res['prd_score']} 分（{res['prd_grade']}）"
        + ("" if res.get("is_admissible") else "，未达准入线"),
        f"· 建议工期：{res['ai_predicted_days']} 人天",
    ]
    cmp = res.get("claimed_vs_predicted")
    if cmp:
        lines.append(
            f"· 报审对比：{cmp['claimed_days']} 人天，偏差 {cmp['diff_percent']} {cmp['risk_flag']}"
        )
    lines += ["", res["pmo_advice"]]
    return "\n".join(lines)


def _handle_chat(result: IntentResult, _ctx: ReplyContext) -> str:
    answer = call_llm_text(COPILOT_SYSTEM_PROMPT, result.query or "")
    if not answer:
        return (
            "模型这会儿连不上，稍后再问我一次。\n"
            "急的话直接发「今日：1、xxx；2、xxx」，本地链路照样能录。"
        )
    return answer


def _week_item_lines(block) -> str:
    """列出当周事项，用于「没找到 / 匹配到多条」时让用户挑。"""
    return "\n".join(f"{it.no}. {it.task}  [{it.status}]" for it in block.items)


_PLANNED_TOKEN_RE = re.compile(r"今\s*(\d{1,2})")


def _handle_planned_status(nos: list, status: str) -> str:
    """「今2 完成」—— 改日计划表，不走周计划那套暂存确认。

    日计划是他自己录的一条条待办，改状态没有歧义（record_id 直接命中），
    所以先校验状态、再直写；周计划可能改到别人的行，仍然必须 stage+确认。
    """
    value = normalize_todo_status(status)
    if not value:
        return "状态只认 完成 / 阻塞 / 进行中，比如「今2 完成」。"

    try:
        items = today_planned_items(date.today())
    except Exception as e:
        logger.exception("读今日计划失败")
        return f"读日计划表失败（{type(e).__name__}），稍后重试一次。"

    if not items:
        return "今天日计划表里还没有条目，先录几条再来改状态。"

    listing = "\n".join(f"今{i}. {it['title']}" for i, it in enumerate(items, 1))

    picked, missing = [], []
    for no in nos:
        idx = int(no) - 1
        if 0 <= idx < len(items):
            picked.append((no, items[idx]))
        else:
            missing.append(no)

    if missing:
        return f"今日计划没有「今{'、今'.join(missing)}」，现在有这些：\n{listing}"

    done, failed = [], []
    for no, it in picked:
        if set_planned_status(it["record_id"], value):
            done.append(f"今{no}. {it['title']}")
        else:
            failed.append(it["title"])

    parts = []
    if done:
        parts.append(f"✅ 改成「{value}」：\n" + "\n".join(done))
    if failed:
        parts.append("这几条接口没写成功，待会儿再试：" + "、".join(failed))
    return "\n\n".join(parts)


def _handle_update_status(result: IntentResult, ctx: ReplyContext) -> str:
    """状态回写：定位 → 暂存 → 等确认。任何情况下都不一步写表。"""
    planned_nos = _PLANNED_TOKEN_RE.findall(result.target or "")
    if planned_nos:
        return _handle_planned_status(planned_nos, result.status or "")

    try:
        plan = weekly_plan.load_active_plan(date.today())
    except Exception as e:
        logger.exception("读取周计划失败")
        return f"读工作簿失败（{type(e).__name__}），稍后重试一次。"

    block = plan.block
    if not block:
        return (
            "工作簿里没读到「每周计划跟踪」，暂时没法回写。\n"
            "sht001 需要有一个当周块：周期日历行 + 表头行 + 事项行。"
        )

    target = result.target or ""
    items, problems = weekly_plan.resolve_targets(block, target)

    if problems:
        detail = "\n".join(f"· {p}" for p in problems)
        return f"{detail}\n\n{_week_item_lines(block)}\n\n用编号或事项名都行，多个编号就空格隔开，比如「5 7 完成」。"

    if not items:
        return (
            f"本周（{block.label}）里没找到「{target}」。\n\n"
            f"{_week_item_lines(block)}\n\n"
            f"用编号或事项名都行，比如「3 完成」。"
        )

    return weekly_write.stage_batch(
        sender=ctx.sender,
        pairs=[(it, result.status or "") for it in items],
        week_label=block.label,
    )


def _version_hits(version: str, month: int, day: int) -> bool:
    """version 形如 "10.29"，按数值比较，"10.9" 与 "10.09" 都算命中。"""
    try:
        m, d = (int(x) for x in version.split(".")[:2])
    except (ValueError, AttributeError):
        return False
    return m == month and d == day


def _filter_nudges(items: list, query: str) -> tuple[list, str]:
    """按用户提到的版本/人名收窄范围。返回 (结果, 收窄说明)。"""
    q = (query or "").strip()
    if not q:
        return items, ""

    m = re.search(r"(\d{1,2})\s*[.\-/月]\s*(\d{1,2})", q)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        hit = [n for n in items if _version_hits(n.story.version, mm, dd)]
        return hit, f"{mm}.{dd:02d} 版本"

    hit = [
        n for n in items
        if q in n.audience or q in n.story.title
        or any(q in who for who in (n.story.dev_owner, n.story.qa_owner, n.story.pm_owner) if who)
    ]
    return hit, q


def _handle_draft_nudge(result: IntentResult, ctx: ReplyContext) -> str:
    """催办：挑人 → 起草 → 暂存等确认。绝不直接发出去。"""
    try:
        items = nudge.pick_nudges()
    except Exception as e:
        logger.exception("挑催办对象失败")
        return f"读版本计划失败（{type(e).__name__}），稍后再试一次。"

    if not items:
        return nudge.stage(ctx.sender, [])

    matched, scope = _filter_nudges(items, result.query or "")
    if not matched:
        who = "、".join(f"{n.audience}（{n.story.title[:18]}）" for n in items[:5])
        return (
            f"没在待催清单里找到「{scope}」。\n\n"
            f"当前该催的 {len(items)} 条：{who}\n"
            f"可以说「催一下 {items[0].story.version}」或「催办」看全部。"
        )

    # 先落暂存（stage 建批次），再把批次画成卡片。顺序不能反：
    # 卡片按钮带 batch_id，没有批次那卡片就是一堆点不出结果的按钮。
    nudge.stage(ctx.sender, matched)
    batch = nudge.get_batch(ctx.sender)
    title = f"范围：{scope}，{len(matched)}/{len(items)} 条" if scope else ""
    card_note = send_nudge_card(ctx.sender, batch.items if batch else [], title)
    return card_note or ""


def _try_resolve_pending(sender: str, text: str) -> Optional[str]:
    """有待确认的动作时，先接住「确认 / 取消 / 改写」。

    必须走命令层，不能交给意图路由：
    「确认」两个字丢给 LLM 只会被判成闲聊，然后用户以为改成功了。

    两个槽位互斥（同一时刻只有一个在等确认），按写入风险从高到低排：
    改表 > 发消息。
    """
    if weekly_write.has_pending(sender):
        key = text.strip().lower().strip("。.!！")
        if key in weekly_write.CONFIRM_WORDS:
            return weekly_write.commit(sender)
        if key in weekly_write.CANCEL_WORDS:
            return weekly_write.cancel(sender)
        return None

    # 备忘池：往「未开始事项」追加。跟状态回写平级 —— 都是写表，
    # 但一个是改已有行、一个是追加新行，落点不同，各占一个槽位。
    # 词表也分开：「记 / 不记」才是这个语境下他会打的字。
    if weekly_write.has_pending_backlog(sender):
        key = text.strip().lower().strip("。.!！")
        if key in weekly_write.BACKLOG_CONFIRM_WORDS:
            return weekly_write.commit_backlog(sender)
        if key in weekly_write.BACKLOG_CANCEL_WORDS:
            return weekly_write.cancel_backlog(sender)
        return None

    if nudge.has_pending(sender):
        return nudge.handle_pending(sender, text)

    # 每日过审清单的回复。放在最后，因为它的语法最宽松（一串数字），
    # 容易误吃别的待确认输入。
    #
    # 不再用 has_pending() 做前置门：那个门只看「有没有清单」，
    # 不看「这份清单答不答得上他这句话」。一份 0 条的空清单曾把
    # 「5 7已完成」劫走十几个小时。
    # 现在由 handle_pending 自己判断，答不上来返回 None，放行给智能体。
    resolved = triage.handle_pending(sender, text)
    if resolved is not None:
        return resolved

    return None


# ── dispatcher：意图 → handler 查表 ────────────────────────────────────

# 手动指令要拿飞书客户端，但 _COMMANDS 的签名是无参 lambda。
# 用一个模块级引用兜住，run_daemon 启动时赋值。
_FEISHU: Optional[FeishuClient] = None


def _cmd_triage_now() -> str:
    """`/过审` —— 现在就跑一次全量扫描 + 分档推送。

    为什么需要这个入口：18:00 是定时的，但演示、或者他下午想提前看一眼，
    都等不到那个点。全量要 5-7 分钟，必须丢线程，
    否则主循环里的 @Bot 应答会卡死。
    """
    if _FEISHU is None:
        return "我还没连上飞书，等 10 秒再发一次。"
    threading.Thread(target=_run_triage, args=(_FEISHU,), daemon=True).start()
    return "开始扫全部会话，大概 5-7 分钟。扫完清单直接发你私聊。"


_COMMANDS = {
    "/help": lambda: _HELP_TEXT,
    "/过审": _cmd_triage_now,
    "/enable": lambda: "这个群已经默认启用，不需要再绑定。直接发计划或问题就行。",
}

# `/pm ...` 是一整族子命令（帮助 / 自检 / 我是谁 / 表在哪），参数带在命令后面，
# 没法塞进按整串精确匹配的 `_COMMANDS`。单独给一个前缀路由。
_PM_PREFIX = "/pm"
_HANDLERS = {
    INTENT_LOG_PLAN: _handle_log_plan,
    INTENT_UPDATE_STATUS: _handle_update_status,
    INTENT_DRAFT_NUDGE: _handle_draft_nudge,
    INTENT_QUERY_SCHEDULE: _handle_schedule,
    INTENT_QUERY_OWNER: _handle_owner,
    INTENT_QUERY_KB: _handle_kb,
    INTENT_ESTIMATE: _handle_estimate,
    INTENT_CHAT: _handle_chat,
}


def _answer_private(text: str, sender_id: str, chat_id: str) -> str:
    """私聊应答：先本地 PM 能力，判「不归 PM」再转主链。

    为什么由这边判而不是主链抢：
        两个进程都收得到同一条私聊（本进程轮询、主链 WS）。谁答不能靠抢，
        要有唯一判定者。本进程每 3 秒在读那条会话，知道自己判了什么；
        主链不知道，让它猜只能靠关键词表，那套永远会漏。

    转发失败（主链没起 / 502 / 空答复）时退回一句**明确的边界说明**，
    不静默——静默会让人以为消息丢了。
    """
    from app.domains.pm import pm_relay
    from app.domains.pm.pm_agent import RELAY_PREFIX

    reply = process_user_query(text, sender_id)
    if not reply.startswith(RELAY_PREFIX):
        return reply

    raw = reply[len(RELAY_PREFIX):].strip() or text
    logger.info("判定不归 PM，转主链：%s", raw[:60])
    forwarded = pm_relay.forward(raw, sender_id=sender_id, chat_id=chat_id)
    if forwarded:
        logger.info("主链已答复（%d 字）", len(forwarded))
        return forwarded
    if not pm_relay.available():
        return (
            "这件事不归 PM 管，需要主链（工单/发布/CMDB 那些）处理，"
            "但主链这会儿没在跑，我转发不出去。\n"
            "让它起来再发一次，或者你把这句话发到群里。"
        )
    return (
        "这件事不归 PM 管，我转给主链了但没拿到答复（可能超时）。\n"
        "再发一次，或者把这句话发到群里让主链接。"
    )


def _legacy_route(text: str, sender_id: str) -> str:
    """LLM 不可用时的保底路径：确定性意图分类 + handler 查表。

    保留它不是为了双引擎并存，而是为了让模型网关故障时机器人不会彻底哑掉。
    工具能力在 pm_tools 里，这里只负责用关键词把最常用的几条走通。
    """
    result = route_intent(text)
    logger.info(
        "意图=%s conf=%.2f fallback=%s todos=%d target=%s status=%s",
        result.intent, result.confidence, result.fallback_used,
        len(result.todos), result.target, result.status,
    )

    ctx = ReplyContext(sender=sender_id, text=text)
    handler = _HANDLERS.get(result.intent, _handle_chat)
    try:
        return handler(result, ctx)
    except Exception as e:
        logger.exception("handler 执行失败 intent=%s", result.intent)
        return f"这条我没处理成功（{type(e).__name__}），已记日志，换个说法或稍后再试。"


def process_user_query(text: str, sender_id: str) -> str:
    """群消息入口。分派顺序不可调换：

        待确认 > 命令 > 智能体循环 > 旧路由

    前三层都是确定性的：确认/取消必须精确命中，命令必须立刻响应。
    只有落到智能体循环，才交给模型自己决定调什么工具。
    """
    text_clean = text.replace("@_user_1", "").replace("@AgentMate", "").strip()
    if not text_clean:
        return _HELP_TEXT

    logger.info("收到用户指令 sender=%s: %s", sender_id, text_clean)

    resolved = _try_resolve_pending(sender_id, text_clean)
    if resolved is not None:
        logger.info("待确认操作已处理")
        return resolved

    cmd = _COMMANDS.get(text_clean.lower())
    if cmd:
        return cmd()

    # `/pm ...` 是带参数的子命令族，走前缀匹配；不匹配返回 None 继续往下走
    if text_clean.lower().startswith(_PM_PREFIX):
        from app.domains.pm.pm_setup import handle_command

        handled = handle_command(text_clean, sender_id)
        if handled is not None:
            return handled

    try:
        reply = pm_agent.run(text_clean, sender_id)
        if reply:
            return reply
        logger.warning("智能体未产出结果，降级到旧路由")
    except Exception:
        logger.exception("智能体循环异常，降级到旧路由")

    return _legacy_route(text_clean, sender_id)


def _today_records(records: list, today: date) -> list:
    """只保留「计划日期」落在今天的记录。

    原实现把整表 100 条全算进完成率，于是群里出现过 `0/22 (0%)` ——
    22 条里大部分是几天前甚至上月的记录，跟今天没关系。
    """
    start = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=TZ).timestamp() * 1000
    end = start + 24 * 3600 * 1000

    kept = []
    for r in records or []:
        raw = (r.get("fields") or {}).get("计划日期")
        if raw is None:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if start <= ts < end:
            kept.append(r)
    return kept


# ══════════════════════════════════════════════════════════════════════
# 全局感知 —— 群聊 + 私聊
#
# 和「PMO 群 @Bot 应答」是两条独立链路：
#   应答：tenant token，只看得见机器人所在的群，3 秒一轮，要快
#   感知：user token，看得见他全部会话（群 + 私聊），5 分钟一批，要全
# 混在一起会互相拖累，所以单开一个线程。
# ══════════════════════════════════════════════════════════════════════

# 两轮之间的喘息时间。
#
# 别按「轮询间隔」理解：一轮本身要四五分钟（76 个会话 × 3.8s），
# sleep 只是让出 CPU 喘口气。原来 180s，等于每轮多压 3 分钟，
# 冷区轮转被拖到一百多分钟一遍 —— 实测 scan_at 中位数是 108 分钟没被扫过。
# 压到 30s 后冷区一轮降到 30 分钟上下。
WATCH_INTERVAL_SEC = 30

# 感知到但「不急着看」的事件攒在这里，日计划时一起给他
WATCH_PENDING_PATH = PROJECT_DIR / ".watch_pending.json"


def _append_pending_events(events):
    """把非紧急事件按天攒起来。同一天内按 message_id 去重。"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    data = {"date": today, "events": []}
    try:
        if WATCH_PENDING_PATH.exists():
            old = json.loads(WATCH_PENDING_PATH.read_text(encoding="utf-8"))
            if old.get("date") == today:
                data = old
    except Exception:
        pass

    seen = {e.get("message_id") for e in data["events"]}
    for e in events:
        if e.message_id in seen:
            continue
        data["events"].append({
            "message_id": e.message_id,
            "chat_name": e.chat_name,
            "chat_mode": e.chat_mode,
            "sender_name": e.sender_name,
            "text": e.text,
            "ts": e.ts,
            "reason": e.reason,
        })
        seen.add(e.message_id)

    try:
        WATCH_PENDING_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写感知暂存失败: %s", e)


def load_pending_events() -> list:
    """取当天的感知事件。跨天自动作废 —— 隔夜的事已经过时了。"""
    try:
        data = json.loads(WATCH_PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if data.get("date") != datetime.now(TZ).strftime("%Y-%m-%d"):
        return []
    return data.get("events") or []


def render_pending_digest(limit: int = 8) -> str:
    """把攒下的事件渲染成摘要。没内容返回空串，不占版面。"""
    events = load_pending_events()
    if not events:
        return ""
    events.sort(key=lambda e: -int(e.get("ts") or 0))
    lines = [f"【群里有动静 · {len(events)} 条】"]
    for e in events[:limit]:
        t = time.strftime("%m-%d %H:%M", time.localtime(int(e.get("ts") or 0) / 1000))
        where = f"私聊·{e['chat_name']}" if e.get("chat_mode") == "p2p" else e.get("chat_name", "")
        lines.append(f"{t} {where} · {e.get('sender_name', '')}")
        lines.append(f"  {str(e.get('text') or '')[:60]}")
    if len(events) > limit:
        lines.append(f"…另有 {len(events) - limit} 条")
    return "\n".join(lines)


def _render_followup(today: date) -> str:
    """问出去之后的闭环：谁动了、谁还停着。

    这块以前只能在工具里查 —— 得他自己想起来问一句「问了的那几条怎么样了」。
    跟着 09:00 的日计划一起送到私聊，今天该追谁是一眼能看到的。

    回读要打飞书项目（Meego），失败就当没有：不能因为它拖垮整条推送。
    """
    try:
        items = pm_followup.poll(today)
    except Exception as e:
        logger.warning("回读问询进展失败（不影响推送）: %s", e)
        return ""
    if not items:
        return ""
    return "【问出去的，看有没有动静】\n" + pm_followup.render(items)


def _deliver_watch_events(feishu: FeishuClient, events):
    """分两路：直接找我的立刻私聊推，其余攒到日计划。"""
    if not events:
        return

    # 自己发的消息也进了快照（用来判「我回过没有」），但绝不能推给自己。
    events = [e for e in events if getattr(e, "sender_id", "") != PM_OPEN_ID]
    # 已经回过的别再推。它仍然在快照里，过审会把它算成「已处理」，
    # 但「有人找你」这条路只推还没回应的。
    events = [e for e in events if not getattr(e, "replied", False)]
    if not events:
        return

    direct = [e for e in events if getattr(e, "directed", False)]
    direct_ids = {e.message_id for e in direct}
    rest = [e for e in events if e.message_id not in direct_ids]

    if direct:
        direct.sort(key=lambda e: e.ts)
        body = "\n\n".join(e.render() for e in direct[:10])
        tail = f"\n\n…另有 {len(direct) - 10} 条" if len(direct) > 10 else ""
        try:
            feishu.send_user_text(PM_OPEN_ID, f"【有人找你 · {len(direct)} 条】\n\n{body}{tail}")
            logger.info("已私聊推送 %d 条直接找我消息", len(direct))
        except Exception as e:
            logger.warning("私聊推送失败: %s", e)

    # 第三路：没人 @ 我，但事情推进到了该问一句的节点。
    # 这一路以前不存在 —— 这类消息只能等 18:00 过审才被看到，追问已经晚了。
    try:
        picks = pm_proactive.pick(rest)
        block = pm_proactive.render(picks)
        if block:
            feishu.send_user_text(PM_OPEN_ID, block)
            logger.info("主动开口 %d 件: %s", len(picks), "、".join(p.stage.key for p in picks))
    except Exception as e:
        logger.warning("主动开口失败（不影响采集）: %s", e)

    if rest:
        _append_pending_events(rest)
        logger.info("感知到 %d 条相关动静，已暂存待日计划汇总", len(rest))


# ── 催办卡片 ──────────────────────────────────────────────────────
#
# 卡片不是「好看版文本」，它是**唯一能拦住误发**的入口：催办是以他名义发给
# 别人的消息，纯文本确认靠手打「确认」两个字，而这动作不可撤回。
#
# 三件事分开：`pm_nudge_card` 画卡片、`pm_nudge_actions` 处理点击、
# `pm_card_server` 收飞书的回调。daemon 只负责把三者与飞书客户端接起来。
_NUDGE_ACTIONS = None


def _nudge_actions():
    """懒建卡片动作处理器（要等 `_FEISHU` 就位）。"""
    global _NUDGE_ACTIONS
    if _NUDGE_ACTIONS is None:
        from app.domains.pm import pm_nudge_actions

        _NUDGE_ACTIONS = pm_nudge_actions.make_actions(
            send_text=lambda target, text: _feishu_send_text(target, text),
            send_card=lambda target, payload: _feishu_send_card(target, payload),
            update_card=lambda mid, payload: _feishu_update_card(mid, payload),
        )
    return _NUDGE_ACTIONS


def _feishu_send_text(target: str, text: str) -> None:
    if _FEISHU is not None:
        _FEISHU.send_user_text(target, text) if target.startswith("ou_") else             _FEISHU.send_chat_text(target, text)


def _feishu_send_card(target: str, payload: dict) -> str:
    """发卡片并取 message_id。取不到就返回空串（调用方会退化处理）。"""
    from app.domains.pm.pm_confirm_card import card_message_id, send_interactive_card

    if _FEISHU is None:
        return ""
    try:
        return card_message_id(send_interactive_card(target, payload))
    except Exception:
        logger.exception("发催办卡片失败")
        return ""


def _feishu_update_card(message_id: str, payload: dict) -> bool:
    """就地改原卡片。走 lark-cli 的 bot 身份（与发送同一条链路）。"""
    import subprocess

    from app.domains.pm.pm_confirm_card import NO_WINDOW

    lark_bin = "lark-cli.cmd" if shutil.which("lark-cli.cmd") else "lark-cli"
    cmd = [
        lark_bin, "api", "PATCH", f"/open-apis/im/v1/messages/{message_id}",
        "--data", "-",
        "--as", "bot",
    ]
    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        res = subprocess.run(
            cmd,
            input=json.dumps({"content": json.dumps(payload, ensure_ascii=False)},
                             ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8",
            env=env, shell=True, creationflags=NO_WINDOW,
        )
    except Exception:
        logger.exception("更新催办卡片失败")
        return False
    if res.returncode != 0:
        logger.warning("更新卡片返回非零: %s", (res.stderr or "")[:200])
        return False
    return True


def _push_daily_brief(today) -> None:
    """日计划推送：卡片为主，文本兜底。

    为什么卡片优先：早上第一眼要回答「今天要收什么」，纯文本三块等权重平铺，
    卡点与计划挤在一起扫不动。卡片分区 + 挂账徽标 + 底部直达按钮。
    为什么还留文本：卡片构造/发送任何一步失败时，信息不能丢。
    """
    from app.domains.pm import pm_daily_card, pm_daily_brief

    brief = pm_daily_brief.build_daily_brief(today) if today else pm_daily_brief.build_daily_brief()
    target = PMO_GROUP_ID or PM_OPEN_ID
    try:
        payload = pm_daily_card.build_daily_card(brief)
        from app.domains.pm.pm_confirm_card import send_interactive_card

        send_interactive_card(target, payload)
        return
    except Exception:
        logger.exception("日计划卡片发送失败，回落文本")
    text = pm_daily_brief.render_daily_brief(brief)
    if PMO_GROUP_ID:
        send_group_message(PMO_GROUP_ID, text)
    else:
        feishu = _FEISHU
        if feishu is not None:
            feishu.send_user_text(PM_OPEN_ID, text)


def _start_card_server() -> None:
    """起卡片回调服务。起不来只告警——文字口令仍然可用，不是致命故障。"""
    from app.domains.pm import pm_card_server

    def _handle(event: dict) -> str:
        return _nudge_actions().handle(event)

    pm_card_server.start(_handle)


def send_nudge_card(target: str, items: list, title: str = "") -> str:
    """把一批催办草稿渲染成卡片发出去，并回填 message_id。返回人话结果。

    这是**唯一**的催办出口：文本预览与卡片共用同一份 `pm_nudge._PENDING`，
    卡片发不出去时回落到文本，两条路都能确认。
    """
    from app.domains.pm import pm_nudge_card as card
    from app.domains.pm.pm_confirm_card import send_interactive_card

    batch = __import__("app.domains.pm.pm_nudge", fromlist=["x"]).get_batch(target)
    if batch is None:
        return "没有待确认的催办草稿。"
    payload = card.build_nudge_card(items, batch.batch_id, title)
    try:
        resp = send_interactive_card(target, payload)
    except Exception:
        logger.exception("催办卡片发送失败，回落文本预览")
        from app.domains.pm import pm_nudge

        return pm_nudge.stage(target, [it.nudge for it in items])

    from app.domains.pm.pm_confirm_card import card_message_id

    mid = card_message_id(resp)
    from app.domains.pm import pm_nudge

    pm_nudge.attach_message_id(target, mid)
    if not mid:
        logger.warning("卡片发出但没拿到 message_id，后续点击会退化成发新卡")
    return ""


def _run_triage(feishu: FeishuClient):
    """18:00 的每日过审。两阶段：先全量备料，再从快照分档。

    以前是一个阶段 —— collect() 自己遍历 278 个会话。问题是感知线程同时在跑，
    两边都拉 lark-cli，互相拖死。2026-11-01 那次号称全量，实际只采到 4 条消息、
    0 条相关，推了一份空清单还锁了他一整天。

    拆开之后：备料慢没关系（6-7 分钟，没人等），分档是毫秒级的。
    """
    try:
        snap = pm_watch.full_sweep()
        logger.info("过审备料完成：%d 条相关（已扫描 %d 个会话）",
                    len(snap.events), snap.stats.get("scanned", 0))

        blocks = triage.run_daily_blocks(PM_OPEN_ID)
        for block in blocks:
            feishu.send_user_text(PM_OPEN_ID, block)
            time.sleep(0.4)  # 连发太快会被限流
        logger.info("每日过审清单已推送，%d 条消息", len(blocks))
    except Exception as e:
        logger.exception("每日过审失败: %s", e)
        try:
            feishu.send_user_text(
                PM_OPEN_ID, f"今天的过审没跑成（{type(e).__name__}），日志里有详情。"
            )
        except Exception:
            pass


def _watch_loop(feishu: FeishuClient):
    """全局感知线程。只扫热区，不扫全量。

    为什么不全量：278 个会话 × 1.43s ≈ 6.6 分钟。以前靠「每轮扫 20 个轮转」
    绕开这个问题，代价是同一个群要 50 分钟才轮到一次 —— 那不叫感知。
    现在改成：白天只盯最近有动静的会话（热区），18:00 由 _run_triage 做一次全量。

    为什么单开线程：lark-cli 每次调用要 1-2 秒（node 启动），塞进主循环会把
    @Bot 的响应拖到几十秒以上，群里的交互直接废掉。
    """
    time.sleep(60)  # 启动后先缓一分钟，避免重启时把刚看过的消息再推一遍
    while True:
        try:
            state = pm_watch.WatchState.load()
            snap = pm_watch.Snapshot.load()
            if snap.reset_if_stale(datetime.now(TZ).strftime("%Y-%m-%d")):
                snap.save()
            events = pm_watch.poll(state, limit=pm_watch.BATCH_SIZE, snapshot=snap)
            _deliver_watch_events(feishu, events)
        except Exception as e:
            logger.exception("全局感知扫描异常: %s", e)
        time.sleep(WATCH_INTERVAL_SEC)


# 授权观察者的单例标记：一个用户同时只该有一个在等，重复起线程会重复通知。
_AUTH_WATCH = False


def _maybe_send_auth_qr(feishu: FeishuClient, text: str, sender_id: str) -> None:
    """用户发 `/pm 授权` 后，把二维码图片单独发一条，并起一个「等授权」的观察者。

    为什么单独发而不是塞进文字里：飞书文本消息不认图片，只能分两条。
    顺序是「先链接后二维码」——链接可以复制、二维码用来扫，两种场景都覆盖。

    为什么还要起观察者：用户在浏览器点完「确认授权」之后，飞书**不会回调我们
    任何东西**。不主动查，就只能等他记得回来说一声；他忘了说，体验就是
    「扫完了然后没反应」。观察者只轮询本地凭据目录，不消耗飞书接口配额。

    失败不抛：二维码只是增强，文字里的链接已经够用。
    """
    if text.lower() not in ("/pm 授权", "/pm 登录", "/pm auth"):
        return
    from app.domains.pm.pm_setup import _read_pending

    pending = _read_pending()
    url = str(pending.get("verification_url") or "")
    if not url:
        return
    try:
        import subprocess
        import tempfile

        from app.domains.pm.pm_win import NO_WINDOW
        from app.protocols.feishu_send import send_image

        # 三个坑叠在一起，实测才定下来的写法：
        # 1. `--output` 只接受 cwd / /tmp / ~/files 三种根下的路径，仓库里的
        #    绝对路径会被拒（错误文案是误导性的「output file path is required」）；
        # 2. 链接里的 `&` 在 shell 下会被 cmd.exe 当命令分隔符，URL 被拦腰切掉；
        # 3. 但 lark-cli 在 Windows 上只有 `lark-cli.cmd` 这个批处理壳，
        #    不开 shell 起不来。
        # 结论：shell=True + **给 URL 和路径都加引号**，两者同时满足。
        out_png = Path(tempfile.gettempdir()) / "pm_auth_qr.png"
        cmd = f'lark-cli auth qrcode "{url}" --output "{out_png}"'
        subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, shell=True, creationflags=NO_WINDOW,
        )
        if out_png.is_file():
            send_image(sender_id, str(out_png))
            logger.info("已发送授权二维码给 %s", sender_id[:12])
    except Exception as e:
        logger.warning("发送授权二维码失败（文字里的链接仍可用）: %s", e)
        return

    # 观察者：授权完成就主动汇报，并把登录收尾。用户不用说「好了」。
    _watch_auth_completion(sender_id, str(pending.get("open_id") or ""),
                           str(pending.get("device_code") or ""),
                           int(pending.get("expires_at") or 0))


def _watch_auth_completion(sender_id: str, open_id: str, device_code: str,
                           expires_at: int) -> None:
    """后台等授权结果，完成后收尾 + 主动通知。只起一个观察者。"""
    global _AUTH_WATCH  # noqa: PLW0603  单例标记，避免重复起线程
    if _AUTH_WATCH:
        return
    import threading

    _AUTH_WATCH = True

    def _run() -> None:
        global _AUTH_WATCH
        try:
            from datetime import datetime

            from app.domains.pm.pm_identity import finish_login, wait_ready
            from app.protocols.feishu_send import send_text

            # 剩余有效期就给这么多，最多 10 分钟；少留 30 秒给收尾
            left = max(30, int(expires_at - datetime.now().timestamp()) - 30)
            ready = wait_ready(open_id, timeout=min(left, 600), interval=5.0)
            if not ready:
                logger.info("授权观察超时，放弃等待 %s", open_id[:12])
                return
            ok, msg = finish_login(open_id, device_code)
            if ok:
                send_text(sender_id,
                          f"{msg}\n\n"
                          "PM 工作台已经就绪，到点我会把日计划、提测询问和复盘推到这里。\n"
                          "发 `/pm 检查` 可以看全套能力状态。")
                logger.info("授权完成并已通知 %s", sender_id[:12])
            else:
                send_text(sender_id, f"授权看起来完成了，但登录没接上：{msg}\n"
                                     "发 `/pm 授权完成` 我再试一次。")
        except Exception as e:
            logger.exception("授权观察者异常: %s", e)
        finally:
            _AUTH_WATCH = False

    threading.Thread(target=_run, daemon=True).start()


def _resolve_p2p_chat(state: dict) -> str:
    """找出「PM ↔ AgentMate」的私聊 chat_id。
    为什么必须找到它
    ----------------
    推送全走私聊（`send_user_text` 按 open_id 发），但接收原来只轮询 PMO 群。
    两条路走的不是同一个会话：他收到的推送在私聊里，回的话也在私聊里，
    而 daemon 盯着群 —— 他回什么都收不到。2026-10-29 就是这么丢的：
    他 19:34 回「这些都和我没关系」，日志里一个字都没有。

    来源按可靠性排序：

    1. 发送接口回写的 `p2p_chat_id`（`send_user_text` 顺手落盘，最可靠）
    2. 本会话里「和他本人」的那条 p2p（`.watch_state.json`，`target_type=user`）
    3. 会话清单里和机器人同名的 p2p（旧口径，保留兼容）

    第 2 条是给「刚接进来、还没发过消息」的人用的：换人之后发送回写还不存在，
    而感知层已经把「他和某个人的私聊」记在会话清单里了——按 open_id 找，
    比按机器人名字找更准（机器人名字可以改，open_id 不会）。
    """
    cid = state.get("p2p_chat_id")
    if cid:
        return cid

    # 兜底：从感知层的会话清单里找
    try:
        st = json.loads((PROJECT_DIR / ".watch_state.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    chats = st.get("chats") or {}

    # ② 按「对方就是 PM 本人」找（p2p 的 target 是 user，id 就是他的 open_id）
    if PM_OPEN_ID:
        for chat_id, meta in chats.items():
            if meta.get("mode") != "p2p":
                continue
            if meta.get("target_type") == "user" and meta.get("target_id") == PM_OPEN_ID:
                state["p2p_chat_id"] = chat_id
                _save_state(state)
                logger.info("定位到 PM 本人私聊会话: %s", chat_id)
                return chat_id

    # ③ 旧口径：找和机器人同名的 p2p
    for chat_id, meta in chats.items():
        if meta.get("mode") != "p2p":
            continue
        if meta.get("target_type") != "bot":
            continue
        if (meta.get("name") or "").strip() == BOT_NAME:
            state["p2p_chat_id"] = chat_id
            _save_state(state)
            logger.info("定位到机器人私聊会话: %s", chat_id)
            return chat_id
    return ""


def _refresh_bot_identity(feishu) -> None:
    """把机器人自己的 open_id 与显示名拉到模块常量里（启动时调一次）。

    两个值都必须在运行期取：open_id 写死会让「@我」的判定在换 app 后全面失效，
    显示名写死会让私聊会话定位返回空——两种失败都不抛异常，只是功能静默消失。
    """
    global BOT_OPEN_ID, BOT_NAME
    try:
        info = feishu.bot_info()
    except Exception as e:
        logger.warning("取 bot 身份失败（@我 与私聊定位会受影响）: %s", e)
        return
    oid = str(info.get("open_id") or "")
    name = str(info.get("app_name") or "").strip()
    if oid:
        BOT_OPEN_ID = oid
    if name:
        BOT_NAME = name
    logger.info("bot 身份: open_id=%s name=%s", BOT_OPEN_ID or "（未取到）", BOT_NAME)


def run_daemon():
    global BOT_OPEN_ID, BOT_NAME

    feishu = FeishuClient(APP_ID, APP_SECRET)
    # 机器人自己的 open_id 与显示名在运行期取（`/bot/v3/info`）。
    # 写死的话换 app 之后「@我」会被判成「@了别人」、私聊通道也会定位失败，
    # 两种失败都不报错，只是功能静默消失。
    _refresh_bot_identity(feishu)
    global _FEISHU
    _FEISHU = feishu  # 供 `/过审` 这类手动指令取用

    # 卡片回调服务：飞书把按钮点击 POST 到本机 9113（主链代收后转过来）。
    # 起不来不拦启动——文字口令（「确认」/「第2条改成：…」）仍然可用，
    # 只是按钮点下去没反应，日志里会说明原因。
    _start_card_server()

    logger.info("==========================================")
    logger.info("  🚀 PM Hub 专属守护进程已启动")
    logger.info(f"  群聊目标: {PMO_GROUP_ID or '（未配置，群推送将跳过）'}")
    logger.info(f"  Bot OpenID: {BOT_OPEN_ID or '（未取到，@我 判定会失效）'}")
    logger.info("==========================================")

    state = _load_state()

    # 上线问候只在上次启动超过 6 小时前才发。改代码重启一次就刷一条「已恢复在线」
    # 是纯噪音，群里另外一个人看到只会觉得这机器人在自我表演。
    #
    # 群没配（`[projects.pm.push].group_id` 为空）时改发私聊：不配群是「只发私聊」
    # 的合法配置，不是缺失——往空 chat_id 发只会失败，一次启动问候就丢了。
    last_boot = float(state.get("boot_at") or 0)
    if time.time() - last_boot > 6 * 3600:
        greeting = (
            "AgentMate 已恢复在线。\n"
            "群聊监听已开，09:00 晨间雷达与 17:30 日清复盘照常推送。"
            "发 /help 看能做什么。"
        )
        try:
            if PMO_GROUP_ID:
                feishu.send_chat_text(PMO_GROUP_ID, greeting)
            else:
                feishu.send_user_text(PM_OPEN_ID, greeting)
        except Exception as e:
            logger.warning("启动问候未发出（不影响主循环）: %s", e)
    state["boot_at"] = time.time()
    _save_state(state)

    last_processed_msg_id = ""

    # 全局感知线程：群聊 + 私聊。和主循环互不阻塞，详见 _watch_loop 的注释。
    threading.Thread(target=_watch_loop, args=(feishu,), daemon=True).start()
    logger.info("全局感知线程已启动，间隔 %ds", WATCH_INTERVAL_SEC)

    slots = _read_slots()
    if slots:
        logger.info(
            "定时账本: 晨间=%s 晚间=%s 过审=%s",
            slots.get("morning", "-"), slots.get("evening", "-"), slots.get("triage", "-"),
        )

    # 先拉一次最新的 message_id 避免把历史消息当做新消息重复处理
    # 群未配置时跳过：空 chat_id 拉消息只会报 230002，那行日志除了噪音没有信息。
    init_msgs = feishu.list_messages(PMO_GROUP_ID, page_size=1) if PMO_GROUP_ID else []
    if init_msgs:
        last_processed_msg_id = init_msgs[0].get("message_id", "")
        logger.info(f"初始化最新消息基线: {last_processed_msg_id}")

    # 私聊通道。推送全走私聊，所以接收也必须盯私聊，否则他回什么都收不到。
    p2p_chat_id = _resolve_p2p_chat(state)
    last_p2p_msg_id = ""
    if p2p_chat_id:
        init_p2p = feishu.list_messages(p2p_chat_id, page_size=1)
        if init_p2p:
            last_p2p_msg_id = init_p2p[0].get("message_id", "")
        logger.info("私聊通道就绪: %s（基线 %s）", p2p_chat_id, last_p2p_msg_id)
    else:
        logger.warning(
            "未找到机器人私聊会话 —— 他私聊回的指令会收不到。"
            "先给他发一条私聊再重启，或等下一次推送自动回写 chat_id。"
        )

    while True:
        try:
            now = datetime.now(TZ)
            today_str = now.strftime("%Y-%m-%d")

            # ── 1. 定时任务触发 ──
            if now.weekday() < 5:  # 工作日
                # 09:00 双轨日计划推送（运营轨 sht001 + 研发轨 sht000）
                if (now.hour == 9 and now.minute >= 0) or (now.hour > 9 and now.hour < 12):
                    if _claim_today("morning", today_str):
                        logger.info(f"触发定时日计划推送: {today_str}")
                        # 多维表格同步是附加动作，失败不能拖垮推送
                        try:
                            vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
                            radar = evaluate_daily_radar(parse_stories(vals), today=now.date())
                            sync_tasks_to_bitable(generate_today_tasks(radar, today_dt=now.date()))
                        except Exception as e:
                            logger.exception(f"研发轨同步多维表格失败（不影响推送）: {e}")
                        # 群未配置 = 只发私聊（`[projects.pm.push].group_id` 空）。
                        # 这不是缺失，是最小可用形态：日计划照样推到私聊里。
                        if PMO_GROUP_ID:
                            _push_daily_brief(now.date())
                        # 群里的动静不进 PMO 群（易山武也在），单独私聊给他
                        digest = render_pending_digest()
                        daily_todos = triage.render_open_todos()
                        followup_block = _render_followup(now.date())
                        # 知识库缺口只在周一出现一次 —— 他说过这三块不急，
                        # 每天提一句等于拿噪音换存在感。
                        gaps_block = kb_gaps.render_weekly(now.date())

                        # 决策队列：先把当天的消息灌进事件日志，再按
                        # 「需要你出手 / 等你回 / 知道一下」分档。
                        # 放在最前面 —— 他早上第一眼要看的是「我今天得动什么」，
                        # 不是逐条读消息。
                        try:
                            pm_thread.ingest(now.date())
                            # include_follow=False：那一档交给下面的备忘池讲，
                            # 否则同一天两条消息说同一批事。
                            decision = pm_thread.queue(now.date(), include_follow=False)
                        except Exception as e:
                            logger.exception("决策队列生成失败（不影响推送）: %s", e)
                            decision = ""

                        # 「跟」类的事（等时机、等别人回话）不进今天的待办 ——
                        # 天天提醒等于噪音，他的原话是「比较久远、短时间不急」。
                        # 只问一句要不要记进工作簿「未开始事项」，攒着慢慢跟。
                        # 拼在同一条推送里发，不另起一条。
                        backlog_note = ""
                        try:
                            fresh = _backlog_fresh(pm_thread.backlog_items(now.date()))
                            if fresh:
                                backlog_note = weekly_write.stage_backlog(
                                    PM_OPEN_ID,
                                    [
                                        weekly_write.BacklogDraft(
                                            task=i["task"],
                                            priority=i["priority"],
                                            owner=i["owner"],
                                            remark=i["remark"],
                                        )
                                        for i in fresh
                                    ],
                                )
                                _backlog_mark(
                                    [i.get("key") or i["task"] for i in fresh], now.date()
                                )
                                logger.info(
                                    "备忘池：暂存 %d 条待确认（%s）",
                                    len(fresh),
                                    "、".join(i["task"] for i in fresh),
                                )
                        except Exception as e:
                            logger.exception("备忘池暂存失败（不影响推送）: %s", e)

                        if (
                            decision or backlog_note or digest
                            or daily_todos or followup_block or gaps_block
                        ):
                            # 有欠着的提测询问就先补发，没发成的在末尾点名——
                            # 「哪天那条没发出去」必须说出来，否则漏一次就是整周没人核对。
                            missed = _flush_nudge_ask(feishu, today_str)
                            tail = (
                                "⚠️ 这些提测询问还没发出去，我会继续重试：" + "、".join(missed)
                                if missed else ""
                            )
                            feishu.send_user_text(
                                PM_OPEN_ID,
                                "\n\n".join(
                                    x for x in (
                                        decision, backlog_note, daily_todos,
                                        followup_block, gaps_block, digest, tail,
                                    ) if x
                                ),
                            )

                # 提测询问节拍已提到 while 循环层（与 morning 解耦），见下方 if 块

                # 17:30 晚报复盘与发问
                if (now.hour == 17 and now.minute >= 30) or (now.hour >= 18 and now.hour <= 22):
                    if _claim_today("evening", today_str):
                        logger.info(f"触发定时晚间复盘: {today_str}")
                        vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
                        stories = parse_stories(vals)
                        radar = evaluate_daily_radar(stories, today=now.date())
                        records = _today_records(fetch_existing_records(), now.date())
                        card = build_evening_review_card(records, radar)
                        # 群未配置时发私聊：复盘卡是每天最后一条，丢了就等于没做。
                        send_interactive_card(PMO_GROUP_ID or PM_OPEN_ID, card)

                # 17:30 让他排明天的待跟进；18:00 还没给就补一句，之后不再烦
                try:
                    if intake.need_ask(now):
                        intake.mark_asked()
                        feishu.send_user_text(PM_OPEN_ID, intake.ask_text())
                        logger.info("已发出待跟进排期询问")
                    elif intake.need_remind(now):
                        intake.mark_reminded()
                        feishu.send_user_text(PM_OPEN_ID, intake.remind_text())
                        logger.info("已发出待跟进排期提醒")
                except Exception as e:
                    logger.exception("待跟进排期提问失败: %s", e)

            # ── 1.4 提测询问（自己的槽、自己的 try，与 morning 平级）──
            #
            # 为什么从 morning 分支里提出来：原来它缩在 `if _claim_today("morning")`
            # 内部，morning 槽一旦被别的路径先占（重启补推、手动触发），当天
            # nudge 就不执行，哪怕它自己的槽还空着。两个语义完全不同的动作
            # 共用一个 if，失败方式是静默的。
            #
            # 只起草不发送：他改完说「确认」才真发（pm_nudge.commit）。
            #
            # 30 分钟一次的探测节流：主循环是 3 秒一轮，而「飞书项目 MCP 没配」
            # 这类失败在秒级内不可能自愈。不节流会一天刷两万行同样的告警，
            # 真出别的问题时被淹掉。节流值是**探测频率**不是发送频率——
            # 发送由 `_claim_today("nudge_ask")` 保证一天一次。
            if now.weekday() < 5 and _probe_due("nudge_ask", 30 * 60):
                try:
                    due = nudge.due_iterations(now.date())
                    if due:
                        # 先落盘再发：落盘失败也不阻止本次发送——
                        # 「今天发出去」比「账本好看」重要。
                        _enqueue_nudge_ask(
                            [{"iteration": it.name, "due": today_str} for it in due],
                            today_str,
                        )
                        items = nudge.nudges_from_iteration(due, today=now.date())
                        if items and _claim_today("nudge_ask", today_str):
                            feishu.send_user_text(PM_OPEN_ID, nudge.stage(PM_OPEN_ID, items))
                            # 文本预览保留（历史习惯 + 卡片发不出去时的兜底），
                            # 卡片是真正能点的那份
                            send_nudge_card(PM_OPEN_ID, items)
                            _write_nudge_pending([
                                r for r in _read_nudge_pending()
                                if r.get("iteration") not in {it.name for it in due}
                            ])
                            logger.info(
                                "已起草 %d 条提测询问，命中 %d 个迭代：%s",
                                len(items), len(due), "、".join(i.name for i in due),
                            )
                except Exception as e:
                    logger.exception("起草提测询问失败: %s", e)

            # ── 1.5 18:00 每日过审（群聊 + 私聊清单）──
            # 全量扫 270 个会话要五六分钟，必须单开线程，否则主循环里的 @Bot 应答
            # 会被卡死。这也是为什么它不放在上面那个工作日分支里：
            # 周末也想看，漏看的成本比多看一眼高。
            if (now.hour == 18 and now.minute >= 0) or (now.hour >= 19 and now.hour <= 22):
                if _claim_today("triage", today_str):
                    logger.info(f"触发每日过审: {today_str}")
                    threading.Thread(
                        target=_run_triage, args=(feishu,), daemon=True
                    ).start()

            # ── 2.5 私聊通道 ──
            # 推送全走私聊，接收如果只看群，他回什么都白回。曾经因为只读群，
            # 丢了「这些都和我没关系」的反馈，他以为卡住了。
            if not p2p_chat_id:
                p2p_chat_id = _resolve_p2p_chat(_load_state())
                if p2p_chat_id:
                    init_p2p = feishu.list_messages(p2p_chat_id, page_size=1)
                    last_p2p_msg_id = init_p2p[0].get("message_id", "") if init_p2p else ""
                    logger.info("私聊通道已补上: %s", p2p_chat_id)

            if p2p_chat_id:
                try:
                    p_msgs = feishu.list_messages(p2p_chat_id, page_size=5)
                    if p_msgs:
                        fresh = []
                        for m in p_msgs:
                            if m.get("message_id") == last_p2p_msg_id:
                                break
                            fresh.append(m)

                        if fresh:
                            last_p2p_msg_id = p_msgs[0].get("message_id", "")
                            for m in reversed(fresh):
                                sender_id = (m.get("sender") or {}).get("id", "")
                                # 只认他本人。机器人自己发的也会被拉回来，
                                # 不挡掉就会变成自己跟自己对话。
                                if sender_id != PM_OPEN_ID:
                                    continue
                                try:
                                    body = json.loads(m.get("body", {}).get("content", "{}"))
                                except Exception:
                                    body = {}
                                text = (body.get("text") or "").strip()
                                if not text:
                                    continue
                                logger.info("私聊收到指令: %s", text[:80])
                                # 今天问过排期的话，他这一句就算给了，今晚不再提醒
                                if intake.note_reply():
                                    logger.info("已记下他给了明天的待跟进排期")
                                # `/dev` 是主链的 Claude Code 通道，daemon 没有客户端。
                                # 静默跳过：让主链答，不刷一条「不认识这个命令」。
                                if text.startswith("/dev"):
                                    logger.info("私聊里收到 /dev，交给主链处理（daemon 不接）")
                                    continue
                                reply = _answer_private(text, sender_id, p2p_chat_id)
                                feishu.reply_text(m.get("message_id"), reply)
                                # `/pm 授权` 除了文字还要把二维码图片发过去——
                                # 手机上复制 URL 再打开很别扭，扫码一步到位。
                                _maybe_send_auth_qr(feishu, text.strip(), sender_id)
                                logger.info("已私聊回复")
                except Exception as e:
                    logger.exception(f"私聊通道异常: {e}")

            # ── 2. 轮询群消息监听 @Bot ──
            # 群未配置时整段跳过：这条链路只服务「群里 @ 机器人」，
            # 没有群就没有可 @ 的地方，轮询空 chat_id 只会每 3 秒报一次错。
            msgs = feishu.list_messages(PMO_GROUP_ID, page_size=5) if PMO_GROUP_ID else []
            if msgs:
                new_msgs = []
                for m in msgs:
                    m_id = m.get("message_id")
                    if m_id == last_processed_msg_id:
                        break
                    new_msgs.append(m)

                if new_msgs:
                    last_processed_msg_id = msgs[0].get("message_id")
                    # 从旧到新处理
                    for m in reversed(new_msgs):
                        mentions = m.get("mentions", [])
                        is_at_bot = any(men.get("id") == BOT_OPEN_ID for men in mentions)

                        if is_at_bot:
                            content_raw = m.get("body", {}).get("content", "{}")
                            try:
                                body = json.loads(content_raw)
                            except Exception:
                                body = {}
                            text = body.get("text", "")
                            sender_id = m.get("sender", {}).get("id", "")
                            msg_id = m.get("message_id")

                            logger.info(
                                f"捕获到 @AgentMate 消息: id={msg_id}, sender={sender_id}, text={text}"
                            )
                            reply_text = process_user_query(text, sender_id)
                            feishu.reply_text(msg_id, reply_text)
                            logger.info("已完成群内回复！")

        except Exception as e:
            logger.exception(f"守护循环异常: {e}")

        time.sleep(3)


if __name__ == "__main__":
    run_daemon()