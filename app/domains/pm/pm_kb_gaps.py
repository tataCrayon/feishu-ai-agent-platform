"""知识库缺口台账 —— 「这个我知道该补，但先不急」的东西要有地方落地。

为什么单独一个台账
------------------
他原话：「人力地图也不急，到时候我给你发，ESOP、票据流程先不急，
我觉得还是要记录下来，提醒补充。」

这类东西和待办（pm_awareness）不是一回事：
    待办  → 有明确动作和截止，做完打个勾
    缺口  → 没有截止，属于「知识库该有但现在没有」，会一直躺在那里

混进待办池只会让每日清单变脏：他不是每天要看到「补人力地图」，
而是每过一段时间被提醒一次「这几块还是空的」。所以这里用周级提醒，
用 p2 优先级，不抢当天的注意力。

存放位置
--------
project 根目录的 .kb_gaps.json（和 .followup_intake.json 同级）。
不放 knowledge-base/ 里 —— 那是事实资产，缺口是元数据，混在一起会被
pm_kb_index 当成正文切成检索块，问「人力地图」会命中一句「还没补」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pm_hub.gaps")

_GAPS_PATH = Path(__file__).resolve().parents[3] / ".kb_gaps.json"

# 每周哪一天提一句。周一最合适：本周还没铺开，插一句不打断节奏。
REMIND_WEEKDAY = 0

PRIORITY_ORDER = {"p0": 0, "p1": 1, "p2": 2}


@dataclass(frozen=True)
class Gap:
    id: str
    topic: str          # 缺什么，如「人力地图」
    why: str            # 补上之后能干什么 —— 没有这列，半年后自己都不知道为什么要它
    owner: str          # 谁补
    source: str         # 怎么来的：他说会发 / 流程没沉淀
    priority: str       # p0 卡着不能干活 / p1 影响判断 / p2 有更好
    created: str
    status: str         # open / done


# ══════════════════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════════════════

def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_GAPS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("读缺口台账失败，按空处理: %s", e)
        return {}


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    _GAPS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _key(topic: str) -> str:
    """topic 当主键。同名的不会再记一条 —— 他重复说「票据流程要补」是常态。"""
    return topic.strip()


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════

def add(
    topic: str,
    why: str,
    owner: str = "李四",
    source: str = "",
    priority: str = "p2",
) -> Gap:
    """登记一个缺口。已存在且未关闭的，只更新描述，不重复建。"""
    data = _load()
    key = _key(topic)
    if not key:
        raise ValueError("topic 不能为空")

    exist = data.get(key)
    if exist and exist.get("status") == "open":
        exist.update({"why": why or exist.get("why", ""), "owner": owner or exist.get("owner", "")})
        if source:
            exist["source"] = source
        if priority:
            exist["priority"] = priority
        _save(data)
        return Gap(**exist)

    gap = Gap(
        id=key,
        topic=key,
        why=why,
        owner=owner,
        source=source,
        priority=priority if priority in PRIORITY_ORDER else "p2",
        created=date.today().isoformat(),
        status="open",
    )
    data[key] = asdict(gap)
    _save(data)
    logger.info("登记知识库缺口: %s", key)
    return gap


def close(topic: str, note: str = "") -> bool:
    """补上了就关掉。note 记一句补在哪，方便日后回查。"""
    data = _load()
    key = _key(topic)
    entry = data.get(key)
    if not entry or entry.get("status") == "done":
        return False
    entry["status"] = "done"
    entry["done_at"] = date.today().isoformat()
    if note:
        entry["note"] = note
    _save(data)
    logger.info("关闭知识库缺口: %s", key)
    return True


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════

def open_gaps() -> List[Gap]:
    """未关闭的缺口，p0 排前面。"""
    items = [
        Gap(**{k: v for k, v in e.items() if k in Gap.__dataclass_fields__})
        for e in _load().values()
        if e.get("status") == "open"
    ]
    return sorted(items, key=lambda g: PRIORITY_ORDER.get(g.priority, 9))


def render() -> str:
    """全量清单。他说「还缺什么」「待补充有哪些」时给这个。"""
    gaps = open_gaps()
    if not gaps:
        return "知识库缺口台账是空的 —— 该补的都补了。"

    lines = [f"还等你补的 {len(gaps)} 块：", ""]
    for g in gaps:
        head = f"· {g.topic}"
        if g.priority == "p0":
            head += "（卡着干活）"
        lines.append(head)
        lines.append(f"　为什么：{g.why}")
        if g.source:
            lines.append(f"　怎么来：{g.source}")
        if g.owner:
            lines.append(f"　谁补：{g.owner}")
    lines.append("")
    lines.append("都是不急的，想动哪块说一声，我按你给的材料沉进知识库。")
    return "\n".join(lines)


def render_weekly(today: Optional[date] = None) -> str:
    """周一才吐一句话，其他天返回空。

    不每天提 —— 他明确说过这几块不急。每天出现一次就是拿噪音换存在感。
    """
    today = today or date.today()
    if today.weekday() != REMIND_WEEKDAY:
        return ""
    gaps = open_gaps()
    if not gaps:
        return ""
    topics = "、".join(g.topic for g in gaps)
    return f"知识库还缺 {len(gaps)} 块：{topics}。不急，等你哪天顺手了给我。"


# ══════════════════════════════════════════════════════════════════════
# 一次性种子：他口头确认过的三块
# ══════════════════════════════════════════════════════════════════════

_SEEDS = (
    {
        "topic": "人力地图",
        "why": "判断需求该压给谁、谁手上已经满了。现在排期分析只能说「这条只有 1 个人扛」，"
               "说不了「他同期还压着几条」。",
        "source": "他说到时候发给我",
        "priority": "p2",
    },
    {
        "topic": "ESOP 流程",
        "why": "ESOP 已划进我的负责范围，但没有对应 SOP。行权窗口、结算节点这些没有基准，"
               "排期分析只能看节点日期，判断不了这一步排得合不合理。",
        "source": "他说先不急，但要记下来提醒补",
        "priority": "p2",
    },
    {
        "topic": "票据流程",
        "why": "票据同样已在负责范围内，且票据系统下周上线，没有流程文档就没法判断"
               "上线前该有哪些动作。",
        "source": "他说先不急，但要记下来提醒补",
        "priority": "p2",
    },
)


def seed_defaults() -> int:
    """把三块已知缺口落进台账。幂等：重复跑不会产生重复条目。"""
    before = len(open_gaps())
    for s in _SEEDS:
        add(s["topic"], s["why"], source=s["source"], priority=s["priority"])
    return len(open_gaps()) - before