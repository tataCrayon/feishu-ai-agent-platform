"""飞书 ↔ dsh 会话桥 — chat_id → dsh session 映射、串行保护、空闲回收。

负责：会话映射（含可选的落盘恢复）、同一会话的串行化、一轮 prompt 的文本收集、
TTL 回收与进程重启后的失效清理。
不负责：业务路由（dsh 侧）、卡片渲染（调用方传入的 `on_event`）、进程自愈（supervisor）。
调用方：`app/main.py` 的消息编排入口。

失败语义：向上抛 `app.dsh` 的 `DshError` 子类（进程不可用 / 协议错误），由调用方
转成用户可见回执；本模块不返回错误值、不生产面向用户的文案。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.dsh.adapter import DshAdapter, DshError, DshProtocolError, DshUnavailableError
from app.dsh.events import (
    SessionEvent,
    TextChunkEvent,
    TextChunkKind,
    ToolCallEvent,
    ToolCallPhase,
)
from app.dsh.media import build_prompt
from app.dsh.narrative import action_of, failure_hint
from app.multimodal import MediaBundle

if TYPE_CHECKING:
    from app.dsh.supervisor import DshSupervisor

logger = logging.getLogger("dsh.gateway")

# 召回上下文与用户原话之间的分隔：必须显眼到模型不会把二者混成一句用户请求，
# 同时**不进历史**——下一轮再召回时不会叠加上一轮那份。
_MEMORY_TEMPLATE = (
    "【长期记忆召回 — 系统注入，不是用户发言】\n{context}\n"
    "【召回结束】以上是你的历史记忆；用户这轮说的是：\n{message}"
)


def _with_memory(text: str, memory: str) -> str:
    """把召回上下文拼在本轮正文前；空召回原样返回，不留空壳段落。"""
    context = (memory or "").strip()
    if not context:
        return text
    return _MEMORY_TEMPLATE.format(context=context, message=text)

ProgressCallback = Callable[[str, str], None]
"""`(stage, detail)`。在适配器的读取协程内同步调用，实现方不得阻塞。"""

# 会话失效的协议错误特征：dsh 重启后旧 sessionId 不再存在。
# 只在这类错误上重建会话并重试；超时等错误可能「已执行」，重试会造成重复任务。
_SESSION_GONE_MARKERS = ("unknown session", "session not found", "no such session")


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """会话桥配置。

    Attributes:
        ttl_sec: 空闲多久后关闭会话（<=0 表示不回收）。
        reap_interval_sec: 后台回收循环的采样间隔。
        state_path: chat → session 映射的落盘位置；None 表示只存内存。
    """

    ttl_sec: float = 1800.0
    reap_interval_sec: float = 60.0
    state_path: Path | None = None


@dataclass
class _Entry:
    """一个 chat 的会话状态。`lock` 保证同一 chat 的 prompt 串行。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_id: str = ""
    last_used: float = 0.0


@dataclass
class _Turn:
    """一轮在途 prompt 的收集器（按 session 索引，串行保证同时最多一个）。

    `message_id` 用来只保留最后一轮 assistant 消息的文本：一个 turn 里模型会
    先说要做什么、然后边调工具边追加中间说明，飞书只该看到最终答案。
    """

    parts: list[str] = field(default_factory=list)
    on_event: ProgressCallback | None = None
    message_id: str = ""
    # 工具标题按 toolCallId 记住：update 帧常常只带状态不带 title，
    # 直接取 event.title 会让进度卡后半段退化成空行或裸 id。
    titles: dict[str, str] = field(default_factory=dict)
    # 工具步骤按 toolCallId 聚合：started 带入参、update 带结果，合成一条才算
    # 「这次工具调用到底做了什么」。轨迹（logs/turns/）靠它回答「怎么处理的」。
    steps: dict[str, dict[str, str]] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)

    def absorb(self, event: TextChunkEvent) -> None:
        if event.message_id != self.message_id:
            self.message_id = event.message_id
            self.parts.clear()
        self.parts.append(event.text)

    def track_tool(self, event: ToolCallEvent) -> None:
        """记一次工具事件（同 id 的 started/updated 合并成一条）。"""
        key = event.tool_call_id or f"_anon{len(self.step_order)}"
        step = self.steps.get(key)
        if step is None:
            step = {"name": "", "status": "started", "input": "", "output": "",
                    "at": time.strftime("%H:%M:%S")}
            self.steps[key] = step
            self.step_order.append(key)
        if event.title:
            step["name"] = event.title
        if event.raw_input:
            step["input"] = event.raw_input
        if event.output_preview:
            step["output"] = event.output_preview
        if event.status:
            step["status"] = event.status

    def step_list(self) -> list[dict[str, str]]:
        return [self.steps[k] for k in self.step_order]


class DshGateway:
    """飞书 chat 与 dsh 会话之间的唯一映射点。

    生命周期：`attach(supervisor)` → `handle(...)` × N → `shutdown()`。
    """

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self._cfg = config or GatewayConfig()
        self._entries: dict[str, _Entry] = {}
        self._turns: dict[str, _Turn] = {}
        # 最近一轮的收集器：调用方在 handle() 返回后读它拼轨迹（工具步骤只在
        # 内存里活一轮，不取就永久丢失）。
        self.last_turn: _Turn | None = None
        self._map_lock = asyncio.Lock()
        self._supervisor: DshSupervisor | None = None
        self._reaper: asyncio.Task[None] | None = None
        self._io_tasks: set[asyncio.Task[None]] = set()
        self._restore_state()

    # ── 接线 ──

    def attach(self, supervisor: DshSupervisor) -> None:
        """绑定 supervisor 并启动空闲回收循环。

        同时登记「进程重启后清空映射」：sessionId 随 dsh 进程消亡，旧值保留下来
        只会让下一条消息反复撞 unknown session；清空后按需懒建会话。
        """
        self._supervisor = supervisor
        supervisor.bind_session_factory(self._on_process_restarted)
        if self._reaper is None:
            self._reaper = asyncio.ensure_future(self._reap_loop())

    async def shutdown(self) -> None:
        """停止回收循环、关闭全部会话、落盘映射。"""
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        for entry in list(self._entries.values()):
            with contextlib.suppress(DshError):
                await self._drop_session(entry)
        self._save_state()
        pending = list(self._io_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("dsh gateway 已关停（%d 个 chat 映射已清理）", len(self._entries))

    # ── 事件入口（supervisor 的 on_event） ──

    def on_event(self, event: SessionEvent) -> None:
        """把事件归到对应轮次：可见文本累积，工具事件转成进度回调。"""
        turn = self._turns.get(getattr(event, "session_id", ""))
        if turn is None:
            # 会话建立时的规则注入轮没有收集器，属预期：静默丢弃。
            return
        if isinstance(event, TextChunkEvent):
            if event.kind is TextChunkKind.THOUGHT:
                # 思考帧进进度回调，与工具事件按到达顺序交给卡片拼时间线。
                # 不在这里节流：节流在卡片侧（2s 合并），丢帧会让推理断句。
                _emit(turn, "thought", event.text)
            else:
                turn.absorb(event)
            return
        if not isinstance(event, ToolCallEvent):
            return
        if event.title:
            turn.titles[event.tool_call_id] = event.title
        turn.track_tool(event)
        stage = _progress_stage(event)
        if stage is None or turn.on_event is None:
            return
        # 进度卡给用户看：说「在做什么」，不贴工具名（见 narrative 模块说明）。
        # 失败时追加一句可行动原因——重试三次和一次都显示同一句话时，
        # 用户只能看到三个 ❌ 却不知道卡在哪。
        title = event.title or turn.titles.get(event.tool_call_id) or ""
        action = action_of(title, event.kind)
        if stage == "agent_error":
            hint = failure_hint(event.output_preview)
            detail = f"{action}（{hint}）" if hint else action
        else:
            detail = action
        _emit(turn, stage, detail)

    # ── 主入口 ──

    async def handle(
        self,
        message: str,
        chat_id: str,
        on_event: ProgressCallback | None = None,
        media: MediaBundle | None = None,
        caller: dict[str, str] | None = None,
        memory: str = "",
    ) -> str:
        """提交一轮消息并返回答复文本。

        同一 `chat_id` 的并发调用在此串行（dsh 协议规定每会话单 prompt 在途）。
        空消息不提交，直接返回空串。

        `media` 里的图片、文档文本、降级说明会随本轮一起提交；只有纯附件没有
        正文的消息同样算有效输入（用户常直接甩一张截图）。

        `memory` 是 MemoryCore 召回的跨会话上下文（人设 / 场景导航 / 工具指引），
        由调用方拼在本轮正文前。**不存进会话历史**：召回结果每轮都不同，写进
        历史只会让同一段人设反复堆积。空串表示这轮没有召回。

        `caller`（sender_id / sender_name）在**建会话时**写进 MCP 子进程的 env：
        dsh 的 MCP 按会话拉起，子进程里的能力工具靠它把订阅/关注绑到正确的人。
        会话已存在时忽略——身份在建会话那一刻就定了，中途换人不该悄悄改绑。

        Raises:
            DshError: 进程不可用或协议失败；调用方负责转为用户可见回执。
        """
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        text = message.strip()
        if not text and (media is None or not media.has_media):
            return ""
        supervisor = self._require_supervisor()
        entry = await self._entry_for(chat_id)
        async with entry.lock:
            session_id = await self._session_for(entry, chat_id, caller)
            logger.debug("dsh prompt 提交（chat=%s session=%s…）", chat_id[:12], session_id[:8])
            try:
                answer = await self._prompt_once(
                    supervisor.adapter, session_id, text, on_event, media, memory
                )
            except DshProtocolError as e:
                if not _is_session_gone(e):
                    raise
                logger.warning("dsh 会话已失效，重建后重试一次: %s", e)
                await self._drop_session(entry)
                session_id = await self._session_for(entry, chat_id, caller)
                answer = await self._prompt_once(
                    supervisor.adapter, session_id, text, on_event, media, memory
                )
            entry.last_used = time.time()
            self._save_state()
            logger.info("dsh prompt 完成（chat=%s，答复 %d 字）", chat_id[:12], len(answer))
            return answer

    # 召回上下文与用户原话之间的分隔：必须显眼到模型不会把二者混成一句用户请求，
    # 同时**不进历史**——下一轮再召回时不会叠加上一轮那份。
    async def _entry_for(self, chat_id: str) -> _Entry:
        async with self._map_lock:
            entry = self._entries.get(chat_id)
            if entry is None:
                entry = _Entry()
                self._entries[chat_id] = entry
            return entry

    async def _session_for(
        self,
        entry: _Entry,
        chat_id: str = "",
        caller: dict[str, str] | None = None,
    ) -> str:
        """取当前会话；过期或缺失时建新会话（建的时候把调用方身份带进去）。"""
        if entry.session_id and _expired(entry.last_used, self._cfg.ttl_sec):
            logger.info("dsh 会话空闲超过 %.0fs，回收后重建", self._cfg.ttl_sec)
            await self._drop_session(entry)
        if not entry.session_id:
            _, session_id = await self._require_supervisor().open_session(chat_id, caller)
            entry.session_id = session_id
            entry.last_used = time.time()
            logger.info("dsh 会话已建立（session=%s…）", session_id[:8])
        return entry.session_id

    async def _prompt_once(
        self,
        adapter: DshAdapter,
        session_id: str,
        text: str,
        on_event: ProgressCallback | None,
        media: MediaBundle | None = None,
        memory: str = "",
    ) -> str:
        prompt = build_prompt(_with_memory(text, memory), media)
        blocks = prompt.blocks
        if not adapter.image_prompts_supported:
            # 服务端没广告图片能力时连块都不提交：ACP 会把整轮拒掉，
            # 开头的文字也会一起丢——那才是真正的「附件把消息带崩」。
            blocks = prompt.text_only()
        turn = _Turn(on_event=on_event)
        self._turns[session_id] = turn
        try:
            await adapter.session_prompt(session_id, text, blocks)
        finally:
            self._turns.pop(session_id, None)
        # 收集器只在内存活一轮：交给调用方拼轨迹后即弃（不留引用，防越积越多）。
        self.last_turn = turn
        return "".join(turn.parts).strip()

    async def _drop_session(self, entry: _Entry) -> None:
        session_id, entry.session_id = entry.session_id, ""
        entry.last_used = 0.0
        if not session_id:
            return
        self._turns.pop(session_id, None)
        supervisor = self._supervisor
        if supervisor is None or not supervisor.healthy:
            return
        # 进程已退出时 close 必然失败，结果等价于会话已不存在。
        with contextlib.suppress(DshError):
            await supervisor.adapter.session_close(session_id)

    async def _on_process_restarted(self, _adapter: DshAdapter) -> None:
        """dsh 进程重启后旧 sessionId 全部失效，清空映射等下条消息懒建。"""
        cleared = sum(1 for entry in self._entries.values() if entry.session_id)
        self._turns.clear()
        for entry in self._entries.values():
            entry.session_id = ""
            entry.last_used = 0.0
        if cleared:
            logger.info("dsh 进程重启，已失效 %d 个会话映射", cleared)

    # ── 内部：空闲回收 ──

    async def _reap_idle(self) -> int:
        """关闭空闲超时的会话；返回关闭数量。"""
        ttl = self._cfg.ttl_sec
        if ttl <= 0:
            return 0
        now = time.time()
        closed = 0
        async with self._map_lock:
            # 正在执行的会话跳过：prompt 本身的超时由 adapter 管，回收不能抢锁。
            stale = [
                entry
                for entry in self._entries.values()
                if entry.session_id and not entry.lock.locked()
                and _expired(entry.last_used, ttl, now)
            ]
        for entry in stale:
            async with entry.lock:
                if entry.session_id and _expired(entry.last_used, ttl):
                    await self._drop_session(entry)
                    closed += 1
        if closed:
            self._save_state()
            logger.info("dsh 空闲回收：关闭 %d 个会话", closed)
        return closed

    async def _reap_loop(self) -> None:
        """后台回收循环；按 conventions §三 吞掉所有异常——循环退出即永久失效。"""
        while True:
            try:
                await asyncio.sleep(self._cfg.reap_interval_sec)
                await self._reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("dsh 空闲回收循环异常（继续运行）: %s", e)
                await asyncio.sleep(1.0)

    # ── 内部：映射落盘 ──

    def _restore_state(self) -> None:
        """启动期恢复映射。

        同步读：文件只有几条记录，且只在构造时读一次——不值得为它在启动路径引入
        线程切换。过期条目直接丢弃，避免第一条消息先撞一次失效会话。
        """
        path = self._cfg.state_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("dsh 会话映射恢复失败（忽略）: %s", e)
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for chat_id, item in raw.items():
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "")
            try:
                last_used = float(item.get("last_used") or 0)
            except (TypeError, ValueError):
                continue
            if not session_id or _expired(last_used, self._cfg.ttl_sec, now):
                continue
            self._entries[str(chat_id)] = _Entry(
                session_id=session_id, last_used=last_used
            )
        if self._entries:
            logger.info("dsh 会话映射已恢复：%d 个 chat", len(self._entries))

    def _save_state(self) -> None:
        path = self._cfg.state_path
        if path is None:
            return
        snapshot = {
            chat_id: {"session_id": entry.session_id, "last_used": entry.last_used}
            for chat_id, entry in self._entries.items()
            if entry.session_id
        }
        task = asyncio.ensure_future(asyncio.to_thread(_write_state_file, path, snapshot))
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)

    def _require_supervisor(self) -> DshSupervisor:
        if self._supervisor is None:
            raise DshUnavailableError("dsh 网关尚未绑定 supervisor")
        return self._supervisor


def _emit(turn: _Turn, stage: str, detail: str) -> None:
    """调用进度回调；回调异常不允许打断执行（与 adapter 的事件分发同一口径）。"""
    callback = turn.on_event
    if callback is None:
        return
    try:
        callback(stage, detail)
    except Exception as e:
        logger.warning("进度回调异常（已忽略）: %s", e)


def _progress_stage(event: ToolCallEvent) -> str | None:
    """工具事件 → 进度卡 stage；与旧编排链路的 stage 词表保持一致。"""
    if event.phase is ToolCallPhase.STARTED:
        return "tool_start"
    if event.status == "completed":
        return "tool_done"
    if event.status == "failed":
        return "agent_error"
    return None


def _is_session_gone(error: DshProtocolError) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _SESSION_GONE_MARKERS)


def _expired(last_used: float, ttl_sec: float, now: float | None = None) -> bool:
    if ttl_sec <= 0:
        return False
    return (now if now is not None else time.time()) - last_used > ttl_sec


def _write_state_file(path: Path, snapshot: dict[str, dict[str, object]]) -> None:
    """写映射快照；失败只记日志（映射丢失仅等于下条消息开新会话）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as e:
        logger.warning("dsh 会话映射落盘失败: %s", e)
