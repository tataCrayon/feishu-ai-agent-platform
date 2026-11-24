"""PM Watch - 全局感知层

他要的不是「盯一个群」，是「盯我所有的信息入口」：

    所有群（不是 PMO 那个只有 2 个人的空群）
    所有私聊
    妙记会议纪要
        │
        └─► 筛掉无关噪音，只留跟我有关系的

三条硬约束
----------
1. **必须用用户身份**。bot 只在被拉进的群里能读消息，而他要的是全部。
   所以这里一律 `--as user`，走李四自己的权限。

2. **必须增量**。几十个会话每次全量拉，API 会被打限流。
   每个会话记 last_ts，只要更新的；每轮只扫最久没扫的一批，轮转覆盖。

3. **必须筛选**。不筛就是把所有群的噪音搬到群里，比不做还糟。
   规则见 is_relevant()，宁可漏报也不要刷屏。

为什么不复用 pm_daemon.FeishuClient
----------------------------------
那个客户端用的是 tenant_access_token（机器人身份），只能看机器人所在的群。
感知必须用 user_access_token，所以走 lark-cli --as user。
两个身份不能混用，混了就会出现「这个群读不到」的怪问题。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domains.pm.feishu_pm_sync import _run_lark_cli
from app.domains.pm.pm_config import pm_config
from app.domains.pm.pm_hub_engine import PM_OPEN_ID, SPREADSHEET_TOKEN, SHEET_ID_VERSIONS, fetch_sheet_values, parse_stories
from app.domains.pm import pm_people, pm_scope

logger = logging.getLogger("pm_hub.watch")

_STATE_PATH = Path(__file__).resolve().parents[3] / ".watch_state.json"

# 快照：当天已采集到的「相关消息」。和 _STATE_PATH 分开存，因为职责不同 ——
# state 是游标（我扫到哪里了），snapshot 是证据（今天到底有哪些事）。
_SNAP_PATH = Path(__file__).resolve().parents[3] / ".watch_snapshot.json"

# 会话清单缓存多久。群列表不常变，新群发现晚一小时没影响 ——
# 但每次刷新要拉 14 页 chat-list（约 17 秒），太勤了纯属浪费。
CHAT_LIST_TTL_SEC = 3600

# 每轮最多扫几个会话。lark-cli 每次调用要 1-2 秒（node 启动），
# 一批 20 个约 22 秒，塞进感知线程不影响主循环。
BATCH_SIZE = 20

# 每个会话一次拉多少条
PAGE_SIZE = 20

# 热区保留多久。最近有动静的会话留在热区，白天只盯这些；
# 278 个全量扫一轮要 6.6 分钟（实测 1.43s/会话），不可能每 3 分钟跑一次。
# 24 小时：昨天动过的今天还会动，三天前动过的基本凉了。
HOT_TTL_SEC = 24 * 3600

# 热区一轮最多扫几个。
#
# 批大小要小于热区规模：给 60 就等于把名额全占了，真正轮转的只剩十几个
# 冷会话。量过一次 scan_at 分布：278 个会话里中位数
# 108 分钟没被扫过，最旧 143 分钟。也就是说群里有人 @我，可能要一个多小时
# 才发现 —— 这条链路的意义就没了。
#
# 让出名额给冷区轮转：热区 36 + 冷区 40 = 76 个/轮。
HOT_BATCH = 36

# 冷会话兜底：每轮在热区之外再扫几个「最久没动静的」。
#
# 不加这一段会有个死角：会话 A 今天早上来了一条消息，但它不在热区里，
# 而热区只在「扫到新消息」时才扩充 —— 不扫 A 就不知道 A 有新消息，
# 于是 A 永远进不了热区，只能等 18:00 全量。用户要的是「监听所有会话」，
# 不能靠一天一次的全量兜这个底。
#
# 实测单会话约 3.8 秒（lark-cli 每次起一个 node），一轮 76 个 ≈ 4.8 分钟：
#     242 个冷会话 ÷ 40 ≈ 6 轮 ≈ 30 分钟全覆盖
# 原来的 15 个/轮配上这个速度，一轮要 7 分钟（含 sleep），冷区要 113 分钟才轮完 ——
# 和实测的 108 分钟中位数对得上。
COLD_BATCH = 40

# 每天几点做一次全量。要早于 18:00 过审，给全量留出跑完的时间。
FULL_SWEEP_HOUR = 17


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class WatchEvent:
    """一条判定为「跟我有关」的消息。"""
    chat_id: str
    chat_name: str
    chat_mode: str          # group | p2p
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    ts: int                 # 毫秒时间戳
    reason: str             # 为什么判定为相关，用于给用户解释
    # 冲我来的 + 我紧跟着已经回过。仍然进快照（过审要算「已处理」），
    # 但不主动推给他 —— 已经回过的再提醒一遍是纯噪音。
    replied: bool = False
    # 是不是直接冲我来的（@我 / 私聊 / 正文点名）。
    #
    # 以前没有这个字段，只存在于 reason 文案的措辞里，下游靠 `"@我" in reason`
    # 反推。代价是文案一改，所有下游静默失效 —— 不报错，只是判据悄悄变宽或变窄。
    # 典型症状：pm_daily_triage 给会议行动项写的 reason 是「会议行动项」，
    # 不含任何 marker，于是 is_directed() 对它恒为 False，和 directed 字段打架，
    # 「被人等」「该问一句」这些下游统计全跟着错。
    # 结论：判定结果必须是字段，文案只管给人看。
    directed: bool = False
    # 这条消息前后各几条，拼成「这件事在说什么」。
    #
    # 单独一句「我问问能不能往后推一下」谁也不知道在推什么 —— 那次过审的
    # 过审里就有这么一条，他圈出来说「总结好点儿」。上下文不在这条消息里，
    # 采集阶段不带上，后面让模型写多好的 prompt 都只能编。
    context: str = ""

    def render(self) -> str:
        t = time.strftime("%m-%d %H:%M", time.localtime(self.ts / 1000))
        if self.chat_mode == "p2p":
            where = f"私聊·{self.chat_name}" if self.chat_name else "私聊"
        else:
            where = self.chat_name
        return f"[{t}] {where} · {self.sender_name}：{self.text[:80]}\n   └ {self.reason}"


@dataclass
class WatchState:
    chats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    refreshed_at: float = 0.0

    @classmethod
    def load(cls) -> "WatchState":
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return cls(
                chats=raw.get("chats") or {},
                refreshed_at=float(raw.get("refreshed_at") or 0),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            _STATE_PATH.write_text(
                json.dumps(
                    {"chats": self.chats, "refreshed_at": self.refreshed_at},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写感知状态失败: %s", e)


@dataclass
class Snapshot:
    """当天已采集到的相关消息。**生产者只有一个**，读者随便读。

    为什么要有这东西
    ----------------
    改造前有两个线程各自遍历 278 个会话：感知线程每轮轮转 20 个，过审线程到点
    自己再全量扫一遍。两边都调 lark-cli（每次拉起一个 node 进程），互相抢资源。
    2026-11-01 20:11 那次的结果是：过审号称「全量扫描」，实际只拿到 4 条消息、
    0 条相关，然后把一份空清单推给他、锁了一整天。

    改造后：扫只发生在这一个地方，过审直接读这里的 events，秒级出清单。

    快照按天存。跨天自动作废，不会把昨天的事混进今天的过审。
    """

    date: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    # chat_id -> 最后一次采到消息的 ts（毫秒）。热区就是从这里来的。
    hot: Dict[str, float] = field(default_factory=dict)
    # 当天完成一次全量的时间。0 表示今天还没全量扫过。
    full_swept_at: float = 0.0
    # 扫描计数：{"群": 扫描会话数, "消息": 总消息数, "无关": 过滤掉的条数}
    stats: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Snapshot":
        try:
            raw = json.loads(_SNAP_PATH.read_text(encoding="utf-8"))
            return cls(
                date=raw.get("date") or "",
                events=raw.get("events") or [],
                hot=raw.get("hot") or {},
                full_swept_at=float(raw.get("full_swept_at") or 0),
                stats=raw.get("stats") or {},
            )
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            _SNAP_PATH.write_text(
                json.dumps(
                    {
                        "date": self.date,
                        "events": self.events,
                        "hot": self.hot,
                        "full_swept_at": self.full_swept_at,
                        "stats": self.stats,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写快照失败: %s", e)

    def reset_if_stale(self, today_key: str) -> bool:
        """跨天了就清空。返回是否发生了重置。"""
        if self.date == today_key:
            return False
        self.date = today_key
        self.events = []
        self.full_swept_at = 0.0
        self.stats = {}
        # hot 不清：三天内活跃的会话仍然值得盯
        return True

    def add(self, events: List["WatchEvent"]) -> int:
        """合并事件，同 message_id 以新判定为准。返回实际变化的条数。

        为什么是覆盖而不是跳过
        --------------------
        一条消息的 replied 会变：第一次扫到时他还没回（False），半小时后他回了，
        下一轮重扫算出 True。如果按 message_id 直接跳过，快照里永远停在 False，
        09:00 的待办就会天天催他去回一条早就回过的消息 ——
        他的原话是「其实我基本都回了，你只需要给我待办就行」。

        覆盖是安全的：事件的关键字段（ts、text、sender）不会变，只有 replied
        这类「后来才知道」的派生字段会变，而且方向是单项的 False → True。
        """
        index = {e.get("message_id"): i for i, e in enumerate(self.events)}
        changed = 0
        for ev in events:
            row = asdict(ev)  # 用 asdict 而不是手抄字段：加字段时不会漏
            i = index.get(ev.message_id)
            if i is None:
                index[ev.message_id] = len(self.events)
                self.events.append(row)
                changed += 1
            elif self.events[i] != row:
                self.events[i] = row
                changed += 1
        return changed

    def hot_chats(self) -> List[str]:
        # 按活跃时间倒序。取前 N 个的时候必须是最活跃的那批，
        # 不能是 dict 的插入顺序 —— 那样热区大了就变成「先来先扫」，
        # 后面进来的新热会话永远排不上。
        cutoff = time.time() - HOT_TTL_SEC
        alive = [(cid, ts) for cid, ts in self.hot.items() if ts / 1000 >= cutoff]
        alive.sort(key=lambda kv: -kv[1])
        return [cid for cid, _ in alive]

    def mark_hot(self, chat_id: str, ts: int) -> None:
        prev = self.hot.get(chat_id) or 0
        if ts > prev:
            self.hot[chat_id] = ts


# ══════════════════════════════════════════════════════════════════════
# 会话清单
# ══════════════════════════════════════════════════════════════════════

def list_my_chats() -> List[Dict[str, Any]]:
    """以用户身份拉取全部会话（群 + 私聊），自动翻页。

    为什么不用 /open-apis/im/v1/chats
    -------------------------------
    那个接口只返回群。实测 100 条全是 chat_mode=group，has_more=True 翻完也没有一条
    p2p，所以「用 chat_mode 字段筛私聊」这条路是走不通的，不是漏了参数。

    私聊只能走 lark-cli 的 `im +chat-list --types p2p` shortcut。它和群列表是两个
    不同的底层数据源，所以这里分两次拉、合并返回。

    p2p_target_type 区分对方是真人（user）还是应用/机器人（bot）。后者是假勤、
    审批这类系统通知，不该按「有人找我」处理。
    """
    out: List[Dict[str, Any]] = []

    for ctype in ("group", "p2p"):
        page_token = ""
        for _ in range(10):  # 最多 10 页，一页 20 条，够用
            args = ["im", "+chat-list", "--types", ctype, "--as", "user"]
            if page_token:
                args += ["--page-token", page_token]

            try:
                res = _run_lark_cli(args)
            except Exception as e:
                logger.warning("拉 %s 会话列表失败: %s", ctype, e)
                break

            data = res.get("data") or {}
            out.extend(data.get("chats") or [])

            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break

    groups = sum(1 for c in out if c.get("chat_mode") == "group")
    logger.info("拉到 %d 个会话（群 %d / 私聊 %d）", len(out), groups, len(out) - groups)
    return out


def refresh_chats(state: WatchState, force: bool = False) -> WatchState:
    """刷新会话清单到状态里。新会话以「当前时间」为起点，避免回灌历史。"""
    if not force and (time.time() - state.refreshed_at) < CHAT_LIST_TTL_SEC:
        return state

    now_ms = int(time.time() * 1000)
    try:
        chats = list_my_chats()
    except Exception as e:
        logger.warning("刷新会话清单异常: %s", e)
        return state

    for c in chats:
        cid = c.get("chat_id")
        if not cid:
            continue
        entry = state.chats.setdefault(cid, {"last_ts": now_ms})
        entry["name"] = c.get("name") or ("" if c.get("chat_mode") == "p2p" else cid[:10])
        entry["mode"] = c.get("chat_mode") or "group"
        entry["external"] = bool(c.get("external"))
        # 私聊对方是真人还是应用（假勤、审批这类系统号）。只有 user 才当「有人找我」。
        entry["target_type"] = c.get("p2p_target_type") or ""
        # 私聊对象的 open_id。用它定位「和 PM 本人的那个会话」比按名字找可靠——
        # 名字可以改，open_id 不会（`pm_daemon._resolve_p2p_chat` 依赖这个字段）。
        entry["target_id"] = c.get("p2p_target_id") or ""

    state.refreshed_at = time.time()
    state.save()
    return state


# ══════════════════════════════════════════════════════════════════════
# 相关性判定
#
# 业务范围（我负责什么、什么不归我）不在这里定义，统一放在 pm_scope。
# 日计划渲染、催办、感知三处共用一份边界，否则迟早会漂移成三套。
# ══════════════════════════════════════════════════════════════════════

# 交付类关键词：命中说明这条消息在谈进度，哪怕没点我名字也可能要我动
_DELIVERY_WORDS = (
    "提测", "上线", "发版", "投产", "验收", "阻塞", "延期", "卡住",
    "排期", "需求变更", "工单", "回滚", "灰度", "联调",
    "改造", "对接", "迁移",
)

# 命中理由里带这些字样的，属于「直接冲我来的」。
# 这类消息如果在我最后一次发言之前，说明我已经回过了，不再重复提醒。
_DIRECTED_MARKERS = ("@我", "私聊", "提到我")

# 「回过」的判定窗口。
#
# 旧逻辑：我的发言必须紧跟在这条之后 30 分钟内才算回过。
# 实测证实这个窗口太窄 —— 他原话「其实我基本都回了」，但过审清单里
# 5 条私聊全被判成未回，因为他是隔了几个小时才回的。
#
# 新逻辑：往后看一段会话，8 小时内我说过话就算回过。
# 为什么不能简单放宽成「此后发过言」：下午在群里随口一句，上午没回的
# 三件事会一起消失。所以既要时间上限，也要往后看的条数上限。
REPLY_WINDOW_MS = 8 * 60 * 60 * 1000
REPLY_LOOKAHEAD = 20

# 私聊里绝大部分是「好的」「ok」「哈哈哈」。全收等于把聊天记录搬进推送，
# 一条决策信息都提取不出来。这里只留「在问我事」或「在谈交付」的。
_P2P_NOISE = {
    "好的", "好", "好滴", "好嘞", "好的好的", "ok", "okay", "嗯", "嗯嗯",
    "收到", "可以", "行", "谢谢", "谢啦", "明白", "了解", "没问题",
    "哈哈哈", "哈哈", "对", "是的", "在", "不在", "稍等", "马上", "好嘞谢谢",
}

# 「在问我事」的信号。私聊的价值主要在这里：有人在等我回话。
# 不要收「呢」「麻烦」这类词 —— 实测「嗯呢，感谢」「感觉好麻烦」全被它们误命中。
_ASK_PATTERNS = (
    "?", "？", "吗", "怎么", "如何", "什么时候", "能不能",
    "是否", "有没有", "多久", "帮忙", "请帮",
)

_URL_RE = re.compile(r"https?://\S+")

# 版本号形态：10.29 / 11.05 / 11.12
_VERSION_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")


def _extract_text(msg: Dict[str, Any]) -> str:
    """从飞书消息体里抽出可读文本。

    飞书 body.content 的结构随 msg_type 变化，只认 {"text": ...} 会把大量消息丢掉：

        text   {"text": "..."}
        post   {"title": "...", "content": [[{"tag":"text","text":"..."}]]}
        card   {"title": "...", "elements": [...]}
        image / file / audio   只有 key，没有正文

    实测「星路产品需求版本规划」群最近 5 条全是空文本 —— 因为都是卡片/富文本，
    按老逻辑会被当成「没内容」直接跳过。而需求讨论恰恰大量发生在卡片里。
    """
    raw = (msg.get("body") or {}).get("content") or ""
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except Exception:
        return raw[:300]

    if isinstance(body, str):
        return body[:300]
    if not isinstance(body, dict):
        return ""

    # 纯文本
    if isinstance(body.get("text"), str) and body["text"].strip():
        return body["text"]

    # 富文本 / 卡片：标题 + 逐行逐段拼
    parts: List[str] = []
    if body.get("title"):
        parts.append(str(body["title"]))

    content = body.get("content")
    if isinstance(content, list):
        for line in content:
            segs = line if isinstance(line, list) else [line]
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                if seg.get("text"):
                    parts.append(str(seg["text"]))
                elif seg.get("href"):
                    parts.append(str(seg["href"]))

    return "".join(parts).strip()


def _apply_mentions(text: str, msg: Dict[str, Any]) -> str:
    """把正文里的 @_user_1 占位符还原成真实姓名。

    不还原的话，判定和展示都会出现「@_user_1 预约单没有冻结资金的」这种鬼东西，
    既认不出在跟谁说话，也没法判断是不是在说我。
    """
    for men in msg.get("mentions") or []:
        key = men.get("key")
        name = men.get("name")
        if key and name:
            text = text.replace(key, "@" + name)
    return text


_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,60}>")


def _strip_html(text: str) -> str:
    """去掉 <p> / <br> / <at ...> 这类标签。

    飞书部分消息（富文本转过来的、以及系统推送）正文里直接带 HTML，
    原样推给他会是「<p>1. ESOP 个人限制户开户RO审批通过后…</p>」，
    看着像乱码，也占掉清单行宽。实测 2026-10-29 的 ESOP 那条就是这样。
    """
    if not text or "<" not in text:
        return text
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    return _TAG_RE.sub("", text).strip()


def _msg_text(msg: Dict[str, Any]) -> str:
    # 先去标签再还原 @：标签里可能裹着 @_user_1，顺序反了会留下半截尖括号。
    return _apply_mentions(_strip_html(_extract_text(msg)), msg)


def _my_names() -> tuple:
    # 名字是硬事实，不做模糊匹配：早先这里有个「浩祥」是抄错的字，当别名用
    # 只会训练系统接受错误称呼，输出里也跟着写错。名字从配置读，换人不改代码。
    names = tuple(pm_config().identity.names)
    if not names:
        logger.warning("[pm.watch] 未配置 [projects.pm.identity].names，"
                       "@ 我 的判定会全部落空")
    return names


def _active_versions() -> set:
    """当前在跟的版本号，用于判定消息是否在谈我负责的版本。

    读不到表格就返回空集合，只靠人名词判，不会因此爆炸。
    """
    try:
        vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N60")
        return {s.version for s in parse_stories(vals)}
    except Exception:
        return set()


def _p2p_signal(text: str) -> bool:
    """私聊里这句话值不值得浮出来。

    判定门槛比群消息低（私聊本来就是冲我说的），但不能零门槛 —— 实测 3 天
    134 个私聊会话里有 103 条命中，全是「好的」「ok」「可以的」，推出去等于刷屏。

    只保留两类：有人在问我事，或者消息在谈交付进展。
    """
    body = _URL_RE.sub(" ", text)  # 链接本身不含信息，别拿它判
    core = body.strip().strip("。.!！~～ ,，")
    if len(core) <= 3:
        return False
    if core.lower() in _P2P_NOISE:
        return False
    if any(p in body for p in _ASK_PATTERNS):
        return True
    if any(w in body for w in _DELIVERY_WORDS):
        return True
    if pm_scope.domain_of(body):
        return True
    return False


def is_relevant(
    msg: Dict[str, Any],
    chat: Dict[str, Any],
    versions: set,
) -> Optional[str]:
    """判定一条消息是否跟我有关。返回理由，无关返回 None。

    判定顺序从强到弱，命中第一条即返回。这里的顺序是产品决策，不是随手排的：

    1. 直接冲我来的（@我 / 真人私聊 / 正文点名）—— 无论什么群都收
    2. 明确不属于我的业务线（星企通等）—— 无论多热闹都跳过
    3. 静音群 —— 只放行第 1 类
    4. 其余：必须落在我的业务域内，且带交付动作

    chat 是会话元信息：{"name", "mode", "target_type"}。target_type 只在私聊里有值，
    区分对方是真人还是应用 —— 假勤、审批这些系统号每天推一堆，不能算「有人找我」。
    """
    chat_mode = chat.get("mode") or "group"
    chat_name = chat.get("name") or ""
    target_type = chat.get("target_type") or ""

    sender = (msg.get("sender") or {})
    sender_id = sender.get("id") or ""

    # 自己发的也要采 —— 它是「他说过什么」的唯一证据。
    #
    # 判「这件事我回过没有」不能只靠消息自带的 replied：那是采集那一刻的快照，
    # 跨天就冻结了。他今天早上回了昨天的问题，昨天的消息永远停在 False，
    # 待办里就会一直挂着「卢燕问了，等你答」—— 他的原话是「其实我基本都回了」。
    #
    # 采集归采集，推送另有一道过滤（_deliver_watch_events），不会自己推给自己。
    # 放在 at_me 之前：自己 @ 自己不该算「有人在群里 @我」。
    if sender_id == PM_OPEN_ID:
        return "我自己发的"

    # 应用/机器人发的（cli_ 前缀）不是「有人在找我」，是播报。
    # 假勤打卡、需求到期提醒、机器人自己的推送回显全在这一类里，收进来只会稀释正事。
    if sender_id.startswith("cli_"):
        return None

    mentions = msg.get("mentions") or []
    text = _msg_text(msg)
    at_me = any((men.get("id") == PM_OPEN_ID) for men in mentions)

    # ── 第 1 类：直接找我。这类消息永远不能漏 ──
    if at_me:
        return "有人在群里 @我"
    if chat_mode == "p2p" and target_type == "user" and text:
        # 真人私聊，但仍要过信息量门槛，否则一天几十条「好的」直接埋掉正事
        return "私聊里问我事" if _p2p_signal(text) else None
    if text and any(n in text for n in _my_names()):
        return "消息里提到我"

    if not text:
        return None

    # ── 第 2 类：不归我管。哪怕在项目组群里讨论得热火朝天，也与我无关 ──
    out = pm_scope.out_of_scope_hit(text)
    if out:
        logger.debug("跳过无关业务线 [%s] %s", out, chat_name)
        return None

    # ── 第 3 类：静音群。全员群、闲聊群不进候选池 ──
    if pm_scope.is_muted_chat(chat_name):
        return None

    # ── 第 4 类：业务域内 + 有交付动作才值得看。只有讨论没有动作的不推 ──
    domain = pm_scope.domain_of(text)
    in_focus_chat = pm_scope.is_focus_chat(chat_name)

    # 版本号命中：只有在我负责的域内才算数，否则 7709 这类也会被带进来
    if domain or in_focus_chat:
        for m in _VERSION_RE.finditer(text):
            key = f"{int(m.group(1))}.{m.group(2)}"
            if key in versions or f"{int(m.group(1))}.{int(m.group(2)):02d}" in versions:
                return f"谈到我在跟的版本 {key}"

    hits = [w for w in _DELIVERY_WORDS if w in text]
    if hits and (domain or in_focus_chat):
        return f"涉及交付动作（{'/'.join(hits[:2])}）"

    return None


# ══════════════════════════════════════════════════════════════════════
# 「是不是冲我来的」—— 对外暴露的唯一判定
# ══════════════════════════════════════════════════════════════════════

def is_directed(reason: str) -> bool:
    """从判定理由文案里读「是不是冲我来的」。

    口径来自 _DIRECTED_MARKERS，只在 **入口** 用一次：采集时把结果落到
    WatchEvent.directed。下游一律读字段，不要再调这个函数 —— 否则文案
    一改，所有下游静默失效，而且没人会发现。

    读取方要兼容老快照（没有 directed 字段），统一走 directed_of()。
    """
    return any(m in (reason or "") for m in _DIRECTED_MARKERS)


def directed_of(row: Dict[str, Any]) -> bool:
    """从快照行里取「是不是冲我来的」：优先字段，缺字段才回退文案。

    这是唯一的兼容入口。老快照是在加 directed 字段之前写的，
    下次重扫会把字段补上，这条回退路径自然消失。
    """
    value = row.get("directed")
    if value is None:
        return is_directed(str(row.get("reason") or ""))
    return bool(value)


def _fetch_messages(chat_id: str, page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    res = _run_lark_cli([
        "api", "GET", "/open-apis/im/v1/messages",
        "--params", json.dumps({
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": page_size,
            "sort_type": "ByCreateTimeDesc",
        }),
        "--as", "user",
    ])
    if not res.get("ok"):
        logger.debug("拉消息失败 chat=%s: %s", chat_id, str(res)[:200])
        return []
    return (res.get("data") or {}).get("items") or []


def _sender_name(msg: Dict[str, Any]) -> str:
    sender = msg.get("sender") or {}
    return sender.get("name") or sender.get("id", "")[:12]


def _similar(a: str, b: str) -> bool:
    """判断两段文本是不是在聊同一件事。

    同一群里一个话题会连着刷七八条，逐条推送就是刷屏。用二元组 Jaccard 粗判，
    够用且不依赖分词。
    """
    if not a or not b:
        return False
    if a[:15] and a[:15] == b[:15]:
        return True
    sa = {a[i:i + 2] for i in range(len(a) - 1)}
    sb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) > 0.55


def _replied_after(ordered: List[Dict[str, Any]], idx: int,
                   window_ms: int = REPLY_WINDOW_MS,
                   lookahead: int = REPLY_LOOKAHEAD) -> bool:
    """这条消息之后我有没有回过。

    判据：往后看最多 lookahead 条，只要在 window_ms 内出现我的发言，就算回过。

    为什么不用「此后我发过言」一句话了事：那会把下午随口一句当成散了场的
    结论，上午没回的三件事一起被抹掉。也不能只看「紧接着下一条」——
    对方常常连发三四条把话说完整，我回的是最后一条。

    为什么窗口从 30 分钟放到 8 小时：他实际是隔几小时回的，30 分钟窗口
    把他回过的全判成没回，等于天天提醒他「卢燕在等你」。
    """
    try:
        base = int(ordered[idx].get("create_time") or 0)
    except (TypeError, ValueError, IndexError):
        return False
    if not base:
        return False

    for m in ordered[idx + 1: idx + 1 + lookahead]:
        try:
            t = int(m.get("create_time") or 0)
        except (TypeError, ValueError):
            continue
        if not t:
            continue
        if t - base > window_ms:
            break
        if (m.get("sender") or {}).get("id") == PM_OPEN_ID:
            return True
    return False


def _context_around(
    ordered: List[Dict[str, Any]],
    i: int,
    back: int = 6,
    ahead: int = 4,
    gap_ms: int = 30 * 60 * 1000,
) -> str:
    """取第 i 条所在的整段对话，拼成「这件事在说什么」。

    ordered 必须是时间正序。

    为什么按「时间间隔」切而不是按条数切：2026-11-02 孙雨滋那条
    「我问问能不能往后推一下」，取前后各 2 条只拿到三句「okok」，
    真正的原因（她第二天有和新加坡 JY 的 call，评审要压到 4 点半前结束）
    在前四条里。按条数切永远切不准，按 30 分钟间隔切能自然落在话题边界上。

    自己发的标成「我」—— 模型必须知道哪句是他自己说的，
    否则会把他已经答应过的事又当成新需求写一遍。
    """
    def _ts(m: Dict[str, Any]) -> int:
        try:
            return int(m.get("create_time") or 0)
        except (TypeError, ValueError):
            return 0

    lo = i
    while lo > 0 and i - lo < back and _ts(ordered[lo]) - _ts(ordered[lo - 1]) <= gap_ms:
        lo -= 1
    hi = i
    while hi < len(ordered) - 1 and hi - i < ahead and _ts(ordered[hi + 1]) - _ts(ordered[hi]) <= gap_ms:
        hi += 1

    parts: List[str] = []
    for j in range(lo, hi + 1):
        if j == i:
            continue
        mm = ordered[j]
        txt = " ".join((_msg_text(mm) or "").split())
        if not txt:
            continue
        me = (mm.get("sender") or {}).get("id") == PM_OPEN_ID
        who = "我" if me else (_sender_name(mm) or "对方")
        # 通讯录还没解析出来时会拿到裸 open_id，对模型来说不如「对方」有用
        if who.startswith("ou_"):
            who = "对方"
        parts.append(f"{who}：{txt[:80]}")
    return " / ".join(parts)


def _coverage_note(now_ms: int, oldest_scan: int, never_scanned: int) -> str:
    """一句话说清这轮的覆盖延迟。

    没有这个数，「感知覆盖得过来吗」就只能靠体感和猜：一轮扫 63 个会话、
    单会话 3.8s，热区之外的会话到底等多久才轮到一次，只有 scan_at 知道。
    """
    parts = []
    if oldest_scan:
        parts.append(f"最久没扫的等了 {(now_ms - oldest_scan) / 60000:.0f} 分钟")
    if never_scanned:
        parts.append(f"{never_scanned} 个首次")
    return "，".join(parts) or "全部刚扫过"


def poll(
    state: WatchState,
    limit: int = BATCH_SIZE,
    snapshot: Optional[Snapshot] = None,
    full: bool = False,
    since_ms: int = 0,
) -> List[WatchEvent]:
    """扫会话，返回新增的相关消息。

    两种模式，区别只在「扫哪些会话」：

        full=False  只扫热区（最近 3 天有动静的会话），通常 10-30 个，几十秒完成
        full=True   全部 278 个，5-7 分钟，每天一次，给 18:00 过审备料

    为什么要分模式：全量一轮 6.6 分钟（实测 1.43s/会话），如果每轮都全量，
    同一个群要等 50 分钟才会被扫第二次 —— 那不叫感知，叫抽查。
    白天只盯热区能把延迟压到一轮（3 分钟）以内。

    since_ms 是**回溯窗口**：早于它的消息不采集，但游标照样推进。
    第一次全量扫描时游标可能停在好几天前，不设窗口会把一整周的积压
    一次性灌进快照（实测 46 条，最早到 10-26），过审再用「今天」去筛就剩 0 条。
    """
    state = refresh_chats(state)

    # 只扫群和私聊，跳过与机器人的会话（那是它自己在说话）
    pool = {
        cid: meta for cid, meta in state.chats.items()
        if meta.get("mode") in ("group", "p2p")
    }

    if full:
        candidates = sorted(pool.items(), key=lambda kv: kv[1].get("last_ts", 0))
    elif snapshot is not None:
        hot = [cid for cid in snapshot.hot_chats() if cid in pool]
        if hot:
            # 热区就是「值得盯的」，全扫。硬用 BATCH_SIZE 截断会让大热区退化成轮转，
            # 又回到 50 分钟才轮到一次的坑里。
            candidates = [(cid, pool[cid]) for cid in hot][:HOT_BATCH]

            # 兜底：热区之外补一批最久没扫过的会话。
            # 没有这一段，冷会话里的新消息只能等 18:00 全量才被发现。
            #
            # 排序用 scan_at（上次被扫到的时刻），不能用 last_ts：
            # 从没发过消息的会话 last_ts 恒为 0，会一直排在最前，
            # 15 个名额被同一批死群占满，后面的永远轮不到。
            taken = {cid for cid, _ in candidates}
            cold = sorted(
                ((cid, m) for cid, m in pool.items() if cid not in taken),
                key=lambda kv: kv[1].get("scan_at", 0),
            )[:COLD_BATCH]
            candidates += cold
        else:
            # 冷启动（刚部署/刚清过快照）：先按轮转扫一批，把热区养起来
            candidates = sorted(pool.items(), key=lambda kv: kv[1].get("scan_at", 0))[:limit]
    else:
        candidates = sorted(pool.items(), key=lambda kv: kv[1].get("scan_at", 0))[:limit]

    versions = _active_versions()
    events: List[WatchEvent] = []
    scanned = 0
    irrelevant = 0
    now_ms = int(time.time() * 1000)
    oldest_scan = 0
    never_scanned = 0

    for chat_id, meta in candidates:
        prev_scan = int(meta.get("scan_at") or 0)
        if prev_scan:
            oldest_scan = prev_scan if not oldest_scan else min(oldest_scan, prev_scan)
        else:
            never_scanned += 1
        # 记下「什么时候扫过这个会话」。冷区轮转靠它排序，
        # 用 last_ts 会饿死那些从没发过消息的群。
        meta["scan_at"] = now_ms
        try:
            msgs = _fetch_messages(chat_id)
        except Exception as e:
            logger.debug("扫描 %s 异常: %s", chat_id, e)
            continue
        scanned += 1

        last_ts = int(meta.get("last_ts", 0))
        newest = last_ts
        emitted: List[str] = []  # 本群已产出事件的正文，用于话题去重

        ordered = list(reversed(msgs))  # 时间正序，话题去重时保留最早的起点
        for i, m in enumerate(ordered):
            try:
                ts = int(m.get("create_time", 0))
            except (TypeError, ValueError):
                continue
            if ts <= last_ts:
                continue  # 已处理过
            # 游标必须先推进：早于回溯窗口的消息不采集，但不能让游标停在原地，
            # 否则下一次扫描还是会把它们当「新消息」重新捞一遍。
            newest = max(newest, ts)
            if since_ms and ts < since_ms:
                continue

            reason = is_relevant(m, meta, versions)
            if not reason:
                irrelevant += 1
                continue

            # 判定在入口算一次，落成字段。下游只读字段，不许再拿 reason
            # 文案做字符串匹配 —— 那等于把判定结果编码进给人看的措辞里。
            directed = is_directed(reason)

            # 冲我来的 + 我后来回过 —— 收进快照（过审要算「已处理」），
            # 但不主动推送。是否提醒由调用方决定，采集层不下这个判断。
            replied = directed and _replied_after(ordered, i)

            text = _msg_text(m)

            # 同一话题连着刷的多条消息只留一条。
            # 但我说的话一条都不能去重：「好的」「我看下」本身没信息量，
            # 却正是「这条我回过了」的证据，被同话题规则吞掉等于没回。
            is_mine = (m.get("sender") or {}).get("id", "") == PM_OPEN_ID
            if not is_mine and any(_similar(text, prev) for prev in emitted):
                logger.debug("同话题已收录，跳过: %s", text[:40])
                continue
            emitted.append(text)

            events.append(WatchEvent(
                chat_id=chat_id,
                chat_name=meta.get("name", chat_id[:10]),
                chat_mode=meta.get("mode", "group"),
                message_id=m.get("message_id", ""),
                sender_id=(m.get("sender") or {}).get("id", ""),
                sender_name=_sender_name(m),
                text=text,
                ts=ts,
                reason=reason,
                replied=replied,
                directed=directed,
                context=_context_around(ordered, i),
            ))

        meta["last_ts"] = newest
        # 有新消息就进热区，哪怕这条不相关 —— 说明这个会话今天在动，值得继续盯
        if snapshot is not None and newest > last_ts:
            snapshot.mark_hot(chat_id, newest)

    state.save()

    if snapshot is not None:
        snapshot.add(events)
        # 累计，不清零。有游标在，同一条消息不会被重复计数，
        # 所以这个数就是「今天到现在为止一共过滤掉多少」——
        # 全量那一轮只补上白天没覆盖到的部分，不能把白天的抹掉。
        snapshot.stats["scanned"] = snapshot.stats.get("scanned", 0) + scanned
        snapshot.stats["irrelevant"] = snapshot.stats.get("irrelevant", 0) + irrelevant
        if full:
            snapshot.full_swept_at = time.time()
        snapshot.save()

    # 每轮留一行账。没有这行日志，就分不清「扫完了没新消息」和「卡住了」。
    # 后半句是覆盖体检：热区之外还有多少会话在排队、排了多久。
    logger.info(
        "扫完 %d 个会话（%s），%d 条相关 / %d 条无关；%s",
        scanned, "全量" if full else "热区", len(events), irrelevant,
        _coverage_note(now_ms, oldest_scan, never_scanned),
    )

    # 消息体里的 sender.name 基本是空的，批量换成人名再返回，
    # 否则推送里全是 ou_xxx，他没法判断该找谁。批量是为了摊掉 lark-cli 的启动开销。
    if events:
        try:
            pm_people.apply_to(events)
        except Exception as e:
            logger.warning("回填人名失败（不影响事件）: %s", e)

    return events


# ══════════════════════════════════════════════════════════════════════
# 全量扫描（唯一入口）
# ══════════════════════════════════════════════════════════════════════

# 全量是重操作（278 会话 × 1.4s ≈ 6.6 分钟）。同一时刻只允许一个在跑。
# 那次事故就是两个线程各扫各的、互相拖死，最后只采到 4 条消息。
_SWEEP_LOCK = threading.Lock()


def full_sweep(force: bool = False) -> Snapshot:
    """全量扫一遍，结果写进快照。18:00 过审前调用。

    force=False 时，当天已经全量过就直接返回现有快照 —— 幂等，
    重启或重试都不会把 6.6 分钟的活重跑一遍。
    """
    today_key = time.strftime("%Y-%m-%d")
    snap = Snapshot.load()
    snap.reset_if_stale(today_key)

    if snap.full_swept_at and not force:
        logger.info(
            "今天 %s 已全量扫过，直接读快照（%d 条相关）",
            time.strftime("%H:%M", time.localtime(snap.full_swept_at)),
            len(snap.events),
        )
        return snap

    if not _SWEEP_LOCK.acquire(timeout=2):
        logger.info("已有全量在跑，本次直接读现有快照（%d 条）", len(snap.events))
        return snap

    try:
        state = WatchState.load()
        # 只回捞今天。游标可能停在几天前（daemon 停摆过），
        # 不设窗口会把一整周的积压灌进快照，过审再按「今天」筛就成了 0 条。
        day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(day_start.timestamp() * 1000)
        logger.info("开始全量扫描 %d 个会话…（回溯到 %s）",
                    len(state.chats), day_start.strftime("%m-%d %H:%M"))
        t0 = time.time()
        events = poll(state, limit=10_000, snapshot=snap, full=True, since_ms=since_ms)
        logger.info(
            "全量扫描完成：%d 个会话 / %d 条相关 / 耗时 %.1fs",
            snap.stats.get("scanned", 0), len(events), time.time() - t0,
        )
        return Snapshot.load()
    finally:
        _SWEEP_LOCK.release()
