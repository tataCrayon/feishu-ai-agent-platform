"""dsh 事件翻译 — 内部事件 → TaskRecord span / 进度卡状态（纯函数，无 IO）。

负责：定义内部事件模型，以及把事件折叠成可观测结构与卡片数据。
不负责：解析 ACP 报文（adapter.py）、渲染飞书卡片 JSON（gateway 侧）。
调用方：adapter.py 的事件消费方、后续 TaskRecord 写入点与进度卡。
失败语义：入参缺必填字段抛 ValueError；本模块不产生 IO，不会抛 IO 异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TypeAlias

# 进度卡最多展示的步骤数：飞书卡片高度有限，超出部分折叠成一行计数，
# 全部步骤仍在 TaskRecord 里（卡片只是视图，不是事实源）。
MAX_PROGRESS_STEPS = 10
# 卡片上的答案预览上限：卡片是摘要视图，全文走 ReplyStore。
ANSWER_PREVIEW_LIMIT = 4000
# span 里保留的工具输出/入参上限，避免单条 trace 膨胀。
SPAN_DETAIL_LIMIT = 200


class TextChunkKind(StrEnum):
    """assistant 文本块的来源：思考过程还是可见回复。"""

    THOUGHT = "thought"
    MESSAGE = "message"


class ToolCallPhase(StrEnum):
    """工具事件对应的生命周期阶段。"""

    STARTED = "started"
    UPDATED = "updated"


# 步骤/span 状态统一用这三个值，协议原始值（in_progress/completed/failed）在
# to_span/apply_event 里一次性映射掉，下游不再判断协议取值。
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"


@dataclass(frozen=True, slots=True)
class TextChunkEvent:
    """assistant 文本块（思考或可见回复）。"""

    session_id: str
    kind: TextChunkKind
    text: str
    message_id: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("TextChunkEvent.session_id 不能为空")
        if not self.text:
            raise ValueError("TextChunkEvent.text 不能为空")


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    """一次工具调用的开始或状态更新。

    Attributes:
        status: 协议原始值（in_progress / completed / failed），翻译时再归一。
        raw_input: 模型给的入参，JSON 文本；非法 JSON 时保留原文。
        output_preview: 工具结果文本预览（仅更新事件有）。
    """

    session_id: str
    tool_call_id: str
    title: str = ""
    status: str = "in_progress"
    kind: str = "other"
    phase: ToolCallPhase = ToolCallPhase.STARTED
    raw_input: str = ""
    output_preview: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("ToolCallEvent.session_id 不能为空")
        if not self.tool_call_id:
            raise ValueError("ToolCallEvent.tool_call_id 不能为空")


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """上下文占用快照（非计费口径，仅用于卡片/档案展示）。"""

    session_id: str
    used_tokens: int = 0
    context_size: int = 0

    def __post_init__(self) -> None:
        if self.used_tokens < 0:
            raise ValueError("UsageEvent.used_tokens 不能为负")


@dataclass(frozen=True, slots=True)
class StopEvent:
    """一轮 prompt 结束；stop_reason 为协议原值（如 end_turn/cancelled）。"""

    session_id: str
    stop_reason: str = "end_turn"

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("StopEvent.session_id 不能为空")


SessionEvent: TypeAlias = ToolCallEvent | TextChunkEvent | UsageEvent | StopEvent


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """写入 trace 的单条 span（logs/agents/trace.jsonl 的行结构）。

    Attributes:
        span: span 类型（tool_call / turn / usage）。
        name: 展示名（工具标题或 stop_reason）。
        status: running / success / error / cancelled。
        tool_call_id: 工具调用关联键，非工具 span 为空。
        tokens: usage span 的上下文占用，其余为 0。
        attributes: 附带明细，值一律为字符串以便 jsonl 直接落盘。
    """

    span: str
    name: str
    status: str = STATUS_SUCCESS
    tool_call_id: str = ""
    tokens: int = 0
    attributes: dict[str, str] = field(default_factory=dict)


_TOOL_STATUS = {
    "in_progress": STATUS_RUNNING,
    "completed": STATUS_SUCCESS,
    "failed": STATUS_ERROR,
}
_STOP_ERROR_REASONS = {"max_tokens", "error"}


def to_span(event: SessionEvent) -> SpanRecord | None:
    """把内部事件翻译成一条 span；文本块不是 span，返回 None。"""
    if isinstance(event, ToolCallEvent):
        return SpanRecord(
            span="tool_call",
            name=event.title or event.tool_call_id,
            status=_TOOL_STATUS.get(event.status, STATUS_RUNNING),
            tool_call_id=event.tool_call_id,
            attributes={
                "phase": str(event.phase),
                "kind": event.kind,
                "raw_input": _clip(event.raw_input, SPAN_DETAIL_LIMIT),
                "output_preview": _clip(event.output_preview, SPAN_DETAIL_LIMIT),
            },
        )
    if isinstance(event, StopEvent):
        if event.stop_reason == "cancelled":
            status = "cancelled"
        elif event.stop_reason in _STOP_ERROR_REASONS:
            status = STATUS_ERROR
        else:
            status = STATUS_SUCCESS
        return SpanRecord(span="turn", name=event.stop_reason, status=status)
    if isinstance(event, UsageEvent):
        return SpanRecord(
            span="usage",
            name="context",
            status=STATUS_SUCCESS,
            tokens=event.used_tokens,
            attributes={"context_size": str(event.context_size)},
        )
    return None


class CardStatus(StrEnum):
    """进度卡整体状态。"""

    RUNNING = "running"
    DONE = "done"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ProgressStep:
    """进度卡上的一行：一次工具调用的当前状态。"""

    seq: int
    tool_call_id: str
    title: str
    status: str = STATUS_RUNNING


@dataclass(frozen=True, slots=True)
class ProgressCard:
    """进度卡的完整数据（飞书卡片渲染的输入，单卡原地更新）。

    Attributes:
        steps: 最近 MAX_PROGRESS_STEPS 条步骤，按发生顺序。
        omitted_steps: 被折叠掉的历史步骤数（卡片只显示计数，不丢档案）。
        answer_preview: 已收到的 assistant 可见文本（截尾）。
    """

    task_id: str
    title: str
    status: CardStatus = CardStatus.RUNNING
    steps: tuple[ProgressStep, ...] = ()
    omitted_steps: int = 0
    answer_preview: str = ""
    used_tokens: int = 0
    stop_reason: str = ""


def new_card(task_id: str, title: str) -> ProgressCard:
    """创建一张刚开跑的进度卡（标题是意图复述）。"""
    if not task_id:
        raise ValueError("ProgressCard.task_id 不能为空")
    return ProgressCard(task_id=task_id, title=title)


def apply_event(card: ProgressCard, event: SessionEvent) -> ProgressCard:
    """把一条事件折叠进卡片；返回新对象，调用方持有上一版（卡片可对比/回放）。"""
    if isinstance(event, TextChunkEvent):
        if event.kind is not TextChunkKind.MESSAGE:
            return card
        if len(card.answer_preview) >= ANSWER_PREVIEW_LIMIT:
            # 预览已满就停止累积：继续拼接只会每轮复制一份长字符串，
            # 全文由调用方从事件流自行落盘（ReplyStore）。
            return card
        answer = (card.answer_preview + event.text)[:ANSWER_PREVIEW_LIMIT]
        return replace(card, answer_preview=answer)

    if isinstance(event, UsageEvent):
        return replace(card, used_tokens=event.used_tokens)

    if isinstance(event, StopEvent):
        card_status = (
            CardStatus.INTERRUPTED if event.stop_reason == "cancelled" else CardStatus.DONE
        )
        return replace(card, status=card_status, stop_reason=event.stop_reason)

    steps = list(card.steps)
    index = next(
        (i for i, step in enumerate(steps) if step.tool_call_id == event.tool_call_id),
        None,
    )
    step_status = _TOOL_STATUS.get(event.status, STATUS_RUNNING)
    if index is None:
        # 更新事件先于开始事件到达（或开始事件被丢弃）时也要建行，
        # 否则卡片会漏掉一次真实工具调用。
        next_seq = steps[-1].seq + 1 if steps else 1
        steps.append(
            ProgressStep(
                seq=next_seq,
                tool_call_id=event.tool_call_id,
                title=event.title or event.tool_call_id,
                status=step_status,
            )
        )
    else:
        previous = steps[index]
        steps[index] = replace(
            previous,
            title=event.title or previous.title,
            status=step_status,
        )

    omitted = card.omitted_steps
    if len(steps) > MAX_PROGRESS_STEPS:
        omitted += len(steps) - MAX_PROGRESS_STEPS
        steps = steps[-MAX_PROGRESS_STEPS:]
    return replace(card, steps=tuple(steps), omitted_steps=omitted)


def _clip(text: str, limit: int) -> str:
    """保留前 limit 个字符；卡片与 span 都只需要开头（尾部常是无信息量的重试/堆栈）。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
