"""PM Awareness - 「我最近知道什么」的唯读取口

它补的是哪一环
-------------
改造前，感知结果有三份落盘产物，但它们只流向一个地方：

    pm_watch.full_sweep()  ──► .watch_snapshot.json ──► pm_daily_triage（18:00 一次性推送）
    pm_daily_triage        ──► .triage_state.json
                           └─► .triage_todos.json  ──► 次日 09:00 日计划

管道是通的，但**只有机器在跑，对话里读不到**。于是他坐在私聊里问
「今天群里都发生了什么」「我还欠谁的事没回」，机器人只能去查版本计划表 ——
明明数据就在磁盘上，答不出来。这就是他说的「像个查询终端，没有全局感知」。

本模块把这三份落盘产物收成一个视图，给 pm_tools 提供四个原子能力：

    overview()        一句话全景          → 塞进 system prompt，让它开口就带着现场
    recent_events()   最近的感知事件       → 「今天群里有什么」
    open_todos()      待办池              → 「我还欠什么」
    close_todos()     划掉待办            → 「第 2 条做完了」

边界：**只读与呈现，不判断**。
相关性、分档、结论都已经在 pm_watch / pm_daily_triage 里算完了。
这一层如果再判一次，两处口径迟早不一致，到时候没人说得清哪边是对的。

为什么不缓存
-----------
快照最大 10KB，读一次 1ms 级。三个文件的 mtime 各自变化，
做缓存要处理三个失效点，收益却是零。只缓存聚合后的 overview 文本（TTL 30s），
因为它在每次对话开头都要用。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("pm_hub.awareness")

# handlers → agents → app → feishu-agent-platform
_PROJECT_DIR = Path(__file__).resolve().parents[3]

SNAPSHOT_PATH = _PROJECT_DIR / ".watch_snapshot.json"
TRIAGE_STATE_PATH = _PROJECT_DIR / ".triage_state.json"
TRIAGE_TODOS_PATH = _PROJECT_DIR / ".triage_todos.json"

# overview 的缓存时长。太短等于每轮对话重算，太长会让他刚处理完的事还挂在抬头。
_OVERVIEW_TTL_SEC = 30
_OVERVIEW_CACHE: Dict[str, Any] = {"at": 0.0, "text": ""}


# ══════════════════════════════════════════════════════════════════════
# 原始读取
# ══════════════════════════════════════════════════════════════════════

def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("读 %s 失败: %s", path.name, e)
        return None


def snapshot() -> Dict[str, Any]:
    """感知快照。结构：{date, events[], hot[], full_swept_at, stats}"""
    data = _read_json(SNAPSHOT_PATH)
    return data if isinstance(data, dict) else {}


def triage_sheet() -> Dict[str, Any]:
    """过审清单的落盘状态。结构：{date, items[], noise{}, stage, pushed_at}"""
    data = _read_json(TRIAGE_STATE_PATH)
    return data if isinstance(data, dict) else {}


def todo_pool() -> List[Dict[str, Any]]:
    data = _read_json(TRIAGE_TODOS_PATH)
    return data if isinstance(data, list) else []


def _write_todos(todos: List[Dict[str, Any]]) -> bool:
    try:
        TRIAGE_TODOS_PATH.write_text(
            json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _OVERVIEW_CACHE["at"] = 0.0  # 待办变了，抬头必须重算
        return True
    except Exception as e:
        logger.warning("写待办池失败: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════
# 事件视图
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Event:
    chat_name: str
    chat_mode: str          # p2p | group
    sender: str
    text: str
    ts: int                 # 毫秒
    reason: str
    replied: bool
    directed: bool = False  # 是不是冲他来的。口径在 pm_watch.is_directed
    message_id: str = ""
    sender_id: str = ""

    @property
    def when(self) -> str:
        try:
            return datetime.fromtimestamp(self.ts / 1000).strftime("%H:%M")
        except Exception:
            return ""

    @property
    def waiting(self) -> bool:
        """他在被人等。

        只有「冲他来的 + 他没回」才算。仅凭 replied=False 会把别人之间的
        通报也算成待回应 —— 周德政 @陈天顺 走工单，跟他没有半毛钱关系，
        但在旧口径里就是一条「未回」。
        """
        return self.directed and not self.replied

    @property
    def status(self) -> str:
        """三态。二态（未回/已回）在「别人之间的通报」上会撒谎 ——
        那条既不是待他回，也不是他回过了，他根本不在对话里。
        """
        if self.waiting:
            return "等你回"
        if self.directed:
            return "你回过了"
        return "知会"

    def render(self, with_reason: bool = True) -> str:
        who = self.sender if self.chat_mode == "p2p" else f"{self.chat_name} · {self.sender}"
        line = f"{self.when} {who}：{clip_text(self.text, 90)}"
        line += f"  〔{self.status}〕"
        if with_reason and self.reason:
            line += f"  （{self.reason}）"
        return line


_URL_RE = re.compile(r"https?://\S+")


def clip_text(text: str, limit: int = 90) -> str:
    """剥长链接 → 压空白 → 截断。

    飞书的 story / wiki 链接有 60-80 字符。一条消息里带一条，
    清单就被挤成一坨，真正要判断的正文反而看不见了。链接对「这是什么事」没有帮助。
    """
    body = _URL_RE.sub("[链接]", " ".join((text or "").split()))
    return body[:limit] + ("…" if len(body) > limit else "")


def _clean_name(name: str, sender_id: str) -> str:
    """快照里的 sender_name 可能是截断的 open_id（旧的感知进程留下的）。

    人名缓存已经存在，就地兜一层。查不到就退回「某人」，不要拿 ou_xxx 糊他的脸 ——
    一屏推送里出现三行 `ou_b72f333a9`，他第一反应就是「这什么烂东西」。
    """
    name = (name or "").strip()
    if name and not name.startswith("ou_"):
        return name
    if sender_id:
        from app.domains.pm import pm_people
        try:
            real = pm_people.name_of(sender_id)
            if real:
                return real
        except Exception:
            pass
    return "某人"


def _directed_of(row: Dict[str, Any]) -> bool:
    """口径只在 pm_watch 定义。这里懒加载 —— pm_watch 是个重模块，
    为了一个布尔判定在 import 期拖进整条 lark-cli 依赖不值得。

    传整行而不是 reason 字符串：老快照没有 directed 字段，
    兼容回退在 pm_watch.directed_of 里做，只有一份。
    """
    from app.domains.pm import pm_watch
    return pm_watch.directed_of(row)


def recent_events(days: int = 1, only_waiting: bool = False) -> List[Event]:
    """取感知到的事件。days=0 表示不限日期（只要快照里还在）。"""
    snap = snapshot()
    raw = snap.get("events") or []
    if not raw:
        return []

    cutoff_ms = 0
    if days > 0:
        cutoff_ms = int((time.time() - days * 86400) * 1000)

    out: List[Event] = []
    for e in raw:
        try:
            ts = int(e.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if cutoff_ms and ts < cutoff_ms:
            continue
        sid = str(e.get("sender_id") or "")
        reason = str(e.get("reason") or "")
        ev = Event(
            chat_name=str(e.get("chat_name") or ""),
            chat_mode=str(e.get("chat_mode") or "group"),
            sender=_clean_name(str(e.get("sender_name") or e.get("sender") or ""), sid),
            text=str(e.get("text") or ""),
            ts=ts,
            reason=reason,
            replied=bool(e.get("replied")),
            directed=_directed_of(e),
            message_id=str(e.get("message_id") or ""),
            sender_id=sid,
        )
        if only_waiting and not ev.waiting:
            continue
        out.append(ev)

    out.sort(key=lambda x: x.ts)
    return out


def snapshot_meta() -> Dict[str, Any]:
    snap = snapshot()
    stats = snap.get("stats") or {}
    swept = snap.get("full_swept_at")
    return {
        "date": snap.get("date") or "",
        "scanned": stats.get("scanned"),
        "irrelevant": stats.get("irrelevant"),
        "events": len(snap.get("events") or []),
        "swept_at": (
            datetime.fromtimestamp(float(swept)).strftime("%m-%d %H:%M") if swept else ""
        ),
    }


# ══════════════════════════════════════════════════════════════════════
# 待办池
# ══════════════════════════════════════════════════════════════════════

def open_todos() -> List[Dict[str, Any]]:
    """未完成的待办，按创建时间正序（老的先提醒）。"""
    todos = [t for t in todo_pool() if not t.get("done")]
    todos.sort(key=lambda t: float(t.get("created_at") or 0))
    return todos


def render_todos(limit: int = 8) -> str:
    """待办清单。口径与 09:00 推送、18:00 过审保持一致：动作 + 找谁 + 挂了几天。

    注意别再拼「群名 · 发送人：原文」—— 那是消息的样子，不是待办的样子。
    """
    todos = open_todos()
    if not todos:
        return ""

    lines = [f"我还欠这些（{len(todos)} 件）："]
    for i, t in enumerate(todos[:limit], 1):
        action = " ".join(str(t.get("todo") or t.get("text") or "").split())[:60]
        bits: List[str] = []
        if t.get("who"):
            bits.append(f"找 {t['who']}")
        frm = str(t.get("from_date") or "")
        if frm:
            try:
                delta = (date.today() - date.fromisoformat(frm)).days
                if delta > 0:
                    bits.append(f"挂 {delta} 天")
            except Exception:
                pass
        lines.append(f"{i}. {action}")
        if bits:
            lines.append(f"    └ {' · '.join(bits)}")
    if len(todos) > limit:
        lines.append(f"…另有 {len(todos) - limit} 件")
    return "\n".join(lines)


def close_todos(indices: Iterable[int]) -> Dict[str, Any]:
    """按「未完成清单里的序号」划掉。序号口径与 render_todos 一致。

    返回 {closed: [...], missing: [...]}。missing 非空时**不做任何写入** ——
    半截划掉比不划更难查：他以为全清了，实际只清了一半。
    """
    wanted = [int(i) for i in indices]
    if not wanted:
        return {"closed": [], "missing": [], "error": "没给编号"}

    todos = todo_pool()
    pending = [t for t in todos if not t.get("done")]
    pending.sort(key=lambda t: float(t.get("created_at") or 0))

    by_no = {i: t for i, t in enumerate(pending, 1)}
    missing = [i for i in wanted if i not in by_no]
    if missing:
        listing = "\n".join(
            f"{i}. {t.get('chat_name')} · {str(t.get('text'))[:40]}"
            for i, t in list(by_no.items())[:10]
        )
        return {
            "closed": [],
            "missing": missing,
            "error": f"没有第 {' '.join(map(str, missing))} 条。当前待办：\n{listing}",
        }

    closed: List[str] = []
    targets = {id(by_no[i]): by_no[i] for i in wanted}
    now = time.time()
    for t in todos:
        if id(t) in targets and not t.get("done"):
            t["done"] = True
            t["done_at"] = now
            closed.append(str(t.get("text"))[:40])

    if not _write_todos(todos):
        return {"closed": [], "missing": [], "error": "写待办池失败，没划掉。"}

    return {"closed": closed, "missing": []}


# ══════════════════════════════════════════════════════════════════════
# 过审清单
# ══════════════════════════════════════════════════════════════════════

def triage_state() -> Dict[str, Any]:
    """过审清单摘要：今天推过没有、几条待他过目、什么状态。"""
    sheet = triage_sheet()
    items = sheet.get("items") or []
    need_act = [
        it for it in items
        if str(it.get("need") or "") == "act" or str(it.get("verdict") or "") == "action"
    ]
    return {
        "date": str(sheet.get("date") or ""),
        "stage": str(sheet.get("stage") or ""),
        "is_today": str(sheet.get("date") or "") == date.today().isoformat(),
        "total": len(items),
        "need_act": len(need_act),
        "pushed": bool(sheet.get("pushed_at")),
    }


# ══════════════════════════════════════════════════════════════════════
# 全景：塞进 system prompt
# ══════════════════════════════════════════════════════════════════════

def overview() -> str:
    """一段话告诉模型「现在是什么局面」。空则返回空串。

    这一段的价值不在信息量，在**开口的姿态**：
    没有它，模型看到「催一下」只能问「催谁」；
    有它，模型知道有 3 条未回的私聊、2 条待办挂着，可以直接对上号。
    """
    now = time.time()
    if now - float(_OVERVIEW_CACHE["at"]) < _OVERVIEW_TTL_SEC:
        return str(_OVERVIEW_CACHE["text"])

    parts: List[str] = []

    # 只列「冲他来的 + 没回」。别人之间的通报不算欠着人家。
    waiting = recent_events(days=3, only_waiting=True)
    if waiting:
        shown = waiting[-6:]
        lines = [f"· {e.render(with_reason=False)}" for e in shown]
        head = f"有 {len(waiting)} 条是别人冲你来的，你还没回"
        if len(waiting) > len(shown):
            head += "（只列最近 6 条）"
        parts.append(head + "：\n" + "\n".join(lines))

    todos = open_todos()
    if todos:
        oldest = todos[0]
        frm = str(oldest.get("from_date") or "")
        age = ""
        try:
            delta = (date.today() - date.fromisoformat(frm)).days
            if delta > 0:
                age = f"，最老的一条挂了 {delta} 天"
        except Exception:
            pass
        parts.append(f"待办池里还挂着 {len(todos)} 条{age}。")

    st = triage_state()
    if st["is_today"] and st["stage"] == "awaiting":
        parts.append(f"今天的过审清单已经推给他了，{st['total']} 条在等他逐条过。")

    meta = snapshot_meta()
    if meta["swept_at"]:
        parts.append(
            f"最近一次全量扫会话：{meta['swept_at']}，"
            f"过滤 {meta['scanned'] or 0} 条消息，留下 {meta['events']} 条跟他相关的。"
        )
    elif meta["events"]:
        # 没有全量时间戳不等于没数据 —— 增量感知一直在写。
        # 说清楚这一点，用户问「你怎么知道」时才不会被一句「我没扫过」噎住。
        parts.append(
            f"今天还没做全量扫描（18:00 会做），增量感知已过滤 {meta['scanned'] or 0} 条，"
            f"留下 {meta['events']} 条跟他相关的。"
        )

    text = ""
    if parts:
        text = "【你掌握到的现场】\n" + "\n".join(parts)

    _OVERVIEW_CACHE["at"] = now
    _OVERVIEW_CACHE["text"] = text
    return text