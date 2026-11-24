"""easyprd 旁路准入闸门单测 — 验 main.on_message 的分流判据（不连飞书）。

这块是本需求风险最高的地方：白名单群放开 require_mention 后，同事的闲聊第一次会
进入本进程。**漏一条判据就是把机器人变成广播喇叭**（去答同事对话），或反过来
把管理员的 /dev 命令吞掉。四个判据都得钉住。

跑法：python -m pytest tests/domains/prd_watch/test_bypass_gate.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.config import PrdWatchConfig
from app.domains.prd_watch.watcher import PrdWatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_CHAT = "oc_gate"
_OTHER = "oc_other_group"


@pytest.fixture(autouse=True)
def _restore_main_globals():
    """本套件会改写 app.main 的模块级单例，跑完必须还原，否则污染其他测试。"""
    import app.main as m

    saved = (m._prd_watch, m._aggregator, m._capability_ingest, m._messaging_cfg)
    try:
        yield
    finally:
        (m._prd_watch, m._aggregator, m._capability_ingest,
         m._messaging_cfg) = saved



class _RecordingWatcher:
    def __init__(self, chats: list[str]) -> None:
        self._chats = chats
        self.got: list[dict] = []

    def watch_chats(self) -> list[str]:
        return list(self._chats)

    async def handle_message(self, **kw: object) -> None:
        self.got.append(dict(kw))


class _FakeAggregator:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, msg: object, process: object) -> None:
        self.submitted.append(getattr(msg, "body_text", "?"))



def _patch(obj: object, **attrs: object) -> None:
    """改写 app.main 的模块级单例。

    走 **kwargs 而非逐行赋值：mypy 会按声明类型（MessageAggregator | None 等）
    拒绝替身，而测试要给的正是替身；键非字面量同时避开 ruff B010。
    """
    for key, value in attrs.items():
        setattr(obj, key, value)


def _setup(watch_chats: list[str], *, enabled: bool = True):
    """返回 (main 模块, watcher 替身, aggregator 替身)。"""
    import app.main as m

    w = _RecordingWatcher(watch_chats)
    agg = _FakeAggregator()
    svc = types.SimpleNamespace(
        active=enabled and bool(watch_chats), watcher=w)
    _patch(m, _prd_watch=svc, _aggregator=agg, _capability_ingest=None,
           _messaging_cfg=types.SimpleNamespace(ack_receipt=False))
    return m, w, agg


def _msg(text: str, chat_id: str, *, mentioned: bool) -> object:
    # body_text/content_text 是 aggregator.extract_text 的真实取值路径
    # （不是 msg.text），造假消息必须走这两个字段，否则正文一律为空
    return types.SimpleNamespace(
        body_text=text,
        content_text=text,
        safe_content_text=text,
        id="om_1",
        conversation=types.SimpleNamespace(chat_id=chat_id),
        sender=types.SimpleNamespace(open_id="ou_z", display_name="张三"),
        mentioned_bot=mentioned,
        resources=[],
        reply=None,
        raw={},
    )


def _run(m, msg) -> None:
    asyncio.run(m.on_message(msg, types.SimpleNamespace()))


async def _noop_send(*_a: object, **_kw: object) -> None:
    return None


def test_unmentioned_in_watch_group_goes_to_bypass_only() -> None:
    m, w, agg = _setup([_CHAT])
    _run(m, _msg("额度还是改成20万吧", _CHAT, mentioned=False))
    assert len(w.got) == 1 and w.got[0]["chat_id"] == _CHAT
    assert agg.submitted == [], "旁路消息绝不能进编排"


def test_mentioned_message_still_routes_normally() -> None:
    m, w, agg = _setup([_CHAT])
    _run(m, _msg("把刚才讨论的变更整理进 PRD", _CHAT, mentioned=True))
    assert w.got == []
    assert agg.submitted == ["把刚才讨论的变更整理进 PRD"]


def test_other_groups_unaffected() -> None:
    m, w, agg = _setup([_CHAT])
    _run(m, _msg("中午吃什么", _OTHER, mentioned=False))
    assert w.got == [] and agg.submitted == ["中午吃什么"]


def test_slash_command_not_swallowed_by_bypass() -> None:
    """/dev 由 _process_batch 的 Step 0.2 拦截；旁路跑在它前面，必须放行。"""
    m, w, agg = _setup([_CHAT])
    _run(m, _msg("/dev 加个日志字段", _CHAT, mentioned=False))
    assert w.got == [] and agg.submitted == ["/dev 加个日志字段"]


def test_fix_reply_intercepted_before_bypass() -> None:
    """点过「修正」的人回的那句文案，必须被回填而不是当成新讨论吞掉。"""
    m, w, _agg = _setup([_CHAT])
    applied: list[tuple[str, str, str]] = []

    def _pending(open_id: str) -> str:
        return "CR-x" if open_id == "ou_z" else ""

    async def _apply(chat_id: str, open_id: str, text: str) -> str:
        applied.append((chat_id, open_id, text))
        return "🔄 已调整"

    _patch(m, _channel=types.SimpleNamespace(send=_noop_send))
    _patch(m._prd_watch, card_actions=types.SimpleNamespace(
        pending_fix=_pending, apply_fix=_apply))
    _run(m, _msg("改为：默认选中票据", _CHAT, mentioned=False))
    assert applied == [(_CHAT, "ou_z", "改为：默认选中票据")]
    assert w.got == [], "修正回填是终态，不得再进讨论窗口"


def test_fix_reply_works_when_service_inactive() -> None:
    """自测台在 enabled=false / 无授权群时也能点按钮，回填判据必须同样放开。"""
    m, _w, agg = _setup([_CHAT], enabled=False)
    applied: list[str] = []

    async def _apply(chat_id: str, open_id: str, text: str) -> str:
        applied.append(text)
        return "🔄 已调整"

    _patch(m, _channel=types.SimpleNamespace(send=_noop_send))
    _patch(m._prd_watch, card_actions=types.SimpleNamespace(
        pending_fix=lambda o: "CR-x", apply_fix=_apply))
    _run(m, _msg("改为：默认选中票据", _CHAT, mentioned=False))
    assert applied == ["改为：默认选中票据"]
    assert agg.submitted == []


def test_disabled_service_leaves_pipeline_untouched() -> None:
    m, w, agg = _setup([_CHAT], enabled=False)
    _run(m, _msg("额度改成20万", _CHAT, mentioned=False))
    assert w.got == []
    # 未启用时不在白名单群的行为不变：这条仍会进编排（准入也未曾放开）
    assert agg.submitted == ["额度改成20万"]


def test_service_none_is_safe() -> None:
    m, _w, agg = _setup([_CHAT])
    _patch(m, _prd_watch=None)
    _run(m, _msg("随便聊聊", _CHAT, mentioned=False))
    assert agg.submitted == ["随便聊聊"]


def test_bypass_failure_does_not_raise_into_channel() -> None:
    """监听器抛错不得冒泡（消息处理入口抛异常会丢整条链路的回执）。"""
    m, w, agg = _setup([_CHAT])

    async def _boom(**_kw: object) -> None:
        raise RuntimeError("台账不可达")

    _patch(w, handle_message=_boom)
    _run(m, _msg("额度改20万", _CHAT, mentioned=False))
    assert agg.submitted == []  # 仍然不进编排，但没抛出异常


def test_watcher_config_watch_chats_passthrough() -> None:
    """PrdWatchConfig → PrdWatcher.watch_chats 的透传（旁路判据的数据源）。"""
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT, _OTHER])
    router = types.SimpleNamespace(
        api_key="k", base_url="http://x", model="m", max_retries=0,
        timeout_sec=5, temperature=0.0, max_tokens=None, top_p=None,
        llm_sites={}, sampling_params=lambda site: {"temperature": 0.0})
    w = PrdWatcher(cfg, router)  # type: ignore[arg-type]
    assert w.watch_chats() == [_CHAT, _OTHER]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")
