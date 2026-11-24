"""每日过审 —— 18:00 把今天的信息摊成一张清单，让他逐条过。

他的原话（2026-10-29）：
    「我需要你总结成待办或者结论找我确认，你得结合上下文总结一下，
      你不能只给我甩原文，而且你还没展示全」

所以这个模块的第一原则是：**给结论，不给原文**。

分档为什么是三档而不是两档
--------------------------
他说「这六条都和我无关」。那六条确实都在他负责的域里（票据、ESOP），但都是
别人之间的通报和 @ 别人的事 —— 判成「待办」就是错的。

真正该分的是「需要他动手」和「他只需知道」：

    action  要我动手 —— 群里 @我、私聊问我、正文点名我
    done    要我动手，但我已经回过了
    fyi     我该知道 —— 我负责的域里的进展通报，但没点我
    irrelevant  判错成无关（他纠正用）

只有 action 会进待办池、第二天早上提醒。fyi 只做简报，不占他的动作。

状态机
-----
    collect()      扫当天全部会话 → 分档 → LLM 压成一句话结论
        │
        ▼
    stage()        私聊推送，落盘进 awaiting
        │
        ▼
    handle_pending() 他回「12 13 明天做」「都无关」「好」
        │
        ▼
    commit()       收敛；action 项写入待办池
        │
        ▼
    次日 09:00     render_open_todos() 追加进日计划
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domains.pm import pm_meeting_sensor, pm_people, pm_watch
from app.domains.pm.pm_intent_router import call_llm_text

logger = logging.getLogger("pm_hub.triage")

_PROJECT_DIR = Path(__file__).resolve().parents[3]
_STATE_PATH = _PROJECT_DIR / ".triage_state.json"
_TODOS_PATH = _PROJECT_DIR / ".triage_todos.json"

ACTION = "action"          # 要我动手
DONE = "done"              # 要我动手，已经回过了
FYI = "fyi"                # 我该知道，但不用我动手
IRRELEVANT = "irrelevant"  # 判错成无关（他纠正用）
# 同一件事被别的段落合并了。不渲染、不编号、不进待办 —— 它只是模型用来
# 告诉我「这两段是一件事」的手段，不是给他看的条目。
MERGED = "merged"

# 结论字数上限。超过这个长度说明模型在复述原文，宁可截断也不要糊他一屏。
SUMMARY_LIMIT = 46

# 一次喂给模型多少件事。太多输出超时，太少调用次数爆炸。
# 5 是实测值：8 件事一批时会稳定撞到读超时，
# 5 件事单批能在 20s 内返回，11 件事就是 3 批。
_SUMMARY_BATCH = 5

# 待办生成是长输出（每件最多 5 个字段），不能用单轮问答的 30s。
# 但也不能给到 90s —— 网关偶发「连上了不返回」，单批 90s × 3 批就把 18:00
# 推送拖到 18:15。45s 是本机实测正常返回（20~30s）的 1.5 倍余量。
SUMMARY_TIMEOUT_SEC = 45

# 单批失败重试次数。网关会偶发读超时，一次失败就退回原文的话，
# 他看到的会是一屏没有结论的原始消息 —— 那正是这次要修的问题。
_SUMMARY_RETRY = 2

# 并发批数。13 件事 = 3 批，串行 3×30s，并发后只有最慢那一批的时间。
_SUMMARY_WORKERS = 3

# 整个「总结」步骤的总预算。到点就收工，剩下的走兜底。
#
# 为什么必须有这个：18:00 的推送是硬约定。只要有一批卡住（实测过网关
# 连上不回，requests 的 read timeout 被流式返回的碎片数据不断重置，
# 90s 也等不回来），整条推送就一起卡死。宁可少几条总结，不能不准点。
SUMMARIZE_BUDGET_SEC = 120


@dataclass
class TriageItem:
    idx: int
    message_id: str
    chat_name: str
    chat_mode: str
    sender: str
    text: str
    ts: int
    verdict: str
    reason: str
    # 以下字段补在末尾并给默认值：已落盘的旧状态文件没有它们，否则反序列化会炸。
    sender_id: str = ""
    summary: str = ""      # LLM 写的一句话结论；生成失败时为空，渲染退回原文
    need: str = ""         # LLM 判的「要不要他动」：reply/act/none；空=没判出来
    replied: bool = False  # 这条之后 30 分钟内他在同群发过言 —— 只是线索，不是结论
    directed: bool = False # 是否冲他来的。collect 判一次存下来，下游不要再猜 reason 的字符串

    # ── 事项维度 ──────────────────────────────────────────────────
    # 一条消息不等于一件事。李皇玉 16:47 到 16:49 发了 4 条碎片，其实是同一件
    # 「周九的账密到底给没给」。按条过审，他要读 4 遍才知道在说什么 —— 这是
    # 他觉得「不智能」的真正原因，不是摘要写得不够好。
    thread_id: int = 0     # 同一事项的条目归到同一个 thread_id
    group_size: int = 1    # 这一条代表了几条原始消息；>1 说明是合成出来的
    # 这件事前后的几句话，采集层给的。没有它，模型只能对着「我问问能不能
    # 往后推一下」编 —— 他曾圈出来的第 8 条就是这种。
    context: str = ""

    # ── 待办维度 ──────────────────────────────────────────────────
    # 结论（summary）是「发生了什么」，待办（todo）是「我该干什么」。
    # 他明确要的是后者：不要他自己从结论里再翻译一遍动作。
    todo: str = ""         # 动词开头的动作，落进待办池的就是这一句
    who: str = ""          # 找谁。空 = 不用找人，自己办
    when: str = ""         # today / tomorrow / thisweek
    why: str = ""          # 为什么现在做，一句依据（他靠这个判断要不要删）


@dataclass
class TriageSheet:
    date: str
    items: List[TriageItem] = field(default_factory=list)
    noise: Dict[str, int] = field(default_factory=dict)
    stage: str = "collected"   # collected | awaiting | committed
    pushed_at: float = 0.0

    @classmethod
    def load(cls) -> Optional["TriageSheet"]:
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            return cls(
                date=raw.get("date", ""),
                items=[TriageItem(**it) for it in raw.get("items") or []],
                noise=raw.get("noise") or {},
                stage=raw.get("stage", "collected"),
                pushed_at=float(raw.get("pushed_at") or 0),
            )
        except Exception:
            return None

    def save(self):
        try:
            _STATE_PATH.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写过审状态失败: %s", e)

    def section(self, verdict: str) -> List[TriageItem]:
        return [it for it in self.items if it.verdict == verdict]


# ══════════════════════════════════════════════════════════════════════
# 采集与分档
# ══════════════════════════════════════════════════════════════════════

def _day_start_ms(today: date) -> int:
    d = datetime(today.year, today.month, today.day)
    return int(d.timestamp() * 1000)


def _names_me(text: str) -> bool:
    """这条行动项有没有点名他。

    人名表只在 pm_watch 定义一份 —— @ 判定、reply 判定、这里都读同一处。
    各写一套的话，迟早出现「@ 了但不算冲他来」这种自相矛盾。
    """
    return any(n in (text or "") for n in pm_watch._my_names())


def _meeting_items() -> List[TriageItem]:
    """把今天妙记里的未完成行动项转成清单条目。

    采集层不做业务过筛 —— 会里出现的东西默认都在他视线内。
    「要不要他管」是分档的事，不是采集的事，否则又是第二套边界。

    但「要不要他**动手**」是另一回事，必须在这层分清：
    纪要里一条行动项通常长这样 ——「查看多批次需求内容：… @孙八」，
    @ 的是孙八，他只是在场。2026-11-05 把这批统一标成 action，
    09:00 清单上就写出「会议纪要有 3 条行动项派给你」，他当场反问
    「什么行动？」—— 那 3 条 @ 的是钱七、王五、孙八，一条都不是他的。

    所以 directed 由原文有没有点名他决定：点名的才是他的活（action），
    没点名的仍然收进清单（他要知道会上定了什么），但只算 fyi。
    """
    try:
        digests = pm_meeting_sensor.collect_today(days=1)
    except Exception as e:
        logger.warning("会议感知失败（本次不并入会议）: %s", e)
        return []

    out: List[TriageItem] = []
    for d in digests:
        for i, t in enumerate(d.open_todos):
            mine = _names_me(t.content)
            out.append(TriageItem(
                idx=0,
                message_id=f"minute:{d.token}:{i}",
                chat_name=f"会议 · {d.title}",
                chat_mode="meeting",
                sender="会议纪要",
                text=t.content,
                ts=0,
                verdict=ACTION if mine else FYI,
                # 两种 reason 分开，下游凭它就知道该谁动手，不用再看文本。
                reason="会议行动项" if mine else "会议行动（别人做）",
                # 行动项原文本身就是结论，不用模型再改写一遍
                summary=t.content,
                need="act" if mine else "none",
                directed=mine,
                # 会议行动项本身就是一句待办，直接当 todo 用；再让模型改写就成了废化。
                # 不是他的活就不该进待办池，否则第二天 09:00 会拿别人的活催他。
                todo=t.content if mine else "",
                when="today" if mine else "",
            ))
    return out


# 同一事项的时间窗口。同事连着说几句间隔基本在 20 分钟内；超过这个间隔再发言，
# 多半换了话题（上午问「最急的是哪个需求」，下午问「提测了没」）。
_THREAD_GAP_SEC = 25 * 60


def _cluster(items: List[TriageItem]) -> List[TriageItem]:
    """把碎片消息聚成事项，每个事项留一条代表条目。

    为什么必须聚类：2026-11-02 的快照里 22 条相关消息，其中李皇玉 16:47–16:49
    的 4 条是同一件「周九的账密到底给没给」，张三 2 条是同一句寒暄。
    按条过审 = 让他把同一件事读 4 遍，然后自己找共同点 —— 那不是总结，是转述。

    纯规则，不花模型调用：
        同会话 + 同发送人 +（间隔 ≤ 25 分钟 或 文本相似）→ 同一件事

    代表条目的 text 是所有碎片的拼接。模型一次看全上下文，才写得出
    「把周九的账密发给卢燕」这种真正的待办；只看最后一句「是还没给？还是给过了？」，
    没人知道要干什么。
    """
    groups: List[List[TriageItem]] = []

    for it in items:
        # 会议行动项本身就是结论，一条就是一个事项，不参与聚类
        if it.chat_mode == "meeting":
            groups.append([it])
            continue

        placed = False
        for g in groups:
            anchor = g[-1]
            if anchor.chat_mode != it.chat_mode:
                continue
            if anchor.chat_name != it.chat_name or anchor.sender != it.sender:
                continue
            gap = abs(it.ts - anchor.ts) / 1000.0
            if gap <= _THREAD_GAP_SEC or pm_watch._similar(it.text, anchor.text):
                g.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])

    out: List[TriageItem] = []
    for tid, g in enumerate(groups):
        g.sort(key=lambda x: x.ts)
        # 代表 = 原文最长那条（信息最全）；元信息用组内最新时间，排序才对得上
        head = max(g, key=lambda x: len(x.text or ""))
        if len(g) > 1:
            head.text = " / ".join(
                " ".join((x.text or "").split()) for x in g if x.text
            )
            head.ts = g[-1].ts
            # 组里任何一条显示他已回话，这件事就算他碰过了
            head.replied = any(x.replied for x in g)
            head.directed = any(x.directed for x in g)
            # 上下文也要合并。相邻两条的前后文高度重叠，按出现顺序去重，
            # 否则同一句话会在 prompt 里重复三四遍，白烧 token 还干扰判断。
            merged: List[str] = []
            for x in g:
                for piece in (x.context or "").split(" / "):
                    piece = piece.strip()
                    if piece and piece not in merged:
                        merged.append(piece)
            head.context = " / ".join(merged)
        head.thread_id = tid
        head.group_size = len(g)
        out.append(head)

    logger.info("聚类: %d 条消息 → %d 件事", len(items), len(out))
    return out


# 机器自动发的不算有人找他。豆包工作每天会推几条问候，出现在待办里
# 只会让他觉得连机器人发的都当回事。
_NOISE_TEXT = ("由豆包工作发送", "（机器人）", "自动发送")


def collect(today: Optional[date] = None) -> TriageSheet:
    """从快照生成过审清单。**自己不扫会话。**

    改造前这里遍历 278 个会话，和感知线程抢 lark-cli。2026-11-01 20:11 那次
    号称全量扫描，实际只拿到 4 条消息、0 条相关，然后把一份空清单推给他，
    还锁了一整天 —— 他回「5 7已完成」被这份空清单劫走。

    现在扫描只有一个生产者（pm_watch.full_sweep），这里只做分档：
    采集 → 分档 拆开之后，采集慢不要紧，分档是毫秒级的。
    """
    today = today or date.today()
    today_key = today.strftime("%Y-%m-%d")
    since = _day_start_ms(today)

    # 会议和群消息是两个来源，失败互不牵连：妙记拉不到时照样出群消息清单。
    meetings = _meeting_items()

    snap = pm_watch.Snapshot.load()
    if snap.date != today_key:
        logger.warning("快照不是今天的（快照 %s / 今天 %s）", snap.date or "空", today_key)
        return TriageSheet(date=today_key, items=meetings, noise={"今天还没扫": 1})

    items: List[TriageItem] = list(meetings)

    for ev in snap.events:
        try:
            ts = int(ev.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < since:
            continue

        text = str(ev.get("text") or "").strip()
        if not text:
            continue

        # 我自己发的话也进了快照（用来判「这条我回过没有」），
        # 但那不是「有人找我」。放进清单就等于机器人跟我汇报我刚说过什么。
        if str(ev.get("reason") or "") == "我自己发的":
            continue

        # 机器人自动发的问候不是「有人找你」
        if any(m in text for m in _NOISE_TEXT):
            continue

        reason = str(ev.get("reason") or "")
        replied = bool(ev.get("replied"))
        # 三档的核心：是不是冲我来的。命中关键词只说明「在我的域里」，
        # 不代表要我动手 —— 群里通报发版进度也会命中「发版」。
        # 读字段，不去猜 reason 文案：采集层已经判过并落成了 directed。
        directed = pm_watch.directed_of(ev)
        if directed:
            verdict = DONE if replied else ACTION
        else:
            verdict = FYI

        items.append(TriageItem(
            idx=0,
            message_id=ev.get("message_id", ""),
            chat_name=ev.get("chat_name") or "私聊",
            chat_mode=ev.get("chat_mode") or "group",
            sender=ev.get("sender_name") or (ev.get("sender_id") or "")[:10],
            text=text,
            ts=ts,
            verdict=verdict,
            reason=reason,
            sender_id=ev.get("sender_id", ""),
            replied=replied,
            directed=directed,
            context=str(ev.get("context") or ""),
        ))

    # 无关的明细留在采集层，这里只要一个总数，够他说「今天挺清静」了
    noise: Dict[str, int] = {}
    dropped = int((snap.stats or {}).get("irrelevant") or 0)
    if dropped:
        noise["跟你无关"] = dropped

    # 同一件事刷多条只留一条，否则清单会被一个话题占满
    deduped: List[TriageItem] = []
    for it in items:
        if any(pm_watch._similar(it.text, d.text) and d.chat_name == it.chat_name for d in deduped):
            continue
        deduped.append(it)

    # 要动手的排最前，其次知情，再次已办
    order = {ACTION: 0, FYI: 1, DONE: 2, IRRELEVANT: 3}
    deduped.sort(key=lambda x: (order.get(x.verdict, 9), x.ts))

    # 碎片合成事项。必须在编号之前 —— 编号是给他回复用的，必须按事项编，
    # 否则「去掉 3」指的是第三条消息还是第三件事，两个人理解不一样。
    deduped = _cluster(deduped)
    for i, it in enumerate(deduped, 1):
        it.idx = i

    try:
        pm_people.apply_to(deduped, "sender_id", "sender")
    except Exception as e:
        logger.warning("回填人名失败（不影响清单）: %s", e)

    # 摘要必须在编号固定之后做，模型要按 idx 对齐
    summarize(deduped)

    # 模型判为 dup 的段落不渲染。必须真的从清单里去掉 —— 只标状态不删的话，
    # 编号会留空洞，他回「去掉 5」就对不上任何东西。
    merged = sum(1 for it in deduped if it.verdict == MERGED)
    if merged:
        deduped = [it for it in deduped if it.verdict != MERGED]

    # summarize 里会用语义判定改写 verdict（比如 done→action），顺序变了。
    # 推送前再排一次、再编一次，保证他回复的编号和看到的条目一致。
    deduped.sort(key=lambda x: (order.get(x.verdict, 9), x.ts))
    for i, it in enumerate(deduped, 1):
        it.idx = i

    logger.info(
        "过审采集完成: %d 件（要动手 %d / 知情 %d / 已办 %d / 并掉 %d）",
        len(deduped),
        sum(1 for x in deduped if x.verdict == ACTION),
        sum(1 for x in deduped if x.verdict == FYI),
        sum(1 for x in deduped if x.verdict == DONE),
        merged,
    )

    return TriageSheet(date=today.strftime("%Y-%m-%d"), items=deduped, noise=noise)


# ══════════════════════════════════════════════════════════════════════
# 总结 —— 这一层存在的唯一理由就是「不要甩原文」
# ══════════════════════════════════════════════════════════════════════

_SUMMARY_SYSTEM = (
    "你在帮项目经理李四（负责公募/私募/ESOP/票据四条产品线）把今天的消息变成待办。\n\n"
    "下面每一段是「一件事」，不是一条消息 —— 同一个人在同一会话里连着发的几句\n"
    "已经被合并成一段。请判断这件事要不要他动；要动的，直接写成待办。\n\n"
    "硬要求：\n"
    "1. summary 是一句话事实，不超过 40 字，用业务语言，**说清是哪件事**：\n"
    "   哪个产品线/需求/客户 + 卡在什么上。need 为 none 时，summary 就是全部输出。\n"
    "   ✗ 不合格：「孙雨滋说去问能否往后推迟」—— 推迟什么？哪个需求？他看完还得回去翻记录。\n"
    "   ✓ 合格：「孙雨滋要问 ESOP 出金上线能否推迟到下周」\n"
    "   ✗ 不合格：「李皇玉问提测」\n"
    "   ✓ 合格：「票据 v3.4.1 的商票模块明天能否提测」\n"
    "2. 如果给了「前文」，说明正文只有半句 —— 必须用前文把主语和对象补全，\n"
    "   让 summary 单独拿出来也读得懂。不要把前文单独写成一条，也不要复述前文。\n"
    "   前文里找不到答案就照实写「（不知道推迟什么）」，不要猜。\n"
    "3. need 只能取三个值：\n"
    '   - "act"   要他做事（拍板、排期、给口径、协调资源、发东西）\n'
    '   - "reply" 只需要他回一句（确认收到、答复是否、给个时间）\n'
    '   - "none"  他被抄送/通知，看到就行，不动也不回\n'
    "   判不准就填 \"act\"。宁可让他多扫一眼，也不要悄悄漏掉。\n"
    "4. need 不是 none 时，必须给 todo：\n"
    "   - 动词开头，说清动什么、动的是哪个东西，他看完不用再翻译一遍。\n"
    "   - 不要写「跟进一下」「回复对方」「处理这个问题」「确认细节」这种空话；\n"
    "     要写「把周九的 webportal 账密发给卢燕」「给公募 11.5 的排期定个上线日」。\n"
    "   - 一句话最多两个动作，用「，」分隔。\n"
    "5. who 是为这件事要找的人名。自己办就填空字符串。\n"
    "6. when 只能取 today / tomorrow / thisweek：\n"
    "   - today     今天就能办、或者已经拖了一天\n"
    "   - tomorrow  要等别人先动，或者今天已经来不及\n"
    "   - thisweek  本周内推进，没有具体日子\n"
    "7. why 是一句依据，说清为什么现在提这件事。写具体时间点或对方话里的关键点，\n"
    "   不要写「对方提到」「有需求」这种等于没说的。\n"
    "8. 同一个人对同一个交付物问了好几次（上午问最急的需求、下午问提测了没），\n"
    "   只写一件待办，summary 里把几个问题一起答。被并进去的那一段，\n"
    '   need 填 "dup"，不要重复写待办 —— 拆成两件他会以为有两件事要办。\n'
    "9. 只输出 JSON 数组，不要解释，不要代码围栏：\n"
    '   [{"idx": 1, "summary": "卢燕要周九的 webportal 账密", "need": "act", "todo": "把周九的 webportal 账密发给卢燕", "who": "卢燕", "when": "today", "why": "16:40 问的，还说让你去问建林有没有记录"}, {"idx": 2, "summary": "支付模块提测推迟到周四", "need": "none"}]\n'
)


def _parse_summaries(raw: str) -> Dict[int, Dict[str, str]]:
    """从模型输出里挖出 [{idx, summary, need}]。挖不动就返回空，由调用方退回原文。"""
    if not raw:
        return {}
    text = raw.strip()
    # 模型偶尔会套一层 ```json
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        rows = json.loads(text[start:end + 1])
    except Exception:
        return {}

    out: Dict[int, Dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("idx"))
        except (TypeError, ValueError):
            continue
        summary = " ".join(str(row.get("summary") or "").split())
        need = str(row.get("need") or "").strip().lower()
        if need not in ("act", "reply", "none", "dup"):
            need = ""
        when = str(row.get("when") or "").strip().lower()
        if when not in ("today", "tomorrow", "thisweek"):
            when = ""
        if not summary:
            continue
        out[idx] = {
            "summary": summary[:SUMMARY_LIMIT],
            "need": need,
            # 待办压到 60 字：再长就不像一句话，他要的是一眼扫完的清单
            "todo": " ".join(str(row.get("todo") or "").split())[:60],
            "who": " ".join(str(row.get("who") or "").split())[:16],
            "when": when,
            "why": " ".join(str(row.get("why") or "").split())[:40],
        }
    return out


def _refine_verdicts(items: List[TriageItem]) -> None:
    """用语义判定替换掉「30 分钟内发过言就算已处理」的粗暴规则。

    旧规则为什么不行：他在群里回一句「收到」，同群当天后面 5 条冲他来的消息
    全被判 done，真待办就这么静悄悄埋了。现在改成看消息本身 ——
    模型说不用动（none）才算已办，说 act/reply 就还是待办，哪怕他刚回过。
    """
    for it in items:
        if not it.need:
            continue  # 模型没给判断，保留初判，不要瞎改
        if it.need == "dup":
            # 模型说「这段已经被第 N 段代表了」。留着只会让同一件事出现两次。
            it.verdict = MERGED
            continue
        if not it.directed:
            it.verdict = FYI
        elif it.need == "none":
            it.verdict = DONE
        else:
            it.verdict = ACTION


def _summary_input(chunk: List[TriageItem]) -> str:
    """把一批事项拼成给模型的输入。

    上下文必须另起一行、明确标注：正文常常只有半句（「去问能否往后推迟」），
    模型只有靠前文才知道「推迟什么」。混进正文它会把前文当成新消息再总结一遍。
    """
    lines = []
    for it in chunk:
        # 原文要给全，否则模型只能编。碎片合并后可能偏长，压到 300 字。
        body = " ".join((it.text or "").split())[:300]
        # 他已经在旁边发过言的，明确告诉模型 —— 很多「冲他来」的消息
        # 其实是他已经答过的，不给这个线索模型会一律判 act。
        hint = "（他之后在群里发过言）" if it.replied else ""
        size = f"（合并了 {it.group_size} 条）" if it.group_size > 1 else ""
        line = f"{it.idx}. [{it.chat_name}] {it.sender}{hint}{size}：{body}"
        ctx = " ".join((it.context or "").split())[:300]
        if ctx:
            line += f"\n   前文：{ctx}"
        lines.append(line)
    return "\n".join(lines)


def _summarize_batch(chunk: List[TriageItem], attempts: int) -> Dict[int, Dict[str, str]]:
    """跑一批，自己重试，永不抛异常 —— 一批挂了不能连累其他批。

    单独成函数是为了能丢进线程池：批之间没有依赖，串行只是白白相加延迟。
    """
    payload = _summary_input(chunk)
    for attempt in range(1, attempts + 1):
        raw = call_llm_text(_SUMMARY_SYSTEM, payload, timeout=SUMMARY_TIMEOUT_SEC)
        if raw:
            got = _parse_summaries(raw)
            if got:
                return got
        logger.info("待办生成第 %d 次没返回（%d 件事），重试", attempt, len(chunk))
    logger.warning("待办生成失败，本批 %d 件事退回原文", len(chunk))
    return {}


def _apply_summaries(chunk: List[TriageItem], got: Dict[int, Dict[str, str]]) -> int:
    """把模型结果写回事项。返回命中条数。"""
    hit_count = 0
    for it in chunk:
        hit = got.get(it.idx)
        if not hit:
            continue
        it.summary = hit["summary"]
        it.need = hit["need"]
        it.todo = hit["todo"]
        it.who = hit["who"]
        it.when = hit["when"]
        it.why = hit["why"]
        # 模型判了要动却没给 todo：拿结论兜底，总比空着强
        if it.need in ("act", "reply") and not it.todo:
            it.todo = it.summary
        hit_count += 1
    return hit_count


def summarize(items: List[TriageItem]) -> None:
    """就地写 summary / need / todo / who / when / why。

    三层保证，按重要性排序：
    1. **有总预算**（SUMMARIZE_BUDGET_SEC）。18:00 推送是硬约定，卡住的批
       直接放弃 —— 少几条总结远好过整份推送不来。
    2. **并发**（_SUMMARY_WORKERS）。批之间无依赖，串行只会把延迟加起来。
    3. **失败退回原文**。一份没总结的清单，也好过他完全不知道今天发生了什么。

    已经带 summary 的跳过：会议行动项原文就是结论，再让模型改写一遍只会把
    「核对菜单配置」变成「确认菜单一致性」这种废化。
    """
    deadline = time.time() + SUMMARIZE_BUDGET_SEC

    # 两轮：第一轮全量；第二轮只补模型漏掉的 idx —— 它偶尔只回前面一半。
    for round_no in (1, 2):
        pending = [it for it in items if not it.summary]
        if not pending:
            break
        left = deadline - time.time()
        if left <= 5:
            logger.warning("总结预算用完，%d 件事未总结（退回原文）", len(pending))
            break
        if round_no > 1:
            logger.info("补漏: %d 件事模型没给，再试一轮", len(pending))

        # 第二轮只给一次机会：补漏是锦上添花，不能为了它把推送拖到十几分钟
        attempts = _SUMMARY_RETRY if round_no == 1 else 1
        chunks = [
            pending[i:i + _SUMMARY_BATCH]
            for i in range(0, len(pending), _SUMMARY_BATCH)
        ]

        # 不用 with：退出时会 join 所有线程，卡住的那批会把预算重新变成无上限。
        # 线程泄漏一个就泄漏一个，它随网关返回自然结束；推送不能被它拖住。
        pool = ThreadPoolExecutor(max_workers=_SUMMARY_WORKERS)
        futures = {
            pool.submit(_summarize_batch, c, attempts): c for c in chunks
        }
        done = 0
        try:
            for fut in as_completed(futures, timeout=max(1, deadline - time.time())):
                chunk = futures[fut]
                try:
                    done += _apply_summaries(chunk, fut.result())
                except Exception as e:
                    logger.warning("应用总结失败（%d 件事）: %s", len(chunk), e)
        except TimeoutError:
            logger.warning("总结超预算，放弃剩余 %d 批", len(futures) - done)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        logger.info(
            "待办生成(第%d轮): %d/%d 件事",
            round_no, sum(1 for it in pending if it.summary), len(pending),
        )

    _refine_verdicts(items)


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def _top_noise(noise: Dict[str, int], n: int) -> str:
    items = sorted(noise.items(), key=lambda kv: -kv[1])[:n]
    return "、".join(f"{k}×{v}" for k, v in items)


# 单条飞书消息的上限。飞书文本消息 content 有长度限制，超了整条发送失败；
# 截断又会丢信息 —— 他连着说了两次「展示不全」，所以选择拆包，不选择截断。
SEND_CHUNK_CHARS = 2600


_WHEN_TEXT = {"today": "今天", "tomorrow": "明天", "thisweek": "本周"}

_LINK_RE = re.compile(r"https?://\S+")


def _fallback_summary(it: TriageItem) -> str:
    """模型没给结论时的兜底。

    不能原样甩 120 字原文 —— 十几条长句堆在一起，等于没总结。
    去掉链接和 @ 之后压到 40 字，至少能一眼扫完。

    调用方必须给它加「〔原文〕」前缀：伪总结比原文更危险 —— 原文他至少知道
    自己看到的是没加工过的；一句被压缩过的半截话，会被当成结论去用。
    """
    raw = " ".join((it.text or "").split())
    raw = _LINK_RE.sub("〔文档〕", raw)
    raw = re.sub(r"@\S+\s*", "", raw).strip()
    return raw[:40] or "（一条没有正文的消息）"


def _todo_block(it: TriageItem) -> List[str]:
    """一条待办两行：做什么 + 凭什么现在做。

    第二行是他决定「留还是删」的唯一依据。缺了它，待办就只是命令，
    他看不出轻重，只会问「凭什么让我做这个」。
    """
    action = it.todo or it.summary or _fallback_summary(it)
    bits: List[str] = []
    if it.who:
        bits.append(f"找 {it.who}")
    bits.append(_WHEN_TEXT.get(it.when or "", "今天"))
    bits.append(it.why or f"{it.chat_name} · {it.sender}")
    return [f"{it.idx:>2}. {action}", f"   └ {' · '.join(bits)}"]


def _entries(sheet: TriageSheet) -> List[str]:
    """把清单拍平成段落序列，一个条目就是一段。

    拆包时以段为单位，保证不会把一个条目切成两半。

    结构上只有两段：要他动手的（待办形态，两行一条）、他该知道的（一行一条）。
    「已办」和「知情」合并 —— 对他来说这两类都是「知道了，不动手」，
    分两段只会让清单变长。
    """
    out: List[str] = [f"今日过审 · {sheet.date[5:]}"]

    if not sheet.items:
        out.append("今天没有需要你判断的事。")
        if sheet.noise:
            out.append(f"（另有 {sum(sheet.noise.values())} 条自动过滤：{_top_noise(sheet.noise, 4)}）")
        return out

    acts = sheet.section(ACTION)
    rest = [it for it in sheet.items if it.verdict in (FYI, DONE)]

    if acts:
        out.append(f"\n要你动手 · {len(acts)} 件")
        for it in acts:
            out.extend(_todo_block(it))

    if rest:
        out.append(f"\n你该知道 · {len(rest)} 条（不用你动手）")
        for it in rest:
            out.append(f"{it.idx:>2}. {it.summary or '〔原文〕' + _fallback_summary(it)}")

    if sheet.noise:
        out.append(f"\n自动过滤 · {sum(sheet.noise.values())} 条")
        out.append(_top_noise(sheet.noise, 5))

    out.append(
        "\n怎么回我：\n"
        "  「好」—— 全收成待办，明早 09:00 提醒你\n"
        "  「去掉 2 5」—— 这两件不记\n"
        "  「3 明天」—— 改到明天\n"
        "  「都无关」—— 上面这些都不用你管"
    )
    return out


def render_blocks(sheet: TriageSheet, max_chars: int = SEND_CHUNK_CHARS) -> List[str]:
    """渲染成 1..N 条消息。宁可多发几条，也不截断。"""
    packed: List[str] = []
    cur: List[str] = []
    size = 0

    for seg in _entries(sheet):
        seg_len = len(seg) + 1
        if cur and size + seg_len > max_chars:
            packed.append("\n".join(cur))
            cur, size = [], 0
        cur.append(seg)
        size += seg_len
    if cur:
        packed.append("\n".join(cur))

    if len(packed) == 1:
        return packed
    # 分多条时标上「1/3」，否则他不知道还有没有下文
    return [f"（{i}/{len(packed)}）\n{block}" for i, block in enumerate(packed, 1)]


def render(sheet: TriageSheet) -> str:
    """完整清单。要分条发送请用 render_blocks。"""
    return "\n".join(_entries(sheet))


# ══════════════════════════════════════════════════════════════════════
# 待办池
# ══════════════════════════════════════════════════════════════════════

def _load_todos() -> List[Dict[str, Any]]:
    try:
        return json.loads(_TODOS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_todos(todos: List[Dict[str, Any]]):
    try:
        _TODOS_PATH.write_text(
            json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写待办池失败: %s", e)


def add_todos(items: List[TriageItem]) -> int:
    """把待办写进池子。同一件事不重复加。

    落库的是 todo（动词开头那一句），不是 summary —— 第二天早上他要看的是
    「该干什么」，不是「昨天发生了什么」。
    """
    todos = _load_todos()
    seen = {t.get("message_id") for t in todos}
    added = 0
    for it in items:
        if it.message_id in seen:
            continue
        action = it.todo or it.summary or it.text
        todos.append({
            "message_id": it.message_id,
            # text 保留给老代码读；新逻辑一律读 todo
            "text": action,
            "todo": action,
            "who": it.who,
            "when": it.when or "today",
            "why": it.why,
            "chat_name": it.chat_name,
            "sender": it.sender,
            "from_date": _today_key(),
            "created_at": time.time(),
            "done": False,
        })
        seen.add(it.message_id)
        added += 1
    _save_todos(todos)
    return added


def _age_days(t: Dict[str, Any]) -> int:
    """这条待办挂了几天。0 = 今天记的。"""
    try:
        return (date.today() - date.fromisoformat(str(t.get("from_date") or ""))).days
    except Exception:
        return 0


def render_open_todos(limit: int = 8) -> str:
    """早间提醒用的待办清单。没有就返回空串。

    排序按「烂了多久」：挂 3 天的排最前。一直躺着没人动的那些才是真问题，
    昨天刚记的反而可以等一等。
    """
    pending = [t for t in _load_todos() if not t.get("done")]
    if not pending:
        return ""

    pending.sort(key=lambda t: (-_age_days(t), str(t.get("when") or "")))

    lines = [f"【手头待办 · {len(pending)} 件】"]
    for i, t in enumerate(pending[:limit], 1):
        action = t.get("todo") or t.get("text") or ""
        lines.append(f"{i:>2}. {str(action)[:60]}")
        bits: List[str] = []
        if t.get("who"):
            bits.append(f"找 {t['who']}")
        days = _age_days(t)
        if days >= 2:
            bits.append(f"挂 {days} 天")
        if bits:
            lines.append(f"    └ {' · '.join(bits)}")
    if len(pending) > limit:
        lines.append(f"…另有 {len(pending) - limit} 件")
    lines.append("回「1 做完了」划掉，「2 明天」推一天。")
    return "\n".join(lines)


def _today_key() -> str:
    return date.today().strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════
# 交互：推送 → 他回复 → 收敛
# ══════════════════════════════════════════════════════════════════════

_PENDING: Dict[str, str] = {}


def has_pending(sender: str) -> bool:
    # 内存标记存的是「清单日期」，不是裸的 "awaiting"。
    # 存裸标记的话，昨天推过的清单会在进程存活期间一直有效 —— 这样锁过一整天。
    if _PENDING.get(sender) == _today_key():
        return True
    _PENDING.pop(sender, None)
    sheet = TriageSheet.load()
    if sheet and sheet.stage == "awaiting" and sheet.date == _today_key() and sheet.items:
        _PENDING[sender] = sheet.date
        return True
    return False


def _stage_guard(sheet: TriageSheet) -> str:
    """清单为空时不许进入 awaiting。

    这是 2026-11-02 事故的根因：采集回来 0 条，照样推了空清单、
    照样把状态锁成 awaiting，于是接下来十几个小时里，他说的每一句带数字的话
    （「5 7已完成」）都被这份空清单劫走，回复「清单里没有第 5 条，当前共 0 条」。
    一个空清单没有资格要求任何人确认。
    """
    if sheet.items:
        return ""
    sheet.stage = "empty"
    sheet.save()
    return "今天没扫到需要你过目的消息，过审清单是空的。"


def stage(sender: str, sheet: TriageSheet) -> str:
    if not sheet.items:
        _PENDING.pop(sender, None)
        return _stage_guard(sheet)
    sheet.stage = "awaiting"
    sheet.pushed_at = time.time()
    sheet.save()
    _PENDING[sender] = sheet.date
    return render(sheet)


def stage_blocks(sender: str, sheet: TriageSheet) -> List[str]:
    """同 stage，但返回可拆条发送的多段正文。"""
    if not sheet.items:
        _PENDING.pop(sender, None)
        return [_stage_guard(sheet)]
    sheet.stage = "awaiting"
    sheet.pushed_at = time.time()
    sheet.save()
    _PENDING[sender] = sheet.date
    return render_blocks(sheet)


_NUM_RE = re.compile(r"\d{1,3}")
_IRRELEVANT_WORDS = ("无关", "不是我的", "不用管", "跳过", "别推", "没关系", "不用")
_ACCEPT_WORDS = ("好", "确认", "没问题", "ok", "OK", "可以", "都对", "就这样")

# 「去掉 2 5」—— 只剔掉不要的，剩下的默认收下。
# 这是从「挑编号」改成「审清单」的关键：挑编号要他把留下的都打一遍，
# 剔只要打掉的那几个。他嫌累的就是前者。
_DROP_WORDS = ("去掉", "删掉", "删除", "不要", "不做", "不记")
# 「3 明天」—— 挪期，不是删。挪完照样进待办。
_POSTPONE_WORDS = ("明天", "后天", "推一天", "往后推", "改期", "推迟")

# 整体否定的说法。他那天的原话是「这六条都和我无关」，一个数字都没给 ——
# 只认编号的话，这句最关键的反馈会被当成闲聊丢掉。
# 注意「六」是中文数字，正则 \d 抓不到，所以整体否定必须单独判。
_ALL_IRRELEVANT = (
    "无关", "没关系", "不是我的", "都不是我的",
    "都无关", "全无关", "全部无关", "都不用",
)


def handle_pending(sender: str, text: str) -> Optional[str]:
    """解析他对清单的回复。没听明白就返回 None，交给后面的模型处理。"""
    sheet = TriageSheet.load()
    if not sheet or sheet.stage != "awaiting":
        _PENDING.pop(sender, None)
        return None

    body = (text or "").strip()
    nums = [int(n) for n in _NUM_RE.findall(body)]

    # 「都无关」—— 不带编号，但把整张清单否掉
    if not nums and any(w in body for w in _ALL_IRRELEVANT):
        return _reject_all(sender, sheet)

    if not nums and any(w in body for w in _ACCEPT_WORDS):
        return commit(sender, sheet)

    if not nums:
        return None

    by_idx = {it.idx: it for it in sheet.items}
    targets = [by_idx[n] for n in nums if n in by_idx]
    if not targets:
        # 编号对不上，说明他说的不是这份清单（最常见：说的是 09:00 日计划里的编号）。
        # 这时候回「清单里没有第 5 条」只会把他带偏，必须放行给下游。
        logger.info("编号 %s 不在过审清单 %s 内，放行", nums, [it.idx for it in sheet.items][:10])
        return None

    if any(w in body for w in _DROP_WORDS):
        for it in targets:
            it.verdict = IRRELEVANT
        sheet.save()
        names = "、".join(str(it.idx) for it in targets)
        # 剔完直接落库，不再要一句「好」—— 多一轮确认就是多一份负担
        return f"第 {names} 件去掉。\n{commit(sender, sheet)}"

    if any(w in body for w in _POSTPONE_WORDS):
        for it in targets:
            it.when = "tomorrow"
            it.verdict = ACTION
        sheet.save()
        names = "、".join(str(it.idx) for it in targets)
        return f"第 {names} 件挪到明天。\n{commit(sender, sheet)}"

    if any(w in body for w in _IRRELEVANT_WORDS):
        for it in targets:
            it.verdict = IRRELEVANT
        sheet.save()
        names = "、".join(str(it.idx) for it in targets)
        return f"第 {names} 件判错，以后同类少推。\n{commit(sender, sheet)}"

    for it in targets:
        it.verdict = ACTION
    added = add_todos(targets)
    sheet.save()

    lines = [f"已记 {len(targets)} 件待办（新增 {added}）："]
    for it in targets[:8]:
        lines.append(f"· {it.todo or it.summary or it.text[:30]}")
    lines.append("\n明早 09:00 会带着它们提醒你。")
    return "\n".join(lines)


def _reject_all(sender: str, sheet: TriageSheet) -> str:
    """整张清单都不要 —— 全部标无关，并把这件事记成人名/域规则的反馈点。

    只改这一天的结论，不动 pm_scope：他的判断在这里是「今天这些事不用我做」，
    不等于「这个业务线不归我」。规则调整得他自己明确说。
    """
    hit = 0
    for it in sheet.items:
        if it.verdict in (ACTION, FYI):
            it.verdict = IRRELEVANT
            hit += 1
    sheet.stage = "committed"
    sheet.save()
    _PENDING.pop(sender, None)
    return (
        f"好，这 {hit} 条都按无关处理，不进待办，明天不会提醒你。\n\n"
        "顺带说一句：如果你想让「通报类」以后干脆不推给你、只留真正 @你 和问你的，"
        "跟我说一声，我把它做成默认。"
    )


def commit(sender: str, sheet: Optional[TriageSheet] = None) -> str:
    """按当前分类落库。只有 action 项进待办池。"""
    sheet = sheet or TriageSheet.load()
    if not sheet:
        _PENDING.pop(sender, None)
        return "没有待过审的清单。"

    todos = sheet.section(ACTION)
    added = add_todos(todos)
    sheet.stage = "committed"
    sheet.save()
    _PENDING.pop(sender, None)

    parts = [f"收下了 {len(todos)} 件待办（新增 {added}）"]
    parts.append("，明早 09:00 提醒你。" if added else "。")
    rest = len(sheet.section(FYI)) + len(sheet.section(DONE))
    if rest:
        parts.append(f"另外 {rest} 条只做知情，不占你的动作。")
    return "".join(parts)


# ══════════════════════════════════════════════════════════════════════
# 定时入口
# ══════════════════════════════════════════════════════════════════════

def run_daily(sender: str) -> str:
    sheet = collect()
    return stage(sender, sheet)


def run_daily_blocks(sender: str) -> List[str]:
    """18:00 入口，返回可拆条推送的正文。"""
    sheet = collect()
    return stage_blocks(sender, sheet)