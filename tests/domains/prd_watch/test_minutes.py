"""easyprd 妙记通道单测（fake lark-cli 调用 + fake 台账，零网络）。

跑法：python -m pytest tests/domains/prd_watch/test_minutes.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch import meeting
from app.domains.prd_watch import minutes as mins
from app.domains.prd_watch.ledger import MemoryLedger, StoryBinding
from app.domains.prd_watch.watcher import PrdWatcher
from app.protocols.lark_cli import LarkCliError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_PAYLOAD = {"changes": [{
    "title": "赎回确认时点 T+0→T+1",
    "quote_text": "赎回支持T+0到账",
    "current_text": "赎回支持T+0到账，节假日顺延。",
    "suggested_text": "赎回确认时点调整为T+1。",
    "change_type": "参数调整", "modules": ["赎回"], "compliance": "需合规确认",
    "evidence_quote": "监管要求 T+1", "speaker": "张三", "scenario": "评审会",
    "rationale": "合规口径变更", "confidence": 0.9,
}]}


@pytest.fixture(autouse=True)
def _reset_seen():
    mins.reset_seen()
    try:
        yield
    finally:
        mins.reset_seen()


def _cfg(**kw: object) -> PrdWatchConfig:
    base: dict[str, object] = {"enabled": True, "dry_run": True,
                               "minutes_story_map": {"mTk1": "8821"}}
    base.update(kw)
    return PrdWatchConfig(**base)  # type: ignore[arg-type]



def _watcher(cfg: PrdWatchConfig) -> tuple[PrdWatcher, MemoryLedger]:
    ledger = MemoryLedger(cfg)
    ledger.add_binding(StoryBinding(story_id="8821", story_title="申购额度调整",
                                    prd_token="docPRD", chat_id="oc_x"))
    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=ledger)
    msg = types.SimpleNamespace(content=json.dumps(_PAYLOAD, ensure_ascii=False))

    class _C:
        async def create(self, **_kw: object) -> object:
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)],
                usage=types.SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    prompt_tokens_details=None, completion_tokens_details=None))

    # _client 声明类型是 AsyncOpenAI，测试给的是鸭子类型替身；走 _patch 绕开赋值检查
    _patch(w._extractor, _client=types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_C())))

    async def _locate(doc: str, quote: str) -> str:
        return "blk1"

    async def _excerpt(doc: str, max_chars: int = 4000) -> str:
        return "赎回支持T+0到账，节假日顺延。"

    _patch(w._anchor, locate=_locate, excerpt=_excerpt)
    return w, ledger


def _patch(obj: object, **attrs: object) -> None:
    """给实例/模块打桩：键非字面量 → mypy 不判赋值类型冲突，ruff 也不报 B010。"""
    for key, value in attrs.items():
        setattr(obj, key, value)


def _stub_run(_monkeypatch, payload: dict | None, error: Exception | None) -> list:
    """替掉 meeting._run（唯一子进程出口），返回调用参数记录。"""
    calls: list[list[str]] = []

    async def _fake(args: list[str], **_kw: object) -> dict:
        calls.append(list(args))
        if error is not None:
            raise error
        return payload or {}

    _patch(meeting, _run=_fake)
    return calls


def test_fetch_minute_text_uses_summary_and_todos(monkeypatch) -> None:
    _stub_run(monkeypatch, {"data": {"items": [{
        "title": "申购规则评审",
        "summary": "会上确认赎回改 T+1。",
        "todos": [{"content": "改 PRD 赎回条款", "owner": "张三"}],
    }]}}, None)
    calls = _stub_run(monkeypatch, {"data": {"items": [{
        "title": "申购规则评审",
        "summary": "会上确认赎回改 T+1。",
        "todos": [{"content": "改 PRD 赎回条款", "owner": "张三"}],
    }]}}, None)
    text, errors, link = asyncio.run(mins.fetch_minute_text("mTk1"))
    assert errors == [] and text
    assert "申购规则评审" in text and "赎回改 T+1" in text
    assert "改 PRD 赎回条款" in text and "张三" in text
    assert link == "", "没配 minutes_base_url 就不该拼出一个链接（域名是租户私有信息）"
    _t2, _e2, link2 = asyncio.run(mins.fetch_minute_text(
        "mTk1", base_url="https://x.feishu.cn/minutes/"))
    assert link2 == "https://x.feishu.cn/minutes/mTk1"
    # 命令面：只取摘要+待办（逐字稿上万字，噪声压过结论），且走 user 身份
    args = calls[0]
    assert args[:2] == ["minutes", "+detail"]
    assert "--summary" in args and "--todo" in args
    assert "--transcript" not in args
    assert args[args.index("--as") + 1] == "user"


def test_missing_scope_error_is_readable(monkeypatch) -> None:
    _stub_run(monkeypatch, None,
              LarkCliError("nope", missing_scopes=["minutes:minute"]))
    text, errors, _link = asyncio.run(mins.fetch_minute_text("mTk1"))
    assert text == "" and errors
    assert "minutes:minute" in errors[0]  # 缺哪个 scope 要说出来，别让人猜


def test_permission_error_in_body_is_not_empty_summary(monkeypatch) -> None:
    """实测：读他人妙记时 HTTP 层不报错，`No read permission` 在 data.minutes[0].error 里。

    不把它当错误上报的话，现象是「妙记没有摘要」——排查的人会去查摘要生成没生成，
    而真正的原因是没权限（需要 minutes +apply-permission）。
    """
    _stub_run(monkeypatch, {"data": {"minutes": [{
        "minute_token": "obcn1", "title": "", "error":
            "No read permission for minute obcn1. Ask the user before running: "
            "minutes +apply-permission --minute-token obcn1 --perm view"}]}}, None)
    text, errors, _link = asyncio.run(mins.fetch_minute_text("obcn1"))
    assert text == "" and errors and "妙记不可读" in errors[0]
    assert "apply-permission" in errors[0]   # 把解法一起说出口


def test_poll_minutes_feeds_same_pipeline(monkeypatch) -> None:
    cfg = _cfg()
    w, ledger = _watcher(cfg)
    _stub_run(monkeypatch, {"data": {"items": [{"summary": "赎回改 T+1"}]}}, None)
    stats = asyncio.run(mins.poll_minutes(cfg, ledger, w))
    assert stats["checked"] == 1 and stats["new"] == 1
    assert stats["changes"] == 1
    # dry_run：识别到但不写台账
    assert ledger._changes == {}


def test_poll_minutes_dedups_second_round(monkeypatch) -> None:
    cfg = _cfg()
    w, _ledger = _watcher(cfg)
    _stub_run(monkeypatch, {"data": {"items": [{"summary": "赎回改 T+1"}]}}, None)
    asyncio.run(mins.poll_minutes(cfg, _ledger2(cfg), w))
    stats2 = asyncio.run(mins.poll_minutes(cfg, _ledger2(cfg), w))
    assert stats2["new"] == 0 and stats2["checked"] == 0


def _ledger2(cfg: PrdWatchConfig) -> MemoryLedger:
    led = MemoryLedger(cfg)
    led.add_binding(StoryBinding(story_id="8821", story_title="申购额度调整",
                                 prd_token="docPRD", chat_id="oc_x"))
    return led


def test_unmapped_minutes_skipped_entirely() -> None:
    """没配 minutes_story_map → 一个请求都不发（不做自动发现是设计红线）。"""
    cfg = PrdWatchConfig(enabled=True, minutes_story_map={})
    w, ledger = _watcher(cfg)
    stats = asyncio.run(mins.poll_minutes(cfg, ledger, w))
    assert stats["checked"] == 0 and "minutes_story_map" in stats["skipped_reason"]


def test_story_without_binding_reports_not_guessed(monkeypatch) -> None:
    cfg = _cfg(minutes_story_map={"mTk1": "9999"})  # 台账里没这个 Story
    w, ledger = _watcher(cfg)
    calls = _stub_run(monkeypatch, {"data": {"items": [{"summary": "x"}]}}, None)
    stats = asyncio.run(mins.poll_minutes(cfg, ledger, w))
    assert stats["errors"] and "9999" in stats["errors"][0]
    assert calls == []  # 没绑定就不该去读妙记


def test_empty_minute_recorded_as_error(monkeypatch) -> None:
    cfg = _cfg()
    w, ledger = _watcher(cfg)
    _stub_run(monkeypatch, {"data": {"items": []}}, None)
    stats = asyncio.run(mins.poll_minutes(cfg, ledger, w))
    errs = " | ".join(stats["errors"])
    assert "没有摘要" in errs, errs
    assert stats["new"] == 0


def test_title_only_minute_still_goes_through(monkeypatch) -> None:
    """只有标题也算素材——判不判得出变更是识别器的事，不在通道里提前丢。"""
    cfg = _cfg()
    w, ledger = _watcher(cfg)
    _stub_run(monkeypatch, {"data": {"items": [{"title": "申购规则评审"}]}}, None)
    stats = asyncio.run(mins.poll_minutes(cfg, ledger, w))
    assert stats["new"] == 1 and stats["errors"] == []


def test_flatten_and_todo_helpers() -> None:
    assert meeting._flatten([{"text": "甲"}, "乙"]) == "甲 乙"
    assert meeting._flatten({"type": "x", "id": "y", "text": "正文"}) == "正文"
    assert meeting._todo_lines([{"content": "改条款", "owner": "张三"}, "", 5]) == ["改条款（张三）"]
    assert len(meeting._todo_lines([{"content": f"t{i}"} for i in range(50)])) == 20
