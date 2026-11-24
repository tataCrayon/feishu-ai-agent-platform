"""dsh 网关的契约测试 — 会话映射、串行保护、TTL 回收。

不拉真实 dsh：用替身 `_FakeSupervisor` 记录「调了哪个会话、有没有并发」，
断言的是网关自己决定的映射与调度，而不是 dsh 的行为。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.dsh.events import (
    StopEvent,
    TextChunkEvent,
    TextChunkKind,
    ToolCallEvent,
    ToolCallPhase,
)
from app.dsh.gateway import DshGateway, GatewayConfig
from app.multimodal import MediaBundle, MediaPart

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeAdapter:
    """记录每次 prompt 的 sessionId，并回一段可见文本（模拟模型输出）。"""

    def __init__(self, gateway: DshGateway) -> None:
        self.gateway = gateway
        self.prompts: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.active = 0
        self.max_active = 0
        self.delay_sec = 0.0
        self.reply_text = "收到"
        self.tool_events: list[ToolCallEvent] = []
        self.chunks: list[tuple[str, str]] = []
        self.blocks: list[list[dict[str, Any]]] = []
        self.image_prompts_supported = False

    async def session_prompt(
        self, session_id: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> StopEvent:
        self.prompts.append((session_id, text))
        self.blocks.append(list(blocks or []))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for event in self.tool_events:
                self.gateway.on_event(event)
            await asyncio.sleep(self.delay_sec)
            chunks = self.chunks or [("", self.reply_text)]
            for message_id, chunk in chunks:
                self.gateway.on_event(TextChunkEvent(
                    session_id=session_id,
                    kind=TextChunkKind.MESSAGE,
                    text=chunk,
                    message_id=message_id,
                ))
        finally:
            self.active -= 1
        return StopEvent(session_id=session_id)

    async def session_close(self, session_id: str) -> None:
        self.closed.append(session_id)


class _FakeSupervisor:
    """只实现网关用到的三个入口：bind_session_factory / open_session / adapter。"""

    def __init__(self, gateway: DshGateway) -> None:
        self.adapter = _FakeAdapter(gateway)
        self.healthy = True
        self.opened: list[str] = []
        # open_session 的入参留档：调用方身份必须一路传到 supervisor，
        # 传丢了的表现是「订阅到了别人名下」，只能靠断言守住。
        self.calls: list[dict] = []
        self._factory: Callable[[Any], Awaitable[None]] | None = None

    def bind_session_factory(
        self, factory: Callable[[Any], Awaitable[None]]
    ) -> None:
        self._factory = factory

    async def open_session(self, chat_id: str = "", caller: dict | None = None):
        """与真实 supervisor 同签名：会话建立时带上调用方身份。"""
        session_id = f"session-{len(self.opened) + 1}"
        self.opened.append(session_id)
        self.calls.append({"chat_id": chat_id, "caller": caller})
        return self.adapter, session_id


def _make_gateway(
    config: GatewayConfig | None = None,
) -> tuple[DshGateway, _FakeSupervisor]:
    gateway = DshGateway(config or GatewayConfig(ttl_sec=0, reap_interval_sec=3600))
    supervisor = _FakeSupervisor(gateway)
    gateway.attach(supervisor)
    return gateway, supervisor


def test_handle_same_chat_reuses_session() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        try:
            first = await gateway.handle("第一条", "oc_chat")
            second = await gateway.handle("第二条", "oc_chat")
        finally:
            await gateway.shutdown()

        assert (first, second) == ("收到", "收到")
        assert len(supervisor.opened) == 1
        session_ids = {sid for sid, _ in supervisor.adapter.prompts}
        assert session_ids == {"session-1"}
        assert supervisor.adapter.closed == ["session-1"]

    asyncio.run(_run())


def test_handle_different_chats_get_separate_sessions() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        try:
            await gateway.handle("甲", "oc_a")
            await gateway.handle("乙", "oc_b")
        finally:
            await gateway.shutdown()

        assert supervisor.opened == ["session-1", "session-2"]

    asyncio.run(_run())


def test_handle_same_chat_concurrent_is_serialized() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.delay_sec = 0.05
        try:
            first, second = await asyncio.gather(
                gateway.handle("第一条", "oc_chat"),
                gateway.handle("第二条", "oc_chat"),
            )
        finally:
            await gateway.shutdown()

        assert (first, second) == ("收到", "收到")
        # 串行：任一时刻只有一轮 prompt 在途，第二个必须等第一个结算
        assert supervisor.adapter.max_active == 1
        assert [text for _, text in supervisor.adapter.prompts] == ["第一条", "第二条"]
        assert len(supervisor.opened) == 1

    asyncio.run(_run())


def test_handle_after_ttl_opens_new_session() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway(
            GatewayConfig(ttl_sec=0.01, reap_interval_sec=3600)
        )
        try:
            await gateway.handle("先问", "oc_chat")
            await asyncio.sleep(0.05)
            await gateway.handle("再问", "oc_chat")
        finally:
            await gateway.shutdown()

        assert supervisor.opened == ["session-1", "session-2"]
        assert supervisor.adapter.closed == ["session-1", "session-2"]

    asyncio.run(_run())


def test_handle_empty_message_does_not_touch_session() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        try:
            assert await gateway.handle("   ", "oc_chat") == ""
        finally:
            await gateway.shutdown()

        assert supervisor.opened == []
        assert supervisor.adapter.prompts == []

    asyncio.run(_run())


def test_reap_idle_closes_only_idle_sessions() -> None:
    async def _run() -> None:
        # TTL 设为一天，避免测试本身耗时；用 last_used 直接表达「哪个会话空闲」
        gateway, supervisor = _make_gateway(
            GatewayConfig(ttl_sec=86400, reap_interval_sec=3600)
        )
        await gateway.handle("甲", "oc_a")
        await gateway.handle("乙", "oc_b")
        try:
            gateway._entries["oc_a"].last_used = time.time() - 2 * 86400
            gateway._entries["oc_b"].last_used = time.time()
            closed = await gateway._reap_idle()
            assert closed == 1
            assert supervisor.adapter.closed == ["session-1"]
        finally:
            await gateway.shutdown()

    asyncio.run(_run())


def test_handle_returns_only_last_assistant_message() -> None:
    """一轮里模型先说要做什么、再给最终答案：只返回最终答案，不带过程独白。"""

    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.chunks = [
            ("msg-1", "用户意图：查新闻，"),
            ("msg-1", "先加载技能。"),
            ("msg-2", "news-1 news-2"),
        ]
        try:
            answer = await gateway.handle("查新闻", "oc_chat")
        finally:
            await gateway.shutdown()

        assert answer == "news-1 news-2"

    asyncio.run(_run())


def test_handle_same_message_id_chunks_are_concatenated() -> None:
    """同一条 assistant 消息被拆成多个 chunk 时，按到达顺序拼接。"""

    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.chunks = [("msg-1", "前半"), ("msg-1", "后半")]
        try:
            answer = await gateway.handle("打招呼", "oc_chat")
        finally:
            await gateway.shutdown()

        assert answer == "前半后半"

    asyncio.run(_run())


def test_progress_callback_maps_tool_events() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.tool_events = [
            ToolCallEvent(
                session_id="session-1", tool_call_id="c1", title="读文件",
                status="in_progress", phase=ToolCallPhase.STARTED,
            ),
            ToolCallEvent(
                session_id="session-1", tool_call_id="c1", title="读文件",
                status="completed", phase=ToolCallPhase.UPDATED,
            ),
            ToolCallEvent(
                session_id="session-1", tool_call_id="c2", title="查库",
                status="failed", phase=ToolCallPhase.UPDATED,
            ),
        ]
        stages: list[tuple[str, str]] = []

        def _on_event(stage: str, detail: str) -> None:
            stages.append((stage, detail))

        try:
            await gateway.handle("跑一下", "oc_chat", on_event=_on_event)
        finally:
            await gateway.shutdown()

        assert stages == [
            ("tool_start", "读文件"),
            ("tool_done", "读文件"),
            ("agent_error", "查库"),
        ]

    asyncio.run(_run())


def test_handle_attaches_media_blocks_when_route_supports_images() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.image_prompts_supported = True
        bundle = MediaBundle([MediaPart(
            "image",
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}},
            "a.png",
        )])
        try:
            await gateway.handle("这是什么", "oc_chat", media=bundle)
        finally:
            await gateway.shutdown()
        types = [b["type"] for b in supervisor.adapter.blocks[-1]]
        assert types == ["text", "image"]

    asyncio.run(_run())


def test_handle_degrades_media_when_route_lacks_image_support() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.image_prompts_supported = False
        bundle = MediaBundle([MediaPart(
            "image",
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}},
            "a.png",
        )])
        try:
            await gateway.handle("这是什么", "oc_chat", media=bundle)
        finally:
            await gateway.shutdown()
        blocks = supervisor.adapter.blocks[-1]
        # 关键：整轮不能被拒，正文必须还在，且如实说明有图没注入
        assert [b["type"] for b in blocks] == ["text"]
        assert "这是什么" in blocks[0]["text"]
        assert "1 张图片" in blocks[0]["text"]

    asyncio.run(_run())


def test_handle_attachment_only_message_is_not_dropped() -> None:
    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        supervisor.adapter.image_prompts_supported = True
        bundle = MediaBundle([MediaPart(
            "image",
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}},
            "a.png",
        )])
        try:
            answer = await gateway.handle("", "oc_chat", media=bundle)
        finally:
            await gateway.shutdown()
        assert answer == supervisor.adapter.reply_text
        assert supervisor.adapter.prompts  # 空正文但有附件必须照常提交

    asyncio.run(_run())


def test_caller_identity_reaches_supervisor_on_session_open() -> None:
    """建会话时把调用方身份带给 supervisor：MCP 子进程的 env 靠它固化身份。

    身份在上游必须一路透传（main → gateway → supervisor → MCP env）。中间任一
    环节丢掉，表现都不是报错，而是「订阅绑到了错的人 / 绑不上」——只能靠断言守。
    """

    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        try:
            await gateway.handle(
                "订阅 CMDB 工单提醒 devuser", "oc_chat",
                caller={"sender_id": "ou_42", "sender_name": "张三"},
            )
        finally:
            await gateway.shutdown()

        opened = supervisor.calls[0]
        assert opened["chat_id"] == "oc_chat"
        assert opened["caller"] == {"sender_id": "ou_42", "sender_name": "张三"}

    asyncio.run(_run())


def test_caller_is_ignored_when_session_already_open() -> None:
    """会话已存在时不重新建：身份在建会话那一刻就定了，中途换人不能悄悄改绑。"""

    async def _run() -> None:
        gateway, supervisor = _make_gateway()
        try:
            await gateway.handle("第一句", "oc_chat",
                                 caller={"sender_id": "ou_a", "sender_name": "A"})
            await gateway.handle("第二句", "oc_chat",
                                 caller={"sender_id": "ou_b", "sender_name": "B"})
        finally:
            await gateway.shutdown()

        assert len(supervisor.calls) == 1
        assert supervisor.calls[0]["caller"]["sender_id"] == "ou_a"

    asyncio.run(_run())
