"""PM Thread —「一件事」作为一等对象

问题
----
同一个事散在 5 个地方，彼此不认识：

    群里一句「1126 需求提测了」     ← pm_watch 事件
    story 7149167238 的提测节点     ← pm_project_mcp
    日计划「今1 跟进1126提测情况」  ← 多维表格
    17:30 你口头说的待跟进         ← pm_followup_intake
    会议纪要里的一句承诺            ← pm_daily_triage

现在只能分别汇报，五条独立信息。问「1126 那个私募怎么样了」，答不出时间线 ——
因为系统里没有「那件事」，只有五份互不相识的记录。

本模块补的就是这个对象
----------------------
    ingest(day)          把各路消息灌进事件日志（幂等）
    threads_of(day)      今天活着的「事」，按「需要你出手」排序
    timeline(thread)     一条事的跨来源时间线
    queue(day)           决策队列（09:00 / 18:00 的输出形态）

为什么用 append-only 日志而不是一张状态表
----------------------------------------
状态表要合并、要冲突消解、要处理「同一件事昨天判成 A 今天判成 B」。
日志只追加，视图每次 fold 出来 —— 判断错了改规则就行，历史不丢。
这也让「这条结论凭什么」永远可回溯到某一行。

如何把消息认到事上（只用确定的锚，不猜语义）
------------------------------------------
    1. story_id 直接出现
    2. 版本号命中【已知迭代集合】的写法（1126 / 11.26 / 11月26 / V2.43.8-20261112）
    3. 需求名连续 ≥6 字命中
锚不到的消息 → adhoc 事，按会话聚，绝不硬塞给需求。
宁可多一条 adhoc，也不要让「票据展示异常」这种噪音混进「1126 需求」的时间线。

职责边界
--------
只做「认领 + 聚合 + 渲染」。
筛什么进视野是 pm_scope 的事；判排期是 pm_schedule_risk 的事；发消息是 pm_send 的事。
本模块不判断业务归属，只回答「这条消息属于哪件事」。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.domains.pm import pm_story_source as src
from app.domains.pm.pm_config import owner_name, pm_config

logger = logging.getLogger("pm_hub.thread")

# handlers → agents → app → feishu-agent-platform
_PROJECT_DIR = Path(__file__).resolve().parents[3]

LOG_PATH = _PROJECT_DIR / ".threads.jsonl"
ANCHOR_CACHE = _PROJECT_DIR / ".threads_anchors.json"
SNAPSHOT_PATH = _PROJECT_DIR / ".watch_snapshot.json"
TRIAGE_STATE_PATH = _PROJECT_DIR / ".triage_state.json"

# 日志保留天数。太短问不出「上周那件事」，太长日志无限长。
KEEP_DAYS = 90

# 锚定表缓存。迭代不会天天变，一小时刷一次够了。
_ANCHOR_TTL_SEC = 3600


# ══════════════════════════════════════════════════════════════════════
# 版本写法
# ══════════════════════════════════════════════════════════════════════

def version_of(iteration: str) -> str:
    """迭代名 → 归一化版本号（YYYYMMDD）。

    "V2.43.8-20261112" → "20261112"
    "20261126"          → "20261126"
    """
    if not iteration:
        return ""
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", iteration)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}{mo:02d}{d:02d}"
    return ""


def aliases_of(version: str) -> Tuple[str, ...]:
    """一个版本在人嘴里有多少种说法。

    "20261126" → ("20261126", "11.26", "11-26", "11/26", "11月26", "1126")

    为什么只认这些写法：这些都是从**已知迭代**反推出来的。
    反过来「在文本里找像日期的数字」会把 3.4、1.5 这种版本号当日期（踩过）。
    """
    if len(version) != 8 or not version.isdigit():
        return ()
    month, day = int(version[4:6]), int(version[6:8])
    raw = (
        version,
        f"{month:02d}.{day:02d}",
        f"{month}.{day:02d}",
        f"{month:02d}-{day:02d}",
        f"{month}-{day}",
        f"{month}/{day}",
        f"{month}月{day}",
        f"{month}月{day:02d}",
        f"{month:02d}{day:02d}",      # 1126
        f"{month}{day:02d}",          # 126（不常见，但有人这么写）
    )
    # 去重但保持顺序 —— 短的写法优先级低，靠后
    seen: set[str] = set()
    out = []
    for a in raw:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return tuple(out)


# ══════════════════════════════════════════════════════════════════════
# 锚点表
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Anchor:
    """一条事凭什么被认出来。"""

    key: str                              # story:7149167238 | ver:20261126
    title: str
    kind: str                             # demand | version
    story_ids: Tuple[int, ...] = ()
    version: str = ""
    names: Tuple[str, ...] = ()           # 需求名（用于关键词命中）
    stage: str = ""
    owner: str = ""
    business: str = ""


def _anchor_of_story(s: src.Story) -> Anchor:
    return Anchor(
        key=f"story:{s.item_id}",
        title=s.name,
        kind="demand",
        story_ids=(s.item_id,),
        version=version_of(s.iteration),
        names=(s.name,),
        stage=s.stage,
        owner=s.operator,
        business=s.line,
    )


def _load_anchor_cache() -> Optional[List[dict]]:
    try:
        raw = json.loads(ANCHOR_CACHE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if time.time() - float(raw.get("at") or 0) > _ANCHOR_TTL_SEC:
        return None
    rows = raw.get("rows")
    return rows if isinstance(rows, list) else None


def _save_anchor_cache(anchors: Sequence[Anchor]) -> None:
    rows = [
        {
            "key": a.key, "title": a.title, "kind": a.kind,
            "story_ids": list(a.story_ids), "version": a.version,
            "names": list(a.names), "stage": a.stage,
            "owner": a.owner, "business": a.business,
        }
        for a in anchors
    ]
    try:
        ANCHOR_CACHE.write_text(
            json.dumps({"at": time.time(), "rows": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("写锚点缓存失败: %s", e)


def _iterations_in_play(today: Optional[date] = None) -> List[str]:
    """当前在跑的迭代名 —— 锚点表的地基。

    不取「MQL 返回的前 N 个」，那样拿到的是一批不相干的老迭代。
    改成：拉全量迭代名，解析名字里的日期，只留今天前 30 天到后 120 天。
    这个窗口覆盖「刚上线还在复盘」到「下下个版本已评审」。
    """
    today = today or date.today()
    lo = today - timedelta(days=30)
    hi = today + timedelta(days=120)

    try:
        all_iters = src.resolve("")
    except Exception as e:
        logger.warning("拉迭代列表失败: %s", e)
        return []

    picked: List[Tuple[date, str]] = []
    for it in all_iters:
        name = it.name or ""
        ver = version_of(name)
        if not ver:
            continue
        try:
            d = datetime.strptime(ver, "%Y%m%d").date()
        except Exception:
            continue
        if lo <= d <= hi:
            picked.append((d, name))

    picked.sort()
    return [name for _, name in picked]


def anchors(today: Optional[date] = None, refresh: bool = False) -> Dict[str, Anchor]:
    """锚点表：key → Anchor。带磁盘缓存，一小时内不重拉。"""
    if not refresh:
        rows = _load_anchor_cache()
        if rows:
            out: Dict[str, Anchor] = {}
            for r in rows:
                try:
                    a = Anchor(
                        key=r["key"], title=r.get("title") or "",
                        kind=r.get("kind") or "demand",
                        story_ids=tuple(int(x) for x in r.get("story_ids") or []),
                        version=r.get("version") or "",
                        names=tuple(r.get("names") or []),
                        stage=r.get("stage") or "",
                        owner=r.get("owner") or "",
                        business=r.get("business") or "",
                    )
                except Exception:
                    continue
                out[a.key] = a
            if out:
                return out

    # 冷启动：拉近期迭代下的 story
    collected: List[Anchor] = []
    try:
        for it_name in _iterations_in_play(today):
            for s in src.stories_of([it_name]):
                collected.append(_anchor_of_story(s))
    except Exception as e:
        logger.warning("拉 story 建锚点失败: %s", e)

    # 同一版本下多于一条 story 时，额外建一个版本级容器
    by_ver: Dict[str, List[Anchor]] = {}
    for a in collected:
        if a.version:
            by_ver.setdefault(a.version, []).append(a)

    table: Dict[str, Anchor] = {a.key: a for a in collected}
    for ver, group in by_ver.items():
        if len(group) > 1:
            table[f"ver:{ver}"] = Anchor(
                key=f"ver:{ver}",
                title=f"{ver[4:6]}月{ver[6:8]}日版本（{len(group)} 条需求）",
                kind="version",
                story_ids=tuple(a.story_ids[0] for a in group),
                version=ver,
                names=tuple(n for a in group for n in a.names),
                business=group[0].business,
            )

    if table:
        _save_anchor_cache(list(table.values()))
    return table


# ══════════════════════════════════════════════════════════════════════
# 认领
# ══════════════════════════════════════════════════════════════════════

# 需求名命中：连续 6 个汉字算强信号。少于 6 字容易撞（「优化」「改造」）。
_MIN_NAME_RUN = 6
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{6,}")


def _name_hits(text: str, names: Iterable[str]) -> Optional[str]:
    """需求名连续命中。返回命中的那段名字，没命中返回 None。

    从需求名里切出每一段连续中文再比，而不是拿整个名字比 ——
    名字通常长这样：【私募数据审核平台】优化需求V1.0，整串永远不可能出现在聊天里，
    但「私募数据审核平台」会。
    """
    runs = _CJK_RUN.findall(text or "")
    if not runs:
        return None
    best: Optional[str] = None
    for name in names:
        for core in _CJK_RUN.findall(name):
            if len(core) < _MIN_NAME_RUN:
                continue
            for run in runs:
                if core in run or (len(run) >= _MIN_NAME_RUN and run in core):
                    if best is None or len(core) > len(best):
                        best = core
    return best


def claim(text: str, table: Dict[str, Anchor]) -> Optional[str]:
    """一条文本属于哪件事。认不出返回 None —— 不硬塞。

    优先级：story_id > 版本号 > 需求名。
    版本号优先于需求名，因为版本是硬标识，名字会有别名。
    """
    if not text:
        return None

    # 1) story_id 直接出现（7 位以上数字，避免和工时、金额撞）
    for m in re.finditer(r"(?<!\d)(\d{7,12})(?!\d)", text):
        key = f"story:{int(m.group(1))}"
        if key in table:
            return key

    # 2) 版本号。一条消息可能同时提到多个版本（「v3.4.0（11.12）已完成，
    #    1126 启动」），挂**文本中最先出现的那个** —— 人说话通常先说主线。
    hits: List[Tuple[int, str]] = []
    for key, a in table.items():
        if not a.version:
            continue
        for alias in aliases_of(a.version):
            if len(alias) < 4:
                continue
            pos = text.find(alias)
            if pos < 0:
                continue
            ver_key = f"ver:{a.version}"
            hits.append((pos, ver_key if ver_key in table else key))
    if hits:
        hits.sort()
        return hits[0][1]

    # 3) 需求名
    best: Optional[tuple[int, str]] = None
    for key, a in table.items():
        if a.kind != "demand":
            continue
        hit = _name_hits(text, a.names)
        if hit and (best is None or len(hit) > best[0]):
            best = (len(hit), key)
    return best[1] if best else None


# ══════════════════════════════════════════════════════════════════════
# 事件日志
# ══════════════════════════════════════════════════════════════════════
#
# 一行一个事件。所有字段都是可选的，读的时候不看缺什么，只看有什么。
#
#   id     幂等键 —— 同一条消息灌两次只留一条
#   k      事 key
#   at     epoch ms
#   kind   msg | todo | mark | note
#   src    来源（群名 / 会议名 / story / 日计划）
#   who    谁说的
#   txt    内容
#   state  todo 专用：open / done

def _read_log() -> List[dict]:
    out: List[dict] = []
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读事件日志失败: %s", e)
    return out


def append(records: Iterable[dict]) -> int:
    """把事件灌进日志，同 id 以新记录为准（upsert）。返回变化的条数。

    为什么不是纯 append
    ------------------
    这里存的不是「不可变事件」，是「一条消息当前的状态」。replied 会从 False
    翻成 True：感知层 30 秒重扫一次，他回了之后重扫就能算出来。如果按 id
    直接跳过，日志永远停在 False —— 09:00 的待办就会一直催他去回早就回过的
    消息，正是他抱怨的那件事。

    重写整个文件而不是原地改：日志在 KEEP_DAYS 内是几 MB 级，
    一次写几十毫秒，换来状态永远是最新的，值。
    """
    rows = _read_log()
    index = {r.get("id"): i for i, r in enumerate(rows) if r.get("id")}
    changed = 0
    for r in records:
        rid = r.get("id")
        if not rid:
            continue
        i = index.get(rid)
        if i is None:
            index[rid] = len(rows)
            rows.append(r)
            changed += 1
        elif rows[i] != r:
            rows[i] = r
            changed += 1

    if not changed:
        return 0

    try:
        LOG_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("写事件日志失败: %s", e)
        return 0
    return changed


def _prune(max_days: int = KEEP_DAYS) -> int:
    """裁掉太老的记录。日志只增不减会拖慢每次 fold。"""
    cutoff = (time.time() - max_days * 86400) * 1000
    rows = _read_log()
    keep = [r for r in rows if float(r.get("at") or 0) >= cutoff]
    if len(keep) == len(rows):
        return 0
    try:
        LOG_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("裁剪日志失败: %s", e)
        return 0
    return len(rows) - len(keep)


# ══════════════════════════════════════════════════════════════════════
# 灌入（各来源 → 日志）
# ══════════════════════════════════════════════════════════════════════

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("读 %s 失败: %s", path.name, e)
        return None


# 他自己的 open_id。他发的消息同样会被感知线程抓到（外发走 user 身份），
# 那些消息是「他说过什么」的证据，必须留在时间线里，但绝不能算成「别人在等你」。
# 值来自 `[projects.pm.identity]`：写死的话换人之后所有「我自己发的」都会被判成
# 「别人在等你」—— 判定不会报错，只是全部反向。
_ME_OPEN_ID = pm_config().identity.open_id


def _directed_of(row: dict) -> bool:
    """这条消息是不是冲他来的。口径只在 pm_watch 定义。

    传整行而不是 reason 字符串：快照行里已经有 directed 字段，
    优先读字段，缺字段（老快照）才回退文案 —— 回退只有 pm_watch 那一份。

    懒加载：pm_watch 拖着 lark-cli 那一串依赖，
    为了一个布尔判定在 import 期付这个代价不值得。
    """
    from app.domains.pm import pm_watch
    return pm_watch.directed_of(row)


def _event_directed(e: dict) -> bool:
    """事件级的「冲他来」判定，带兜底。

    过审条目自带 directed，感知快照组装的也有（pm_watch 采集时落的）。
    只用 meta 的 verdict 的话，watch 来源的事件全部判成「知道一下」，
    他明明在被人等，清单上却什么都没有（2026-11-04 实测 172 条里 169 条如此）。
    """
    m = e.get("meta") or {}
    if m.get("verdict") == "action":
        return True
    if "directed" in m:
        return bool(m.get("directed"))
    return _directed_of(m)


def _person_name(name: str, sender_id: str) -> str:
    """人名兜底。

    快照里有一批 sender_name 是截断的 open_id（ou_bd09077）。
    一屏清单里出现三行 ou_xxx，他第一反应就是「这什么烂东西」——
    查不到就退回「某人」，至少读得下去。
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


def _from_watch(table: Dict[str, Anchor]) -> List[dict]:
    """感知事件的认领。"""
    snap = _read_json(SNAPSHOT_PATH) or {}
    out = []
    for ev in snap.get("events") or []:
        text = ev.get("text") or ""
        k = claim(text, table) or claim(f"{ev.get('chat_name') or ''} {text}", table)
        k = k or f"adhoc:{ev.get('chat_id') or 'unknown'}"
        reason = ev.get("reason") or ""
        sid = ev.get("sender_id") or ""
        if sid == _ME_OPEN_ID:
            who = "我"
        elif ev.get("chat_mode") == "p2p":
            # 私聊的会话名就是对方名字，比 sender_name 可靠 —— 后者常是截断 id。
            who = _person_name(ev.get("chat_name") or "", sid)
        else:
            who = _person_name(ev.get("sender_name") or "", sid)
        out.append({
            "id": f"msg:{ev.get('message_id')}",
            "k": k,
            "at": float(ev.get("ts") or 0),
            "kind": "msg",
            "src": ("群 " if ev.get("chat_mode") == "group" else "") + (ev.get("chat_name") or ""),
            "who": who,
            "txt": text,
            "meta": {
                "mine": sid == _ME_OPEN_ID,
                "replied": bool(ev.get("replied")),
                "reason": reason,
                # 判「该不该他出手」靠这个字段。不写进来，下游只能看到 None，
                # 整批消息就会静默掉进「知道一下」。
                "directed": _directed_of(ev),
                "chat_id": ev.get("chat_id") or "",
                "context": ev.get("context") or "",
            },
        })
    return out


def _day_start_ms(day_str: str) -> float:
    """把 YYYY-MM-DD 转成当天 0 点的时间戳。转不出来返回 0。"""
    try:
        d = datetime.fromisoformat(day_str)
    except Exception:
        return 0.0
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000


def _from_triage(table: Dict[str, Anchor], truth: Dict[str, dict]) -> List[dict]:
    """过审条目的认领。会议纪要里的行动项是最值钱的一类。

    truth 是同一批消息在感知层的当前状态。为什么不用过审条目自带的 replied：
    .triage_state.json 只在 18:00 那轮重算，白天感知层把 replied 纠正成 True 了，
    过审快照还是旧的 False。谁更晚算的听谁的 —— 感知层一直在跑，以它为准。

    同一条消息两边都有时**不新开一条事**：感知层带真实时间和人名，
    过审带 verdict/need 判定，合到同一个 id 上。不合的话，一句话会变成
    两个「事」，清单上重复出现，他只会觉得这机器人在凑数。
    """
    state = _read_json(TRIAGE_STATE_PATH) or {}
    # 会议条目的 ts 在 triage 里就是 0（排序哨兵，让行动项排在当天最前）。
    # 这里不能拿「现在」兜底 —— 每灌一次就刷成最新，一条白天的会议纪要
    # 会一直抢走「这件事最新进展」的位置。用清单日期当天 0 点：
    # 落在当天的窗口里，且不会随时间漂移。
    fallback_at = _day_start_ms(str(state.get("date") or ""))
    out = []
    for it in state.get("items") or []:
        mid = it.get("message_id")
        ref = truth.get(f"msg:{mid}") if mid else None
        replied = bool(ref["meta"]["replied"]) if ref else bool(it.get("replied"))

        if ref:
            merged = dict(ref)
            merged["meta"] = {
                **ref.get("meta", {}),
                "verdict": it.get("verdict") or "",
                "need": it.get("need") or "",
                "directed": bool(it.get("directed")) or _event_directed(ref),
                "replied": replied,
                "reason": it.get("reason") or ref.get("meta", {}).get("reason", ""),
            }
            out.append(merged)
            continue

        text = it.get("summary") or it.get("text") or ""
        k = claim(f"{it.get('chat_name') or ''} {text}", table)
        k = k or f"adhoc:{(it.get('chat_name') or 'misc')}"
        out.append({
            "id": f"tri:{mid}",
            "k": k,
            "at": float(it.get("ts") or 0) or fallback_at,
            "kind": "msg",
            "src": it.get("chat_name") or "",
            "who": it.get("sender") or "",
            "txt": text,
            "meta": {
                "verdict": it.get("verdict") or "",
                "need": it.get("need") or "",
                "directed": bool(it.get("directed")),
                "replied": replied,
                "reason": it.get("reason") or "",
            },
        })
    return out


def _from_todos(table: Dict[str, Anchor]) -> List[dict]:
    """他日计划里的待办。todo 能直接说明「这件事现在要他动手」。"""
    rows = _read_json(_PROJECT_DIR / ".triage_todos.json")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = r.get("title") or r.get("text") or ""
        k = claim(title, table) or "adhoc:todo"
        out.append({
            "id": f"todo:{r.get('id') or r.get('record_id') or title[:24]}",
            "k": k,
            "at": float(r.get("at") or 0) or time.time() * 1000,
            "kind": "todo",
            "src": "日计划",
            "who": "我",
            "txt": title,
            "state": "done" if r.get("done") else "open",
        })
    return out


def _purge_shadowed_triage() -> int:
    """清掉被感知事件盖住的过审条目。

    同一条消息以前会存两份：msg:xxx（感知层）和 tri:xxx（过审）。
    两份的锚点可能算得不一样，于是一句话在清单上变成两个「事」。
    现在过审条目合并进感知事件，但历史日志里的孤儿要清一次 ——
    不清理的话，它每天都会原样再出现一遍。
    """
    rows = _read_log()
    msg_ids = {r.get("id") for r in rows if str(r.get("id") or "").startswith("msg:")}
    keep = [
        r for r in rows
        if not (
            str(r.get("id") or "").startswith("tri:")
            and f"msg:{str(r.get('id'))[4:]}" in msg_ids
        )
    ]
    n = len(rows) - len(keep)
    if not n:
        return 0
    try:
        LOG_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
            encoding="utf-8",
        )
        logger.info("清理被感知事件盖住的过审条目 %d 条", n)
    except Exception as e:
        logger.error("清理过审条目失败: %s", e)
        return 0
    return n


def ingest(today: Optional[date] = None, prune: bool = False) -> int:
    """把各路消息灌进事件日志。幂等 —— 反复调不会重复。"""
    table = anchors(today)
    if not table:
        logger.warning("锚点表为空，本次只灌 adhoc")
    records = _from_watch(table)
    truth = {r["id"]: r for r in records}
    records += _from_triage(table, truth) + _from_todos(table)
    n = append(records)
    _purge_shadowed_triage()
    if prune:
        _prune()
    logger.info("事件日志新增 %d 条（锚点 %d 个）", n, len(table))
    return n


# ══════════════════════════════════════════════════════════════════════
# 视图
# ══════════════════════════════════════════════════════════════════════

# 一条事要你做什么，只有两种：
#   办 —— 有具体动作要落地（会议行动项、排期要决策、日计划还挂着）
#   回 —— 有人问了你一句话，等你答（私聊、群里点名）
#
# 为什么合成一档：他原话「其实我基本都回了，你只需要给我待办就行」。
# 把「没回的消息」单独列一块，对他来说不是待办，是未读列表 ——
# 他不会因为机器人提醒就去回，他本来就会回。真正会漏的是「该办没办」。
# 但私聊里那种等答复的，确实得算待办，所以合并成「办/回」两种标记。
_NEED, _KNOW, _DONE = "need_you", "know", "done"

_ACT_DO, _ACT_REPLY = "办", "回"
# 「跟」是第三类，也是最容易漏的一类：他对别人说过要给回话，但没给。
# 别人问的他基本都会回（这是他的习惯），他自己一句「我回头同步给你」
# 才是真的会飘掉的东西 —— 说出口就当办完了。
_ACT_FOLLOW = "跟"

# ── 处置：他对一件事表过态之后 ────────────────────────────────────
#
# 上面的 verdict / act 都是每次 _build() 现算的派生值，判完就忘。
# 于是「上周他说过别管」系统不知道 —— 第二天重新判成 need_you 再提醒一次。
# 他感受到的不是「判据不准」，是「它记不住我说过什么」。
#
# 处置不做状态表，做成一条事件，跟消息走同一个日志。理由和模块开头一样：
# 只追加、视图每次 fold。他改主意了（ignore → reopen）也有历史可查，
# 而且不用维护「状态表 + 日志」两套存储的一致性。
SETTLE_DONE = "done"        # 办完了
SETTLE_IGNORE = "ignore"    # 别管了，这事不重要
SETTLE_SNOOZE = "snooze"    # 先放放，到 X 再说
SETTLE_REOPEN = "reopen"    # 重新捡起来（撤销前面的处置）
_SETTLE_ACTIONS = (SETTLE_DONE, SETTLE_IGNORE, SETTLE_SNOOZE, SETTLE_REOPEN)

# 说「先放放」但没给日期时，默认放几天。
# 3 天是权衡：太短等于没放，太长等于丢了。值来自配置，换人不必改代码。
SNOOZE_DEFAULT_DAYS = pm_config().rules.snooze_days

# 渲染用的人话。给推送和时间线共用，避免两处各写一套说法。
_SETTLE_LABEL = {
    SETTLE_DONE: "办完了",
    SETTLE_IGNORE: "别管了",
    SETTLE_SNOOZE: "先放放",
    SETTLE_REOPEN: "重新捡起来",
}

# 我说过要给回话的说法。判据是「承诺」而不是「提到」，所以只认
# 明确带交付意图的组合，不认「说下」「看一下」这类随口的话。
_PROMISE_CUES = (
    "同步给你", "同步给", "给你同步", "有信儿给你", "有信儿了", "给你信",
    "跟你说", "跟他说", "反馈给", "反馈下", "反馈一下",
)

# 承诺已兑现的说法。出现即视为这条了了，不用他手动划掉。
# 「已同步 / 反馈了 / 说了」这一小撮词是判断「这事有没有下文」的最小信号集。
_PROMISE_DONE = ("已同步", "已经同步", "同步了", "已反馈", "反馈了", "已发", "已经发", "说过了", "搞定")


def _promise_open(evs: Sequence[dict]) -> Optional[dict]:
    """从我说过的话里，找出「答应了、但还没给信儿」的那一句。

    只看最早一处未兑现的承诺：他一天里可能答应五件事，逐条列出来没人看；
    而欠得最久的那条最危险 —— 「反馈给燕姐」比「回头同步年化」更该先想起来。

    兑现判定用「此后我有没有说过已同步/反馈了」——
    这是他自己会自然说出口的话，不需要额外操作，也不会误判。
    """
    mine = [
        e for e in sorted(evs, key=lambda x: float(x.get("at") or 0))
        if (e.get("meta") or {}).get("mine")
    ]
    if not mine:
        return None

    for i in range(len(mine)):
        txt = mine[i].get("txt") or ""
        if not any(c in txt for c in _PROMISE_CUES):
            continue
        after = " ".join((x.get("txt") or "") for x in mine[i + 1:])
        if any(d in after for d in _PROMISE_DONE):
            continue
        return mine[i]
    return None

# 会议纪要里的行动项是最硬的一类：白纸黑字写了谁做什么。
# 只认 reason，不认 src —— src 是「会议 · 公募1126版本需求评审」，含「会议」二字，
# 用它判定会把这个会里所有消息都当成行动项。
_MEETING_HINT = ("会议行动项", "会议行动（别人做）")


@dataclass
class Thread:
    """一条事摊开后的样子。

    除了 settled / settled_at / snooze_until，其余字段都是每次 fold 出来的。
    那三个是**他表过态**的证据 —— 系统判断可以每天重算，他的态度不行。
    """

    key: str
    title: str
    kind: str
    stage: str = ""
    owner: str = ""
    business: str = ""
    verdict: str = _KNOW
    act: str = ""                      # 办 / 回 / 跟 —— 这件事要他怎么动
    why: str = ""                      # 判成 need_you 的理由，一句话
    promise: Optional[dict] = None     # 判成「跟」时，他说过的那句承诺本身
    events: List[dict] = field(default_factory=list)
    open_todos: List[dict] = field(default_factory=list)
    # 他对这件事的最后处置。空 = 没表过态，按系统判断走。
    settled: str = ""
    settled_at: float = 0.0
    snooze_until: float = 0.0

    @property
    def content_events(self) -> List[dict]:
        """事件本身，不含处置。

        处置是他对这件事的操作痕迹，不是这件事发生过什么。
        混进 last_at / 标题 / 摘要里，会让「上一条消息说了什么」变成
        「他昨天点了别管了」—— 那是元数据，不是内容。
        """
        return [e for e in self.events if e.get("kind") != "settle"]

    @property
    def last_at(self) -> float:
        return max((float(e.get("at") or 0) for e in self.content_events), default=0.0)

    @property
    def is_demand(self) -> bool:
        return self.kind in ("demand", "version")

    @property
    def last_event(self) -> Optional[dict]:
        evs = self.content_events
        return evs[-1] if evs else None


def _is_meeting_action(e: dict) -> bool:
    blob = f"{e.get('src') or ''}{((e.get('meta') or {}).get('reason') or '')}"
    return any(h in blob for h in _MEETING_HINT)


# 飞书的 @ 后面跟人名，到空格或标点为止
_AT_RE = re.compile(r"@([^\s@，,。；;：:、]{1,12})")
# 纪要自带的出处标注，对「谁负责」没有帮助
_FROM_RE = re.compile(r"[（(]来自[^）)]*[）)]")


def _action_brief(text: str) -> str:
    """把一条会议行动项压成「做什么 —— 谁」。

    原文是「规范分红相关报错：捞取B1.0…（来自王五） @钱七」这种 80 字长句。
    他要的不是这句话，是「谁负责哪件事」—— 冒号前是动作，@ 后面是人。
    """
    body = " ".join((text or "").split())
    if not body:
        return ""
    body = _FROM_RE.sub("", body).strip()

    who = ""
    m = _AT_RE.search(body)
    if m:
        who = m.group(1).strip()
        body = _AT_RE.sub("", body).strip()

    for sep in ("：", ":"):
        if sep in body:
            body = body.split(sep, 1)[0]
            break
    body = body.strip(" ，,。；;")

    if len(body) > 22:
        body = body[:22] + "…"
    return f"{body} —— {who}" if who else body


def _meeting_actions(events: Sequence[dict], mine: bool) -> List[dict]:
    """会议行动项分「归他的」和「归别人的」两堆。

    依据是采集层给的 directed，这里不再猜第二遍文本 ——
    判定只做一次，下游读同一个结论。
    """
    picked = [
        e for e in events
        if _is_meeting_action(e) and _event_directed(e) is mine
    ]
    return sorted(picked, key=lambda x: float(x.get("at") or 0))


def _briefs(events: Sequence[dict]) -> List[str]:
    out: List[str] = []
    for e in events:
        brief = _action_brief(str(e.get("txt") or ""))
        if brief and brief not in out:
            out.append(brief)
    return out


def _action_lines(events: Sequence[dict]) -> List[str]:
    """会上派给他的具体动作，一条一行。

    只说「有 3 条行动项」等于什么都没说 —— 他得再问一遍「哪 3 条」。
    而且那 3 条往往根本不是他的：纪要里一条行动项只有一个 @，@ 的是别人。
    """
    return _briefs(_meeting_actions(events, mine=True))


def _other_action_lines(events: Sequence[dict]) -> List[str]:
    """会上派给别人的动作。

    他要看（会上定了什么，比群里任何一条都密），但这不是他的活 ——
    放进「今天要办」就是拿别人的任务催他。
    """
    return _briefs(_meeting_actions(events, mine=False))


def _src_key(e: dict) -> str:
    """会话名，用来在没有 chat_id 时认人。

    过审条目（tri:）只有会话名，没有 chat_id —— 它是从快照转出来的，
    早期版本压根没存 chat_id。私聊的会话名就是人名，群里是群名，
    足够当第二把钥匙。
    """
    return (e.get("src") or "").replace("群 ", "").strip()


def _mine_at(events: Sequence[dict]) -> Dict[str, List[float]]:
    """每个会话里我说过话的时间点，按会话分组。

    「这件事我回没回」不能只信消息自己记的 replied —— 那是采集那一刻的快照，
    跨天就冻结了。他今天早上回了昨天的问题，昨天那条消息的 replied 永远是
    False，09:00 的待办就一直挂着「等你答」。

    真正的、不会过期的证据是：这条之后，我在同一个会话里发过言。
    所以采集层现在也收他自己发的话（reason="我自己发的"），这里才有得算。

    索引双份：chat_id 优先，会话名兜底。老数据没有 chat_id，
    只有会话名可用，而它恰恰是最不该漏判的那批。
    """
    out: Dict[str, List[float]] = {}
    for e in events:
        m = e.get("meta") or {}
        if not m.get("mine"):
            continue
        at = float(e.get("at") or 0)
        cid = m.get("chat_id") or ""
        if cid:
            out.setdefault(cid, []).append(at)
        name = _src_key(e)
        if name:
            out.setdefault(f"n:{name}", []).append(at)
    return out


def _answered(e: dict, mine_at: Dict[str, List[float]]) -> bool:
    """这条消息我是不是已经回过了。

    两个判据合一：采集时就判定过的 replied（同轮内更精确，能区分「回的是不是
    这一条」），以及「此后我在同会话发过言」（跨天也不失效）。
    """
    m = e.get("meta") or {}
    if m.get("replied"):
        return True

    at = float(e.get("at") or 0)
    cid = m.get("chat_id") or ""
    if cid and any(t > at for t in (mine_at.get(cid) or ())):
        return True
    name = _src_key(e)
    if name and any(t > at for t in (mine_at.get(f"n:{name}") or ())):
        return True
    return False


def _settle_of(evs: Sequence[dict]) -> Optional[dict]:
    """这件事最后一次处置。没表过态返回 None。

    取「日志里最后一条」而不是「at 最大的那条」：
    日志是 append 语义，行序就是写入序，而他改主意一定发生在后面。
    按时间戳排会在两种情况下判错 —— 补录历史消息（旧 at 后写）、
    时钟回拨（新 at 更小），那时「最后一次说别管了」会被前面的 done 盖掉。
    """
    for e in reversed(evs):
        if e.get("kind") == "settle":
            return e
    return None


def _apply_settle(s: dict, verdict: str, act: str, why: str) -> Tuple[str, str, str]:
    """他的处置盖过系统判断。返回新的 (verdict, act, why)。

    这是「跨天记得住」的落点：判据每天重算没关系，只要处置还压在头上，
    结论就不会漂回去。
    """
    action = s.get("action") or ""
    until = float(s.get("until") or 0)

    if action in (SETTLE_DONE, SETTLE_IGNORE):
        return _DONE, "", ""

    if action == SETTLE_SNOOZE:
        if until and until > time.time() * 1000:
            return _DONE, "", ""
        # 到点了，重新捡起来。必须说清是「你之前放的那件」——
        # 否则它会像新冒出来的一样，他根本想不起自己放过什么。
        left = datetime.fromtimestamp(until / 1000).strftime("%m-%d") if until else ""
        head = f"你放到 {left} 的，到期了" if left else "推迟到期"
        return _NEED, act or _ACT_DO, f"{head}｜{why}" if why else head

    # reopen：撤销处置，回到自动判断
    return verdict, act, why


def _verdict_of(evs: Sequence[dict], todos: Sequence[dict],
                mine_at: Dict[str, List[float]]) -> Tuple[str, str, str]:
    """一条事要你做什么。返回 (verdict, act, why)。

    顺序即优先级：日计划待办 > 会议行动项 > 别人问了没回 > 知道。

    理由必须具体到「谁在等什么」，不能所有条目都写同一句话 —— 那样等于没写。
    「我」发的消息一律不算欠别人：他外发走 user 身份，自己说的话也会被
    感知线程捞回来，那不是待回的消息，是他承诺过的证据。
    """
    open_todos = [t for t in todos if t.get("state") == "open"]
    if open_todos:
        return _NEED, _ACT_DO, f"日计划还挂着：{(open_todos[0].get('txt') or '')[:30]}"

    ordered = sorted(evs, key=lambda x: float(x.get("at") or 0), reverse=True)

    # 会议行动项单独计数：一次评审会可能派给你 3 件事，
    # 说「有 3 条」比说「有人在会议里给你派了活」有用得多。
    actions = [
        e for e in ordered
        if e.get("kind") == "msg" and _is_meeting_action(e)
        and _event_directed(e)
        and not _answered(e, mine_at)
    ]
    if actions:
        # 只有真 @ 到他的行动项能走到这里。别人的活不算他的待办 ——
        # 那批由 _other_action_lines 在「知道一下」里交代，不占这一档。
        n = len(_briefs(actions))
        return _NEED, _ACT_DO, f"会上派给你 {n} 件事"

    # 他自己答应过、还没兑现的一句。
    # 陈雨帆那段对话里，对方的提问早就被他回了，真正没落地的是
    # 「我一会给燕姐说下，有信儿给你同步」—— 别人问的他会回，
    # 自己答应的才容易忘。这一档补的就是这个缺口。
    if _promise_open(evs):
        return _NEED, _ACT_FOLLOW, "你答应给信儿，还没给"

    for e in ordered:
        m = e.get("meta") or {}
        if e.get("kind") != "msg" or m.get("mine") or _answered(e, mine_at):
            continue
        if not _event_directed(e):
            continue
        who = (e.get("who") or "有人").split(" ")[0][:10]
        if _event_directed(e) and not m.get("verdict") == "action":
            return _NEED, _ACT_REPLY, f"{who} 问了，等你答"
        return _NEED, _ACT_DO, m.get("reason") or f"{who} 需要你动手"

    # 剩下的是「我说过话、没人再等我」或「只是通报」。
    # 前者是已经了结的事，后者才是「知道一下」—— 两者的区别在于
    # 这条事还有没有活的方向。全归 KNOW 的话，「知道一下」会被
    # 一堆我自己的「好的」「收到」撑爆，正事反而被淹。
    msgs = [e for e in ordered if e.get("kind") == "msg"]
    if msgs and all((e.get("meta") or {}).get("mine") for e in msgs):
        return _DONE, "", ""

    return _KNOW, "", ""


def _adhoc_title(evs: Sequence[dict]) -> str:
    """锚不到任何需求的「事」。

    按来源命名 —— 私聊认人，群聊认群。这类事不该硬塞给某条需求，
    但也不该丢掉：私聊没回同样是欠着别人。
    """
    for e in sorted(evs, key=lambda x: float(x.get("at") or 0), reverse=True):
        # 处置事件不是内容，不带来源 —— 用它命名会得到「私聊 · 用户」。
        if e.get("kind") == "settle":
            continue
        src = (e.get("src") or "").strip()
        if not src:
            continue
        if src.startswith("群") or src.startswith("会议"):
            return src
        return f"私聊 · {src}"
    return "杂项"


def _build(anchors_tbl: Dict[str, Anchor], events: List[dict],
           mine_at: Optional[Dict[str, List[float]]] = None) -> List[Thread]:
    """把事件 fold 成事。没有锚的 adhoc 也建，但排在后面。"""
    mine_at = mine_at or _mine_at(events)
    grouped: Dict[str, List[dict]] = {}
    for e in events:
        grouped.setdefault(e.get("k") or "adhoc:misc", []).append(e)

    out: List[Thread] = []
    for key, evs in grouped.items():
        a = anchors_tbl.get(key)
        todos = [e for e in evs if e.get("kind") == "todo"]
        verdict, act, why = _verdict_of(evs, todos, mine_at)

        # 他表过态的事以他为准。系统判得再准，也不该盖过一句「这件别管了」——
        # 反复提醒同一件事，缺的就是这一层。
        s = _settle_of(evs)
        if s:
            verdict, act, why = _apply_settle(s, verdict, act, why)

        out.append(Thread(
            key=key,
            title=a.title if a else _adhoc_title(evs),
            kind=a.kind if a else "adhoc",
            stage=a.stage if a else "",
            owner=a.owner if a else "",
            business=a.business if a else "",
            verdict=verdict,
            act=act,
            why=why,
            # 「跟」的证据是他自己那句承诺，不是最后一条消息 ——
            # 承诺之后对方往往会再补两句，那些话不是他要办的事。
            promise=_promise_open(evs) if act == _ACT_FOLLOW else None,
            events=sorted(evs, key=lambda x: float(x.get("at") or 0)),
            open_todos=[t for t in todos if t.get("state") == "open"],
            settled=(s.get("action") or "") if s else "",
            settled_at=float(s.get("at") or 0) if s else 0.0,
            snooze_until=float(s.get("until") or 0) if s else 0.0,
        ))

    # 待办里「办」排最前，其次「跟」（他答应过的，只有他自己记得），
    # 最后「回」（他基本都会回，不需要排在前面提醒）。
    act_order = {_ACT_DO: 0, _ACT_FOLLOW: 1, _ACT_REPLY: 2, "": 3}
    order = {_NEED: 0, _KNOW: 1, _DONE: 2}
    out.sort(key=lambda t: (order.get(t.verdict, 9), act_order.get(t.act, 3), -t.last_at))
    return out


def threads_of(day: Optional[date] = None, days_back: int = 1) -> List[Thread]:
    """这几天活着的「事」，按「需要你出手」排序。

    days_back=1 表示「今天和昨天」—— 昨晚的消息今早还得能看到。
    """
    day = day or date.today()
    since = (datetime.combine(day, datetime.min.time()) - timedelta(days=days_back)).timestamp() * 1000
    table = anchors(day)
    rows = _read_log()
    # 我说过话的时间线要看得更远：他可能三天后才回一条。
    # 只按 days_back 取，就会把「早就回过的」判成「还在等你」。
    mine_since = (datetime.combine(day, datetime.min.time()) - timedelta(days=14)).timestamp() * 1000
    mine_at = _mine_at([e for e in rows if float(e.get("at") or 0) >= mine_since])

    # 处置不受时间窗口限制：上周说「这件事别管了」，这周它又来了条消息，
    # 仍然不该冒出来。按谓词合并（而不是把 settle 追加到末尾），
    # 才能保住行序 —— _settle_of 靠行序判断哪次处置是最后一次。
    events = [
        e for e in rows
        if e.get("kind") == "settle" or float(e.get("at") or 0) >= since
    ]
    return _build(table, events, mine_at)


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

def _hhmm(ms: float) -> str:
    if not ms:
        return "--:--"
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _md(ms: float) -> str:
    if not ms:
        return ""
    d = datetime.fromtimestamp(ms / 1000)
    return d.strftime("%m-%d")


def _one_line(text: str, limit: int = 46) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _nodes_lines(t: Thread) -> List[str]:
    """从飞书项目拉这条需求的节点排期，补进时间线。

    聊天里没人提这条需求，不代表它没在跑。节点排期是它在系统里的心跳 ——
    少了这一段，「问一句答来龙去脉」就只答得出半程。
    """
    if not t.key.startswith("story:"):
        return []
    try:
        from app.domains.pm import pm_schedule_risk as risk
        plan = risk.plan_of(int(t.key.split(":", 1)[1]))
    except Exception as e:
        logger.debug("拉节点失败 %s: %s", t.key, e)
        return []

    lines = ["", "节点："]
    for n in plan.nodes:
        if not (n.scheduled or n.current):
            continue
        mark = "▶" if n.current else ("✓" if n.passed else "○")
        span = f"{risk.fmt_day(n.start)}→{risk.fmt_day(n.end)}" if n.scheduled else "未排期"
        who = f"　{len(n.people)} 人" if n.people else ""
        lines.append(f"· {mark} {n.name}　{span}{who}")
    return lines


def timeline(t: Thread, limit: int = 12) -> str:
    """一条事的时间线。这是「问一句，答来龙去脉」的底座。

    顺序：状态 → 卡在谁 → 节点排期（系统事实）→ 消息（人的事实）。
    系统事实在前，因为它比聊天可靠；聊天补的是「为什么」。
    """
    lines = [f"{t.title}　{t.stage or t.kind}"]
    if t.owner:
        lines.append(f"卡在：{t.owner}")

    lines.extend(_nodes_lines(t))

    evs = t.events[-limit:]
    if not evs:
        lines.append("")
        lines.append("（还没有群聊/会议记录）")
        return "\n".join(lines)

    lines.append("")
    for e in evs:
        kind = e.get("kind")
        who = e.get("who") or "?"
        thumb = f"{_md(float(e.get('at') or 0))} {_hhmm(float(e.get('at') or 0))}　{who}"
        if kind == "msg":
            lines.append(f"· {thumb}｜{_one_line(e.get('txt') or '')}")
        elif kind == "todo":
            mark = "×" if e.get("state") == "done" else "○"
            lines.append(f"· {mark} 待办｜{_one_line(e.get('txt') or '')}")
        elif kind == "mark":
            lines.append(f"· 节点｜{_one_line(e.get('txt') or '')}")
        elif kind == "settle":
            # 处置是他自己的动作，用「你：」起头 —— 混在别人说的话里
            # 会让人以为这也是某条消息。
            act = _SETTLE_LABEL.get(e.get("action") or "", e.get("action") or "")
            until = float(e.get("until") or 0)
            tail = (
                f"（放到 {datetime.fromtimestamp(until / 1000):%m-%d}）"
                if e.get("action") == SETTLE_SNOOZE and until else ""
            )
            lines.append(f"· {_md(float(e.get('at') or 0))}　你：{act}{tail}")
        else:
            lines.append(f"· {kind}｜{_one_line(e.get('txt') or '')}")
    return "\n".join(lines)


def _ctx_tail(e: dict, limit: int = 34) -> str:
    """短消息补前文。

    「东方证券？」这种单句，脱离上文完全看不懂。感知层已经把前几条消息存进
    context 了（"对方：什么单 / 对方：过期期权提醒的？"），这里取最后一句接上。
    """
    txt = (e.get("txt") or "").strip()
    if len(txt) >= 14:
        return _one_line(txt, limit)
    ctx = ((e.get("meta") or {}).get("context") or "").strip()
    if not ctx:
        return _one_line(txt, limit)
    tail = [p.strip() for p in ctx.split("/") if p.strip()][-2:]
    if not tail:
        return _one_line(txt, limit)
    return _one_line(" → ".join(tail + [txt]), limit + 12)


def _thread_tail(t: Thread, limit: int = 40) -> str:
    """取这条事最近的一两句话。

    最新一条常常是「东方证券？」这种半句，单看根本没法回。往前再取一条拼上，
    变成「这些信息也带去吗 / 开了代理？ → 东方证券？」，他才一眼知道在说什么。
    这比依赖 context 字段可靠 —— 会议纪要和部分私聊根本没有 context。
    """
    msgs = [e for e in t.events if e.get("kind") == "msg" and (e.get("txt") or "").strip()]
    if not msgs:
        return ""
    # 要显示的是对方说了什么，不是我自己怎么回的。
    # 「好滴好滴 感谢！」这种回执总是最新一条，挂在待办上零信息量。
    others = [e for e in msgs if not (e.get("meta") or {}).get("mine")]
    cand = others or msgs
    last = cand[-1]
    txt = (last.get("txt") or "").strip()
    if len(txt) >= 14 or len(cand) < 2:
        return _ctx_tail(last, limit)
    prev = (cand[-2].get("txt") or "").strip()
    if not prev:
        return _one_line(txt, limit)
    return _one_line(f"{_one_line(prev, 20)} → {txt}", limit + 14)


def _need_title(t: Thread) -> str:
    """待办条目的标题。

    「回」的条目主体是人（他要回谁），「办」的条目主体是事。
    adhoc 的 title 是「私聊 · 叶建林」，回话场景里前面那个「私聊 ·」是废话。
    """
    title = t.title
    if t.act in (_ACT_REPLY, _ACT_FOLLOW):
        return title.replace("私聊 · ", "").replace("群 ", "")
    return title


def _promise_tail(t: Thread, limit: int = 52) -> str:
    """「跟」条目的正文：他说过的那句承诺，前面接一句对方的话。

    「okok 年化的 反正后面有信儿了 我跟你同步」单看根本不知道在说哪件事。
    往前接一句陈雨帆的「年化没招」，才认得出来。
    """
    p = t.promise
    if not p:
        return _thread_tail(t, limit)
    txt = (p.get("txt") or "").strip()
    at = float(p.get("at") or 0)
    before = [
        e for e in t.events
        if e.get("kind") == "msg" and float(e.get("at") or 0) < at
        and not (e.get("meta") or {}).get("mine") and (e.get("txt") or "").strip()
    ]
    if before:
        prev = (before[-1].get("txt") or "").strip()
        return _one_line(f"{_one_line(prev, 18)} → {txt}", limit)
    return _one_line(txt, limit)


def queue(day: Optional[date] = None, days_back: int = 1, include_follow: bool = True) -> str:
    """决策队列 —— 09:00 的输出形态。

    只有两档：今天要办 / 知道一下。

    为什么不再分「需要出手」和「别人在等你」：他原话「其实我基本都回了，
    你只需要给我待办就行」。把没回的消息单独列一块，那是未读列表，不是待办 ——
    他不会因为机器人提醒就去回，他本来就会回。所以合并成一档，
    用【办】【回】标出这件事要他做哪种动作。

    「知道一下」压到 3 条 + 一个数字：知道的部分不需要展开，
    展开只会把待办淹掉。
    """
    ts = threads_of(day, days_back)
    if not ts:
        return ""

    # 「跟」单独一档。他原话：「列出 只是为了提醒我」——
    # 所以照列，但不放在「今天要办」里，否则每条都要他今天动手，
    # 一天下来清单就被"其实不急"的事占满了。
    #
    # include_follow=False 用在 09:00 的推送：那一档由备忘池（写进工作簿
    # 「未开始事项」）单独负责，两边都讲同一批事只是重复噪音。
    need = [t for t in ts if t.verdict == _NEED and t.act != _ACT_FOLLOW]
    follow = [t for t in ts if t.verdict == _NEED and t.act == _ACT_FOLLOW]
    if not include_follow:
        follow = []
    know = [t for t in ts if t.verdict == _KNOW]
    quiet = [t for t in ts if t.verdict == _DONE]

    lines: List[str] = []

    if need:
        lines.append(f"今天要办（{len(need)}）")
        for i, t in enumerate(need[:8], 1):
            lines.append(f"{i}.【{t.act}】{_need_title(t)}")
            acts = _action_lines(t.events)
            if acts:
                # 先说「几件事」，再逐条列「哪几件」。
                # 只说数量他下一句一定问「哪几条」；只列条目又不知道一共多少。
                if t.why:
                    lines.append(f"   {t.why}：")
                for a in acts[:6]:
                    lines.append(f"   · {a}")
                if len(acts) > 6:
                    lines.append(f"   …另有 {len(acts) - 6} 件")
            elif t.why:
                lines.append(f"   {t.why}")
            if t.owner and t.kind in ("demand", "version"):
                lines.append(f"   卡在 {t.owner}")
            last = t.last_event
            if not acts and last and (last.get("txt") or ""):
                stamp = f"{_md(float(last.get('at') or 0))} {_hhmm(float(last.get('at') or 0))}"
                lines.append(f"   {stamp}｜{_thread_tail(t, 40)}")
        if len(need) > 8:
            lines.append(f"   …还有 {len(need) - 8} 件")
    else:
        lines.append("今天要办：没有")

    if follow:
        lines.append("")
        lines.append(f"记着的（{len(follow)}）· 不急，进工作簿「未开始事项」")
        for t in follow[:5]:
            stamp = ""
            if t.promise:
                at = float(t.promise.get("at") or 0)
                stamp = f"{_md(at)} {_hhmm(at)}｜"
            lines.append(f"· 【跟】{_need_title(t)}")
            if t.why:
                lines.append(f"   {t.why}")
            if t.promise:
                lines.append(f"   {stamp}{_promise_tail(t, 40)}")
        if len(follow) > 5:
            lines.append(f"   …还有 {len(follow) - 5} 件")

    if know:
        lines.append("")
        lines.append(f"知道一下（{len(know)}）")
        # 会议类排最前，并且必须列出会里定了什么。
        # 「会上定了 6 件事」这种话等于没说 —— 他下一句一定是「哪 6 件」。
        # 会开完就散了，群里还能翻，纪要翻不到，所以这几行值得占地方。
        named = [(t, _other_action_lines(t.events)) for t in know]
        meet = [(t, a) for t, a in named if a]
        rest = [t for t, a in named if not a]
        for t, acts in meet[:2]:
            lines.append(f"· {_one_line(t.title, 22)}（都不是你的活）")
            for a in acts[:4]:
                lines.append(f"   · {a}")
            if len(acts) > 4:
                lines.append(f"   …另有 {len(acts) - 4} 件")
        for t in rest[:3]:
            tail = _thread_tail(t, 26)
            lines.append(f"· {_one_line(t.title, 16)}" + (f"｜{tail}" if tail else ""))
        shown = len(meet[:2]) + len(rest[:3])
        if len(know) > shown:
            lines.append(f"   …还有 {len(know) - shown} 件")

    if quiet:
        # 分开说：他划掉的、他说不管的、他放着的、系统判完的。
        # 混成一句「已经处理完 N」等于告诉他别问细节 ——
        # 而他真正要看的恰恰是「我放着的那些，什么时候到期」。
        done_by_me = [t for t in quiet if t.settled == SETTLE_DONE]
        ignored = [t for t in quiet if t.settled == SETTLE_IGNORE]
        parked = [t for t in quiet if t.settled == SETTLE_SNOOZE]
        others = [t for t in quiet if not t.settled]

        bits = []
        if done_by_me:
            bits.append(f"你划掉 {len(done_by_me)}")
        if ignored:
            bits.append(f"你说不管 {len(ignored)}")
        if parked:
            bits.append(f"你放着 {len(parked)}")
        if others:
            bits.append(f"已收尾 {len(others)}")

        lines.append("")
        lines.append(f"今天不动了（{len(quiet)}）· " + "、".join(bits))

        for t in parked[:3]:
            until = (
                datetime.fromtimestamp(t.snooze_until / 1000).strftime("%m-%d")
                if t.snooze_until else "未定"
            )
            lines.append(f"· {_one_line(t.title, 18)}｜放到 {until}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 备忘池视图
# ══════════════════════════════════════════════════════════════════════

# 承诺句开头的语气词。「okok 可以的 我这个会结束也同步给燕姐反馈下」里，
# 有用的只有后半句，前面全是嗯啊这是。
_FILLER = re.compile(
    r"^(?:(?:ok+|okay|好的|可以|行|嗯|对|哈哈+|啧|收到)[\s，,。.!！~]*)+", re.I
)

# 承诺的核心：动词 + 对象。只留这一段，事项名才能像表里其它条目一样短。
_PROMISE_CORE = re.compile(
    r"(同步给|反馈给|反馈下|反馈一下|跟你说|给你同步|给你信|问一下|确认下)"
    r"([^\s，,。？?！!]{0,10})"
)


def _who(t: Thread) -> str:
    """事项名里的对象：私聊 · 陈雨帆 → 陈雨帆。"""
    return t.title.replace("私聊 · ", "").replace("群 ", "")


def _promise_task(t: Thread) -> str:
    """把承诺压成一行事项名。

    工作簿里的事项都短（「知识库设计」「需求评估工具事项」），
    所以只留「对谁做什么」，语气词和后面的追问全部丢掉。
    """
    who = _who(t)
    core = _FILLER.sub("", ((t.promise or {}).get("txt") or "").strip())
    m = _PROMISE_CORE.search(core)
    if m:
        return f"跟进{who}：{m.group(1)}{m.group(2)}"
    return f"跟进{who}：{_one_line(core, 16)}"


def backlog_items(day: Optional[date] = None, days_back: int = 1) -> List[dict]:
    """该记进工作簿「未开始事项」的事。

    只收【跟】一种，而且**收全**：只要 act 是「跟」就进，不要求它一定带
    承诺句。因为 09:00 的待办清单里已经不再展开「跟」这一档了（见 queue
    的 include_follow），这里漏一条，那条事就彻底没人提。
    """
    out: List[dict] = []
    for t in threads_of(day, days_back):
        if t.act != _ACT_FOLLOW:
            continue

        if t.promise:
            at = float(t.promise.get("at") or 0)
            # 备注只写「什么时候、答应过、没下文」三件事。不贴聊天原文 ——
            # 表是他天天看的地方，塞一段「okok 可以的 我这个会结束…」
            # 既占宽又难读，真要核对原文他回去翻会话更快。
            remark = f"{_md(at)} 聊天里答应过，至今没下文"
            task = _promise_task(t)
        else:
            remark = _one_line(t.why or "", 30)
            task = f"跟进{_who(t)}：{_one_line(t.title.replace('私聊 · ', ''), 16)}"

        out.append({
            "task": task,
            "priority": "中级",
            "owner": owner_name(),
            "remark": remark,
            "key": t.key,
        })
    return out


def resolve(query: str, day: Optional[date] = None) -> Optional[Thread]:
    """「1126」「私募」「审核平台」→ 命中的那件事。用于对话里指代。"""
    q = (query or "").strip()
    if not q:
        return None

    table = anchors(day)
    key = claim(q, table)
    ts = threads_of(day, days_back=90)
    if key:
        for t in ts:
            if t.key == key:
                return t
        a = table.get(key)
        if a:
            return Thread(key=a.key, title=a.title, kind=a.kind,
                          stage=a.stage, owner=a.owner, business=a.business)

    # 退一步按标题模糊比
    for t in ts:
        if q in t.title or t.title in q:
            return t
    for t in ts:
        if q in (t.key or ""):
            return t
    return None


# ══════════════════════════════════════════════════════════════════════
# 节点变化 / 他的处置 → 日志
# ══════════════════════════════════════════════════════════════════════

def record_stage(key: str, stage: str, owner: str = "", at: Optional[float] = None) -> bool:
    """story 节点变化时记一笔。

    这是「群里说提测了，但 story 没改」能被发现的唯一方式 ——
    有节点事件，才能把「消息说的事」和「系统记的事」对上。
    """
    if not key or not stage:
        return False
    stamp = at or time.time() * 1000
    # 同一事同一状态一天只记一次，避免每次轮询都灌一行
    day_tag = datetime.fromtimestamp(stamp / 1000).strftime("%Y%m%d")
    rid = f"stage:{key}:{stage}:{day_tag}"
    return append([{
        "id": rid,
        "k": key,
        "at": stamp,
        "kind": "mark",
        "src": "story",
        "who": owner,
        "txt": f"{stage}　{owner}".strip(),
    }]) > 0


def settle(key: str, action: str, until: Optional[float] = None,
           note: str = "", at: Optional[float] = None) -> bool:
    """记下他对一件事的处置。返回是否写入了新记录。

    action：done 办完了 / ignore 别管了 / snooze 先放放 / reopen 捡回来。
    until 只对 snooze 有效，毫秒时间戳；不传默认 SNOOZE_DEFAULT_DAYS 天后。

    id 里带日期：同一件事同一天同一种处置只留一条（日志是 upsert），
    他不会因为重复说「别管了」而把日志灌满。跨天的处置是两条不同记录，
    fold 时按 at 取最后一条，所以「昨天忽略、今天捡回来」也能正确表达。
    """
    if not key or action not in _SETTLE_ACTIONS:
        logger.warning("settle 参数不合法 key=%s action=%s", key, action)
        return False

    stamp = at or time.time() * 1000
    if action == SETTLE_SNOOZE and not until:
        until = stamp + SNOOZE_DEFAULT_DAYS * 86400 * 1000

    day_tag = datetime.fromtimestamp(stamp / 1000).strftime("%Y%m%d")
    ok = append([{
        "id": f"settle:{key}:{action}:{day_tag}",
        "k": key,
        "at": stamp,
        "kind": "settle",
        "src": "用户",
        "who": "我",
        "txt": action,
        "action": action,
        "until": float(until or 0),
        "note": note,
    }]) > 0

    if ok:
        logger.info(
            "处置已记：%s → %s%s",
            key, action,
            f"（到 {datetime.fromtimestamp(until / 1000):%m-%d}）" if action == SETTLE_SNOOZE and until else "",
        )
    return ok