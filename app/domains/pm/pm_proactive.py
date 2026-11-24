"""事件驱动开口 —— 感知线程的第三路出口。

以前感知线程只有两条出口：

    冲我来的（@我 / 私聊 / 点名） → 立刻私聊推
    其余                         → 攒进 .watch_pending.json，等日计划一起给

缺的正是第三条：某件事已经推进到「该问一句」的节点，但没人在群里 @ 我。
这类消息以前只能等 18:00 过审才被看到 —— 那时候再问，排期已经跑偏了。

这个模块不做「提醒」。提醒他还是要自己想怎么说。
它做的是**代拟一句话术**：他看完复制就能发出去。

为什么不调模型生成话术
--------------------
这段代码跑在感知线程里，模型一超时（30 秒级）整轮扫描就被拖住，
后面几十个会话全排队。模板里已经带了人名和原文，具体度够了；
措辞靠人工打磨，比赌模型稳定更划算。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pm_hub.proactive")

PROJECT_DIR = Path(__file__).resolve().parents[3]
_STATE_PATH = PROJECT_DIR / ".proactive_state.json"

# 同一轮最多推几件。多了就变成刷屏，反而没人看。
MAX_PICKS = 3

# 原文引用长度。太长会把推送撑爆，太短看不出在说什么。
QUOTE_LIMIT = 60


@dataclass(frozen=True)
class Stage:
    """一个推进节点：命中哪些词、为什么要现在问、怎么开口。"""

    key: str        # 阶段名，出现在推送标题里
    words: tuple    # 触发词。命中任意一个就算到达这个节点
    angle: str      # 「为什么现在问」—— 这句是判断依据，不是客套
    template: str   # 兜底话术，{who} 替换成对方名字


# 顺序有意义：一条消息同时命中多个阶段时，只取最靠前的那个。
# 「卡住」放最前 —— 卡住不说，后面所有日期都是假的。
_STAGES = (
    Stage(
        key="卡住",
        words=("阻塞", "卡住", "延期", "故障", "风险", "延误"),
        angle="卡住的时候不问原因和影响面，排期就是假的。",
        template="哈喽{who}，这块是卡在哪了？需要我协调谁你说一声，我这边好跟客户交代。",
    ),
    Stage(
        key="提测",
        words=("提测", "测试环境", "可验证"),
        angle="提测之后验收标准和灰度范围没对齐，测试跑完还得返工。",
        template="哈喽{who}，提测收到。验收标准和灰度范围定了吗？我这边好同步给客户。",
    ),
    Stage(
        key="上线",
        words=("上线", "投产", "发版", "发布"),
        angle="上线只走完一半，验收结果和客户反馈今天不收，明天就凉了。",
        template="哈喽{who}，上线还顺利吧？验收结果和客户那边的反馈今晚能给我吗？",
    ),
    Stage(
        key="评审",
        words=("评审", "定稿", "方案确认"),
        angle="方案到评审这步，需求边界和验收口径不定下来，后面全是扯。",
        template="哈喽{who}，评审这块需求边界和验收口径能定下来吗？我按这个往下排。",
    ),
)


@dataclass
class Pick:
    stage: Stage
    event: Any      # pm_watch.WatchEvent，这里只用到几个字段，不引类型避免环形依赖


def _match(text: str) -> Optional[Stage]:
    body = text or ""
    for stage in _STAGES:
        if any(w in body for w in stage.words):
            return stage
    return None


def _load_state(today_key: str) -> Dict[str, str]:
    """读「今天已经对哪些会话开过口」。返回 {chat_id: stage_key}。"""
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if raw.get("date") != today_key:
        return {}
    return dict(raw.get("sent") or {})


def _save_state(today_key: str, sent: Dict[str, str]) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps({"date": today_key, "sent": sent}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("写开口状态失败: %s", e)


def pick(events: List[Any], today_key: str = "") -> List[Pick]:
    """筛出该主动开口的事件。

    三道门，缺一不可：

    1. 他没回过 —— 回过说明已经处理了，再问很蠢
    2. 不是冲他来的 —— 那一路已经由「有人找你」推过，不要重复
    3. 这个会话今天还没开过口 —— 一个群一天最多打扰一次
    """
    if not events:
        return []

    today_key = today_key or time.strftime("%Y-%m-%d")
    sent = _load_state(today_key)
    picks: List[Pick] = []

    for e in events:
        if len(picks) >= MAX_PICKS:
            break
        if getattr(e, "replied", False):
            continue
        # 冲他来的走「有人找你」那一路，这里不重复打扰。
        # 读字段，不猜文案：判定在 pm_watch 采集时就已经落成 directed。
        if getattr(e, "directed", False):
            continue

        chat_id = getattr(e, "chat_id", "")
        if chat_id and chat_id in sent:
            continue

        stage = _match(getattr(e, "text", ""))
        if not stage:
            continue

        if chat_id:
            sent[chat_id] = stage.key
        picks.append(Pick(stage=stage, event=e))

    if picks:
        _save_state(today_key, sent)
    return picks


def _who(event: Any) -> str:
    """对方怎么称呼。私聊没名字时退回「你」。"""
    name = (getattr(event, "sender_name", "") or "").strip()
    if not name or name.startswith("ou_"):
        return "你"
    return name


def _where(event: Any) -> str:
    chat_name = (getattr(event, "chat_name", "") or "").strip()
    mode = getattr(event, "chat_mode", "group")
    who = _who(event)
    if mode == "p2p":
        return f"私聊 · {who}"
    return f"{chat_name or '群聊'} · {who}"


def _clip(text: str, limit: int = QUOTE_LIMIT) -> str:
    body = " ".join((text or "").split())
    return body if len(body) <= limit else body[:limit] + "…"


def render(picks: List[Pick]) -> str:
    """渲染成一条可以直接发出去的推送。没内容返回空串，调用方据此跳过。"""
    if not picks:
        return ""

    lines = [f"【该问一句了 · {len(picks)} 件】"]
    for p in picks:
        e = p.event
        who = _who(e)
        lines.append("")
        lines.append(f"· {p.stage.key}　{_where(e)}")
        lines.append(f"  「{_clip(getattr(e, 'text', ''))}」")
        lines.append(f"  为什么现在问 —— {p.stage.angle}")
        lines.append(f"  这么说就行 —— {p.stage.template.replace('{who}', who)}")

    lines.append("")
    lines.append("不用我开口就忽略；想改语气，跟我说这句该怎么说。")
    return "\n".join(lines)