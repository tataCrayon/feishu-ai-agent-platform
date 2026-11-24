"""问出去之后的闭环 —— 谁动了、谁没动、动了之后该找谁。

为什么必须有这一层
------------------
问一句「提测了嘛」只是开头。真正花时间的是后面：等回复、判断对方说的
靠不靠谱、提测了还得转头找测试问什么时候能测完。这些如果留在人脑子里，
那这个机器人就只是个话术生成器。

判断依据只用 story，不猜聊天内容
--------------------------------
他说过「story 他们不及时更新」。但**提测这一步绕不开 story** ——
测试要接着干，状态不改测试根本不知道有活。所以：

    状态从 待开发/实现中 → SIT测试     ⇒ 确实提测了
    当前负责人从 开发 → 测试           ⇒ 该问的人换了

这两个信号比"翻对方聊天记录猜他说了什么"可靠得多，也不涉及读别人私聊。
聊天内容作为补充信号后续再接。

状态机
------
    asked     已发问询，等回复
    advanced  对方有动作了（状态变了，或负责人换了）
    closed    已归档（上线了 / 不用再跟）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domains.pm import pm_story_source

logger = logging.getLogger("pm_hub.followup")

_STATE_PATH = Path(__file__).resolve().parents[3] / ".nudge_followup.json"

# 停了这么多天还没动静，值得再问一次。不是每天催 —— 那会把人催烦。
STALE_DAYS = 3


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写跟进状态失败: %s", e)


def record(sent_items: List[Dict[str, Any]]) -> int:
    """登记刚发出去的问询。sent_items 由调用方组装：

        {"story_id": 7152457342, "story_name": "...", "audience": "翁奕鹏",
         "open_id": "ou_...", "role": "dev", "stage": "实现中",
         "operator": "翁奕鹏", "message_id": "om_..."}

    没有 story_id 的（工作簿来源）不登记 —— 回读不了，登了也是死数据。
    """
    data = _load()
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for it in sent_items:
        sid = str(it.get("story_id") or "")
        if not sid or sid == "0":
            continue
        data[sid] = {
            **it,
            "asked_at": now,
            "state": "asked",
            "checked_at": "",
        }
        added += 1
    if added:
        _save(data)
        logger.info("登记 %d 条问询跟进", added)
    return added


@dataclass
class Progress:
    """一条问询的当前状态。"""
    entry: Dict[str, Any]
    now_stage: str
    now_operator: str
    kind: str          # handed_over / owner_changed / stuck / closed / gone
    days: int          # 问出去多少天了

    @property
    def headline(self) -> str:
        name = self.entry.get("story_name") or ""
        if len(name) > 28:
            name = name[:27] + "…"
        return name


def poll(today: Optional[date] = None) -> List[Progress]:
    """回读所有在跟的问询，产出有变化或该追的那些。

    只返回值得他知道的：有进展、或者停太久。中间的"没动但也没到该追"
    不返回 —— 每天报一遍"还是没动"就是噪音。
    """
    today = today or date.today()
    data = _load()
    if not data:
        return []

    ids = [int(k) for k in data if str(k).isdigit()]
    try:
        current = pm_story_source.stories_by_ids(ids)
    except Exception as e:
        logger.warning("回读 story 失败，本轮跳过: %s", e)
        return []

    out: List[Progress] = []
    for sid, entry in data.items():
        if entry.get("state") == "closed":
            continue

        story = current.get(int(sid)) if str(sid).isdigit() else None
        asked_at = entry.get("asked_at") or ""
        try:
            days = (today - datetime.fromisoformat(asked_at).date()).days
        except Exception:
            days = 0

        if story is None:
            # 需求被删或转走了，不再跟
            entry["state"] = "closed"
            out.append(Progress(entry, "", "", "gone", days))
            continue

        was_stage = entry.get("stage") or ""
        was_owner = entry.get("operator") or ""

        if story.stage in ("已上线", "生产验证"):
            entry["state"] = "closed"
            out.append(Progress(entry, story.stage, story.operator, "closed", days))
            continue

        if story.handed_over and was_stage not in pm_story_source.HANDED_OVER_STAGES:
            # 从开发环节走到了测试环节
            out.append(Progress(entry, story.stage, story.operator, "handed_over", days))
            entry["state"] = "advanced"
            continue

        if story.operator and was_owner and story.operator != was_owner:
            out.append(Progress(entry, story.stage, story.operator, "owner_changed", days))
            entry["state"] = "advanced"
            continue

        if days >= STALE_DAYS:
            out.append(Progress(entry, story.stage, story.operator, "stuck", days))

    for p in out:
        data[str(p.entry.get("story_id"))]["checked_at"] = today.isoformat()
    _save(data)
    return out


def render(items: List[Progress]) -> str:
    """把进展排成人话。他扫一眼就知道该动谁。"""
    if not items:
        return ""

    advanced = [p for p in items if p.kind in ("handed_over", "owner_changed")]
    stuck = [p for p in items if p.kind == "stuck"]
    closed = [p for p in items if p.kind == "closed"]

    lines = []
    if advanced:
        lines.append(f"有 {len(advanced)} 条动了：")
        for p in advanced:
            if p.kind == "handed_over":
                who = p.now_operator or "（没写负责人）"
                lines.append(f"· {p.headline} → 已提测（{p.now_stage}），该问 {who} 什么时候测完")
            else:
                lines.append(f"· {p.headline} → 负责人换成 {p.now_operator}，该问他了")
        lines.append("")

    if stuck:
        lines.append(f"有 {len(stuck)} 条问了没动静：")
        for p in stuck:
            lines.append(f"· {p.headline} → 还停在「{p.now_stage}」，{p.days} 天了")
        lines.append("")

    if closed:
        lines.append(f"{len(closed)} 条已经不用跟了（上线或已撤）。")

    return "\n".join(lines).rstrip()


def close(story_ids: List[int]) -> int:
    """手工归档。他说「这几条不用跟了」时用。"""
    data = _load()
    n = 0
    for sid in story_ids:
        key = str(sid)
        if key in data and data[key].get("state") != "closed":
            data[key]["state"] = "closed"
            n += 1
    if n:
        _save(data)
    return n