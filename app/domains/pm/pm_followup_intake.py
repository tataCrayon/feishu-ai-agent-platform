"""每天下班前的待跟进排期 —— 17:30 问一次，18:00 没给再提一次。

为什么要定时问
--------------
他说得很清楚：「待跟进事项我给，但是需要你每天下班前让我去排」。
待跟进是他脑子里的事，机器人猜不出来；但人会忘，所以需要到点提醒一次。
关键是**只提两次**：17:30 问，18:00 补一句，之后不再烦。

为什么 18:00 之后再问没意义
--------------------------
那时他已经下班或者在看别的东西，再推就是打扰。漏了就漏了，
次日 09:00 的日计划里还会带出当前清单，成本可控。

状态
----
.followup_intake.json   {"asked": "2026-11-02", "answered": "2026-11-02"}

    asked     当天已经问过（防止重启后重复问）
    answered  当天已经收到他的排期（收到就不再提醒）
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from app.domains.pm import pm_awareness, pm_followup

logger = logging.getLogger("pm_hub.intake")

_STATE_PATH = Path(__file__).resolve().parents[3] / ".followup_intake.json"

# 到点问。这两个是本模块唯一的节拍定义，daemon 只负责调用。
ASK_HOUR, ASK_MINUTE = 17, 30
REMIND_HOUR = 18


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写排期状态失败: %s", e)


def need_ask(now: Optional[datetime] = None) -> bool:
    """今天该问了吗：过了 17:30，且今天还没问过。"""
    now = now or datetime.now()
    if now.hour < ASK_HOUR or (now.hour == ASK_HOUR and now.minute < ASK_MINUTE):
        return False
    return _load().get("asked") != _today()


def need_remind(now: Optional[datetime] = None) -> bool:
    """该补一句了吗：过了 18:00，今天问过、还没收到排期、也还没提醒过。

    reminded 这个标记不能省 —— 主循环 3 秒转一圈，没有它 18:00 到 24:00
    会给他发上千条「还没收到」。
    """
    now = now or datetime.now()
    if now.hour < REMIND_HOUR:
        return False
    data = _load()
    return (
        data.get("asked") == _today()
        and data.get("answered") != _today()
        and data.get("reminded") != _today()
    )


def mark_asked() -> None:
    data = _load()
    data["asked"] = _today()
    _save(data)


def mark_reminded() -> None:
    data = _load()
    data["reminded"] = _today()
    _save(data)


def mark_answered() -> None:
    """他给了排期。收到就不再提醒，也不追问。"""
    data = _load()
    data["answered"] = _today()
    _save(data)


def note_reply() -> bool:
    """他私聊说话了 —— 如果今天问过排期，就记成已给。

    刻意不解析内容：18:00 之后他发的任何话，大概率就是在排期；
    再去抠"这算不算待跟进"只会漏判，然后 20:00 补一句「还没收到」把他惹烦。
    记成已给的最坏后果只是今晚不再提醒，明早日计划照样有清单。
    """
    today = _today()
    data = _load()
    if data.get("asked") != today or data.get("answered") == today:
        return False
    mark_answered()
    return True


def ask_text() -> str:
    """17:30 那条。先把他手上挂着的东西摊开，比空口问「有什么要跟的」好答。"""
    lines = ["明天要跟的事，你排一下。"]

    try:
        items = pm_followup.poll()
        waiting = [p for p in items if p.kind == "stuck"]
        moved = [p for p in items if p.kind in ("handed_over", "owner_changed")]
    except Exception as e:
        logger.warning("读跟进进展失败（不影响提问）: %s", e)
        waiting, moved = [], []

    if moved:
        lines.append("")
        lines.append(f"今天有 {len(moved)} 条动了：")
        for p in moved[:4]:
            lines.append(f"· {p.headline}")

    if waiting:
        lines.append("")
        lines.append(f"还有 {len(waiting)} 条问了没动静：")
        for p in waiting[:4]:
            lines.append(f"· {p.headline}（{p.days} 天）")

    todos = ""
    try:
        todos = pm_awareness.render_todos()
    except Exception:
        pass
    if todos:
        lines.append("")
        lines.append(todos)

    lines.append("")
    lines.append("直接说就行，比如「明天：催 11.26 提测、找赵六确认测试排期」。")
    return "\n".join(lines)


def remind_text() -> str:
    """18:00 那条。只提一次，别让他觉得被追着。"""
    return (
        "还没收到你明天的待跟进。\n"
        "不回也没事，明早的日计划会按现在的清单走。"
    )