"""PM Session - 会话记忆

核心判断：**记忆不是聊天记录。**
把过去 10 句话原样塞给模型，它仍然不知道"第 3 条"是哪条需求 ——
因为"第 3 条"指的是上一轮我们自己渲染出来的那个列表，不是用户说过的话。

所以本模块存两样东西：

    turns  对话原文        —— 让模型知道聊到哪儿了
    ctx    可指代现场      —— 让模型知道"它/那个/第n条"指向什么

ctx 由工具在产生结构化结果时写入（见 pm_tools 的 _ctx 参数），
loop 每轮把它渲染进 system 消息。这是多轮指代唯一的可靠实现方式：
不靠模型猜，靠我们自己给。

为什么要分两级（旧版是一刀切不落盘）
-----------------------------------
旧版不落盘的理由是「昂贵的错误是用昨天的上下文答今天的问题」。
这个顾虑是对的，但一刀切丢光带来另一个问题：
他昨天说过「以后催办都叫进哥」，今天新建会话，agent 又忘了。

    ┌─ 6 小时 ──────────────────────────── 7 天 ────────►
    │  同一段：完全信任
    │         原文全量 + ctx 全量
    │
    │  旧对话：只给话题，不给原文
    │         · turns 清空 —— 不是裁，是一条都不回放
    │         · ctx 清空 —— 「第 3 条」不能指向昨天的清单
    │         · digest 顶上 —— 只留「他问过什么」，最多 6 条
    │         · 明确要求模型去 pm_thread 现查，不许照搬
    │
    └ 超过 7 天：丢掉

为什么不回放原文
---------------
原文里有一半是我们自己的回复，而那些回复里的数字、结论都会过期。
模型看到「11.26 发版」就敢说，问题是我们自己也可能记错、需求也可能改。
所以旧对话只保留**问句**（他要什么），结论一律现查。
这也是「总结」和「搬运」的分界：搬运是留原文，总结是留线索 + 指向事实源。

落盘形态
--------
.sessions.json 一个文件装所有 sender。单用户场景下没必要分文件，
读改写整块的开销（几 KB）远小于维护多文件的复杂度。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pm_hub.session")

_PROJECT_DIR = Path(__file__).resolve().parents[3]
STORE_PATH = _PROJECT_DIR / ".sessions.json"

# 热窗口：这段时间内算「同一段对话」，原文全量保留，行为和旧版一致。
SESSION_TTL = timedelta(minutes=30)

# 超过多久算「真的旧」。用时间跨度而不是日期边界，
# 避免 23:50 聊完、00:05 再问就被当成隔夜。
# 6 小时以内还是「这次接着上次」，ctx 继续有效，也不打旧对话提示。
STALE_AFTER = timedelta(hours=6)

# 冷窗口：超过热窗口但在这之内，重启/隔夜后还能捞回来，但降级成「一句话话题记录」。
RESUME_TTL = timedelta(days=7)

# 跨天压缩时，最多留几个他问过的话题。
DIGEST_MAX_ASKS = 6

# 每条话题抽多少字。够看清问的是什么，不够承载结论。
DIGEST_ASK_CHARS = 60

# 保留多少轮原文（一轮 = 一问一答）
MAX_TURNS = 8

# 原文总字节上限。轮数控制不住长文，字节才控制得住。
# 超了从最早的整轮开始丢 —— 一次只丢一条会把问答对拆散，模型看不懂。
MAX_TURNS_BYTES = 24_000

# 最多同时记多少个会话，防止长跑内存泄漏。超过按最久未用淘汰。
MAX_SESSIONS = 50

# ctx 落盘上限。超过就只存 turns —— ctx 里可能挂着几百条消息摘要，
# 那是给当场看的，不该变成跨天记忆的负担。
CTX_MAX_BYTES = 16_000


@dataclass
class Turn:
    role: str          # "user" | "assistant"
    content: str


def _trim(turns: List[Turn]) -> List[Turn]:
    """条数 + 字节双限，超了从最早的整轮开始丢。

    一次只丢一条会把一问一答拆散，模型看到孤立的一句回答只会更糊涂。
    """
    while len(turns) > MAX_TURNS * 2:
        turns = turns[2:]
    while len(turns) > 2 and sum(len(t.content.encode("utf-8")) for t in turns) > MAX_TURNS_BYTES:
        turns = turns[2:]
    return turns


def _condense(turns: List[Turn], when: datetime) -> str:
    """把一段对话压成话题记录。

    只抽他自己的话。我们自己的回复是结论，而结论的正确来源是 pm_thread /
    工作簿 / story，不是聊天记录 —— 从聊天记录里考古最容易给出过期结论。
    """
    asked: List[str] = []
    for t in turns:
        if t.role != "user":
            continue
        line = " ".join(t.content.split())
        if not line or line in asked:
            continue
        asked.append(line[:DIGEST_ASK_CHARS] + ("…" if len(line) > DIGEST_ASK_CHARS else ""))

    if not asked:
        return ""

    asked = asked[-DIGEST_MAX_ASKS:]
    body = "\n".join(f"{i}. {q}" for i, q in enumerate(asked, 1))
    return f"【{when.strftime('%m月%d日 %H:%M')} 前后你问过】\n{body}"


@dataclass
class Session:
    sender: str
    turns: List[Turn] = field(default_factory=list)
    ctx: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=datetime.now)
    # 从磁盘捞回来的时间。None = 本进程内的连续对话，不做旧对话提示。
    resumed_at: Optional[datetime] = None
    # 跨天压缩出来的话题记录。只在 is_resumed 期间注入，
    # 它替代的是「把昨天 8 轮原文回放给模型」，不是「把昨天的结论复述给模型」。
    digest: str = ""

    # ── 写 ────────────────────────────────────────────────────────

    def say(self, role: str, content: str) -> None:
        self.turns.append(Turn(role=role, content=content))
        self.turns = _trim(self.turns)
        self.updated_at = datetime.now()
        _persist(self)

    def remember(self, **kwargs: Any) -> None:
        """登记可指代的对象。工具产出结构化结果后必须调用它。

        用法：session.remember(nudges=[{"no":1,"who":"赵六","what":"..."}])
        渲染时会变成一段编号清单，模型据此把"第3条"对上号。
        """
        self.ctx.update(kwargs)
        self.updated_at = datetime.now()
        _persist(self)

    def forget(self, *keys: str) -> None:
        hit = any(self.ctx.pop(k, None) is not None for k in keys)
        if hit:
            _persist(self)

    # ── 读 ────────────────────────────────────────────────────────

    @property
    def is_resumed(self) -> bool:
        return self.resumed_at is not None

    def stale_note(self) -> str:
        """跨天之后，只给「聊过什么」，不给「聊过什么原文」。

        区别很关键：
        - 给原文 = 让模型拿昨天的清单答今天的问题，正是旧版不落盘想避免的错
        - 给话题 = 让模型知道该去查哪件事，结论现查

        所以这里输出的是 digest（他自己的问句摘要），并明确禁止复述。
        """
        if not self.is_resumed:
            return ""
        head = (
            "【重要】下面是之前聊过的话题，不是刚刚发生的。\n"
            "不要用「刚才」「刚刚」「前面那个」指代它们。\n"
            "要确认某件事现在到哪一步，调 pm_thread 现查，**不要照搬这里的说法当结论**。"
        )
        return head + ("\n\n" + self.digest if self.digest else "")

    def render_ctx(self) -> str:
        """把可指代现场渲染成给模型看的一段文字。空则返回空串。"""
        if not self.ctx:
            return ""

        blocks: List[str] = []

        nudges = self.ctx.get("nudges")
        if nudges:
            lines = [
                f"{n.get('no')}. {n.get('who')} ← {n.get('what')}"
                for n in nudges
            ]
            blocks.append("上一轮你拟的催办清单（用户说「第n条」即指这里）：\n" + "\n".join(lines))

        radar = self.ctx.get("radar")
        if radar:
            blocks.append(f"上一轮查过的排期：{radar}")

        items = self.ctx.get("weekly_items")
        if items:
            lines = [f"{i.get('no')}. {i.get('task')}  [{i.get('status')}]" for i in items]
            blocks.append("本周计划事项（用户说「第n条」即指这里）：\n" + "\n".join(lines))

        events = self.ctx.get("events")
        if events:
            lines = [
                f"{e.get('no')}. {e.get('chat')} · {e.get('who')}：{e.get('digest')}"
                for e in events
            ]
            blocks.append("上一轮列出的感知消息（用户说「第n条」即指这里）：\n" + "\n".join(lines))

        todos = self.ctx.get("todos")
        if todos:
            lines = [f"{t.get('no')}. {t.get('chat')}：{t.get('text')}" for t in todos]
            blocks.append("待办池当前清单（用户说「第n条」即指这里）：\n" + "\n".join(lines))

        extra = self.ctx.get("note")
        if extra:
            blocks.append(str(extra))

        if not blocks:
            return ""
        return "【当前上下文】\n" + "\n\n".join(blocks)

    def render_turns(self) -> List[Dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in self.turns]


# ══════════════════════════════════════════════════════════════════════
# 落盘
# ══════════════════════════════════════════════════════════════════════


def _read_store() -> Dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 文件坏了不能拖垮对话，当成空的，下次写会覆盖掉
        logger.warning("会话文件读取失败，按空处理：%s", STORE_PATH)
        return {}


def _write_store(data: Dict[str, Any]) -> None:
    try:
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STORE_PATH)
    except Exception:
        logger.warning("会话文件写入失败：%s", STORE_PATH)


def _as_record(sess: Session) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    if sess.ctx:
        try:
            blob = json.dumps(sess.ctx, ensure_ascii=False)
            if len(blob.encode("utf-8")) <= CTX_MAX_BYTES:
                ctx = sess.ctx
        except (TypeError, ValueError):
            ctx = {}
    return {
        "updated_at": sess.updated_at.isoformat(timespec="seconds"),
        "turns": [{"role": t.role, "content": t.content} for t in sess.turns],
        "ctx": ctx,
        "digest": sess.digest,
    }


def _prune(store: Dict[str, Any]) -> None:
    """清掉超过冷窗口的记录，避免文件无限长大。"""
    now = datetime.now()
    for key in list(store.keys()):
        rec = store.get(key)
        if not isinstance(rec, dict):
            store.pop(key, None)
            continue
        try:
            age = now - datetime.fromisoformat(rec.get("updated_at", ""))
        except Exception:
            store.pop(key, None)
            continue
        if age > RESUME_TTL:
            store.pop(key, None)


def _persist(sess: Session) -> None:
    store = _read_store()
    store[sess.sender] = _as_record(sess)
    _prune(store)
    _write_store(store)


def _restore(sender: str) -> Optional[Session]:
    rec = _read_store().get(sender)
    if not isinstance(rec, dict):
        return None
    try:
        updated = datetime.fromisoformat(rec.get("updated_at", ""))
    except Exception:
        return None

    age = datetime.now() - updated
    if age > RESUME_TTL:
        return None

    turns = [
        Turn(role=str(t.get("role", "")), content=str(t.get("content", "")))
        for t in (rec.get("turns") or [])
        if isinstance(t, dict) and t.get("role")
    ]

    sess = Session(sender=sender, updated_at=updated)

    if age <= STALE_AFTER:
        # 还是「这次接着上次」：原文和 ctx 都原样恢复，不打旧对话提示。
        # 不这么做的话，每半小时说一句都会被当成隔夜，模型会莫名变谨慎。
        sess.turns = turns
        sess.ctx = rec.get("ctx") or {}
        sess.digest = ""
        return sess

    # 真的旧了：一条原文都不回放，只留「他问过什么」。
    #
    # 为什么不裁到最近 2 轮原文：原文里有我们自己的结论，模型会照搬。
    # 结论的正确来源是 pm_thread / 工作簿 / story，而不是聊天记录。
    # 话题摘要 + 明确「去现查」才是指代和事实都安全的形式。
    sess.turns = []
    sess.ctx = {}
    sess.digest = _condense(turns, updated) if turns else (rec.get("digest") or "")
    sess.resumed_at = datetime.now()
    return sess


# ══════════════════════════════════════════════════════════════════════
# 会话仓库
# ══════════════════════════════════════════════════════════════════════

_SESSIONS: "OrderedDict[str, Session]" = OrderedDict()


def _evict_expired() -> None:
    """只清内存，磁盘保留 —— 下次 get() 会从冷窗口捞回来。"""
    now = datetime.now()
    for sender in [s for s, sess in _SESSIONS.items() if now - sess.updated_at > SESSION_TTL]:
        _SESSIONS.pop(sender, None)


def get(sender: str) -> Session:
    """取会话。内存没有就试磁盘，磁盘也没有才新建。"""
    _evict_expired()
    sess = _SESSIONS.get(sender)
    if sess is None:
        sess = _restore(sender) or Session(sender=sender)
        _SESSIONS[sender] = sess
    else:
        _SESSIONS.move_to_end(sender)
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.popitem(last=False)
    return sess


def reset(sender: str) -> None:
    """彻底忘掉一个人的会话，内存和磁盘一起清。"""
    _SESSIONS.pop(sender, None)
    store = _read_store()
    if store.pop(sender, None) is not None:
        _write_store(store)


def stats() -> Dict[str, Any]:
    return {
        "sessions": len(_SESSIONS),
        "detail": {
            s: {
                "turns": len(x.turns),
                "ctx_keys": list(x.ctx.keys()),
                "resumed": x.is_resumed,
            }
            for s, x in _SESSIONS.items()
        },
    }