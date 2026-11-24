"""通道回调必须回到本仓事件循环（跨循环投递）。

钉住的是一条会吃掉用户答案的边界：`lark_channel` 在自己起的后台线程 + 独立
事件循环上 await 我们注册的 handler；若就地执行，同一条消息的两个阶段就分属
两条循环，共用的 `asyncio.Lock` 一旦出现竞争就抛
`is bound to a different event loop`，异常从完成卡那次刷新冒出去，把已经拿到
的答案整条丢掉（线上实测 3 次，都是 `[locked, waiters:2]`）。

这里不连飞书：直接取 SDK 侧真正被调用的那个 callable（`_channel._handlers`），
换个线程里的新循环去 await 它——SDK 的 bg 线程就是这个形状。

跑法：python -m pytest tests/test_channel_loop.py -q
"""

from __future__ import annotations

import asyncio
import sys

from app.gateway.channel import BotChannel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _registered(ch: BotChannel, event: str):
    """SDK 侧真正被 `_invoke` 调用的那个 callable。"""
    return ch._channel._handlers[event][-1]


async def _call_on_foreign_loop(fn, *args) -> object:
    """在另一条线程的新循环上调用（复刻 SDK 后台线程）。

    必须 `to_thread` 而不是阻塞 `join`：本仓循环被同步阻塞住的话，投递回来的
    回调根本没机会执行——那测的就不是投递，而是「同步等待会不会死锁」。
    真实链路里 SDK 线程 await 我们返回的 Future，主循环一直是活的。
    """
    def _run() -> object:
        async def _main():
            return fn(*args)

        return asyncio.run(_main())

    return await asyncio.to_thread(_run)


def test_message_handler_runs_on_home_loop() -> None:
    async def _run() -> None:
        ch = BotChannel(app_id="cli_fake", app_secret="fake")
        home = asyncio.get_running_loop()
        seen: dict[str, object] = {}

        def handler(msg):
            seen["loop"] = asyncio.get_running_loop()
            seen["msg"] = msg

        ch.on_message(handler)
        await _call_on_foreign_loop(_registered(ch, "message"), "hi")

        assert seen["loop"] is home, "回调必须在本仓循环上执行"
        assert seen["msg"] == "hi"

    asyncio.run(_run())


def test_async_handler_result_is_awaited() -> None:
    """异步画押：SDK 会 await 回调，跨循环投递必须返回 asyncio Future。

    投成 `concurrent.futures.Future` 的话 `_invoke` 不会 await，等于
    fire-and-forget——异常也一起丢了。
    """
    async def _run() -> None:
        ch = BotChannel(app_id="cli_fake", app_secret="fake")
        home = asyncio.get_running_loop()
        seen: dict[str, object] = {}

        async def handler(event):
            seen["loop"] = asyncio.get_running_loop()
            await asyncio.sleep(0)
            return "done"

        ch.on_card_action(handler)
        out = await _registered(ch, "cardAction")("evt")

        assert out == "done"
        assert seen["loop"] is home

    asyncio.run(_run())


def test_sync_handler_is_also_marshalled() -> None:
    """同步回调（on_error）同样要在本仓循环上跑：它可能调 loop 绑定的东西。"""
    async def _run() -> None:
        ch = BotChannel(app_id="cli_fake", app_secret="fake")
        home = asyncio.get_running_loop()
        seen: dict[str, object] = {}

        def handler(err):
            seen["loop"] = asyncio.get_running_loop()
            seen["err"] = err

        ch.on_error(handler)
        await _call_on_foreign_loop(_registered(ch, "error"), RuntimeError("boom"))

        assert seen["loop"] is home
        assert isinstance(seen["err"], RuntimeError)

    asyncio.run(_run())


def test_registration_outside_loop_calls_inline() -> None:
    """事件循环外注册（离线脚本/单测）不投递：没有第二条循环可撞。"""
    ch = BotChannel(app_id="cli_fake", app_secret="fake")
    seen: dict[str, object] = {}

    def handler(msg):
        seen["msg"] = msg

    ch.on_message(handler)
    asyncio.run(_call_on_foreign_loop(_registered(ch, "message"), "hi"))

    assert seen["msg"] == "hi"
    assert ch._home_loop is None
