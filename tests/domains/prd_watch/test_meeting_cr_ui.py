"""会议素材入口 / CR 台账 / UI 对账 / 需求知识台账的单测（fake lark-cli，零网络）。

覆盖 2026-11-16 这一轮扩展的**边界**（不是 happy path）：
1. 妙记摘要与待办在 `artifacts` 下（真实返回形态），顶层同名字段也要认；
2. 会议链接三种形态（妙记 / 纪要 docx / 裸 token）各自路由到正确的读法；
3. CR 台账写失败不得抛（评论已经进了 PRD，台账失败只意味着不能轮询决策）；
4. UI 差异走**与变更同一套**分带阈值，且稿有 PRD 无 → 新增条款；
5. 知识台账的 Story 主键幂等：同一 Story 二次登记不得出现两条。

跑法：python -m pytest tests/domains/prd_watch/test_meeting_cr_ui.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types

from app.config import PrdWatchConfig
from app.domains.prd_watch import meeting
from app.domains.prd_watch.cr_store import (
    STATUS_ADOPTED,
    CrRow,
    CrStore,
    MemoryCrStore,
    snapshot,
)
from app.domains.prd_watch.kb import COLS, ChangeRecord, KnowledgeLedger, StoryRecord
from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL
from app.domains.prd_watch.ui import UiMatcher, points_from_json

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_MINUTE_PAYLOAD = {"data": {"minutes": [{
    "minute_token": "mTk1",
    "title": "申购规则评审",
    "artifacts": {
        "summary": "会上确认赎回确认时点改为 T+1。",
        "todos": [{"content": "改 PRD 赎回条款", "is_done": False}],
    },
}]}}

_DOC_PAYLOAD = {"data": {"document": {
    "document_id": "docNote1",
    "title": "智能纪要：申购规则评审",
    "content": "# 总结\n申购上限从 5 万提到 20 万。\n# 待办\n- 更新 PRD",
}}}


def _patch(obj: object, **attrs: object) -> None:
    for key, value in attrs.items():
        setattr(obj, key, value)


def _stub_run(payload: dict, error: Exception | None = None) -> list[list[str]]:
    calls: list[list[str]] = []

    async def _fake(args: list[str], **_kw: object) -> dict:
        calls.append(list(args))
        if error is not None:
            raise error
        return payload

    _patch(meeting, _run=_fake)
    return calls


# ── 1. 妙记摘要/待办在 artifacts 下 ────────────────────────────────
def test_artifacts_summary_and_todos_are_read() -> None:
    """真实返回是 `data.minutes[0].artifacts.{summary,todos}`。

    只认顶层同名字段时现象是「妙记没有摘要」——排查的人会去查摘要生成没生成，
    而真正原因是读错了位置。
    """
    _stub_run(_MINUTE_PAYLOAD)
    mat = asyncio.run(meeting.from_minute_token("mTk1"))
    assert mat.ok and mat.kind == "minute"
    assert "T+1" in mat.text
    assert "改 PRD 赎回条款" in mat.text
    assert mat.errors == []


def test_title_only_minute_counts_as_material() -> None:
    """只有标题也算素材：判不出变更是识别器的事，通道不提前丢。"""
    _stub_run({"data": {"minutes": [{"minute_token": "mTk2", "title": "对齐会"}]}})
    mat = asyncio.run(meeting.from_minute_token("mTk2"))
    assert mat.ok and "对齐会" in mat.text


def test_permission_error_is_not_reported_as_empty_summary() -> None:
    """他人妙记的权限失败写在响应体 `error` 里，HTTP 层不报错。"""
    _stub_run({"data": {"minutes": [{
        "minute_token": "obcn1", "title": "",
        "error": "No read permission for minute obcn1"}]}})
    mat = asyncio.run(meeting.from_minute_token("obcn1"))
    assert not mat.ok
    assert any("No read permission" in e for e in mat.errors)


# ── 2. 链接形态路由 ────────────────────────────────────────────────
def test_link_kinds_route_to_the_right_reader() -> None:
    assert meeting.classify_link("https://x.feishu.cn/minutes/abc123") == ("minute", "abc123")
    assert meeting.classify_link("https://x.feishu.cn/docx/Zz9") == ("doc", "Zz9")
    assert meeting.classify_link("https://x.feishu.cn/wiki/Wk1") == ("doc", "Wk1")
    assert meeting.classify_link("随便一句话") == ("", "")


def test_doc_link_reads_note_and_keeps_title() -> None:
    calls = _stub_run(_DOC_PAYLOAD)
    mat = asyncio.run(meeting.from_link("https://x.feishu.cn/docx/docNote1"))
    assert mat.ok and mat.note_doc_token == "docNote1"
    assert mat.title.startswith("智能纪要")
    assert calls[0][:2] == ["docs", "+fetch"]


# ── 3. CR 台账 ────────────────────────────────────────────────────
def test_cr_store_memory_merges_partial_saves() -> None:
    """改状态时只带少量字段，save 必须合并而不是把坐标清空。"""
    store = MemoryCrStore()

    async def _run() -> None:
        await store.save(CrRow(cr_id="CR-1", prd_token="docA", comment_id="cm1",
                               title="上限调整", status="pending"))
        await store.set_status("CR-1", STATUS_ADOPTED, decided_by="张三")
        row = await store.get("CR-1")
        assert row is not None
        assert row.status == STATUS_ADOPTED and row.decided_by == "张三"
        assert row.prd_token == "docA" and row.comment_id == "cm1"  # 坐标没被 merge 掉
        snap = await snapshot(store)
        assert snap.total == 1 and snap.pending == 0

    asyncio.run(_run())


def test_cr_store_without_coordinates_is_not_ready() -> None:
    store = CrStore("", "")
    assert store.ready is False
    assert asyncio.run(store.save(CrRow(cr_id="CR-x"))) is False


def test_cr_store_write_failure_does_not_raise() -> None:
    """写台账失败只告警：评论已进 PRD，台账失败不该让整批变更回滚。"""
    store = CrStore("base", "tbl")

    async def _boom(*_a: object, **_kw: object) -> dict:
        raise RuntimeError("base down")

    _patch(store, get=_async_none)
    import app.domains.prd_watch.cr_store as mod

    _patch(mod, upsert_record=_boom)
    assert asyncio.run(store.save(CrRow(cr_id="CR-2"))) is False


async def _async_none(*_a: object, **_kw: object) -> None:
    return None


def test_cr_row_carries_decision_coordinates() -> None:
    row = CrRow(cr_id="CR-3", prd_token="docB", comment_id="cm9", status="pending",
                confidence=0.91, band=BAND_NORMAL)
    body = row.to_record()
    assert body["CR编号"] == "CR-3" and body["PRD文档token"] == "docB"
    assert body["置信度"] == 0.91 and body["决策状态"] == "pending"


# ── 4. UI 差异 ────────────────────────────────────────────────────
_UI_JSON = """{"diffs": [
 {"title": "赎回页缺到账时间", "diff_type": "PRD有稿无",
  "prd_text": "赎回页展示预计到账时间", "quote_text": "赎回页展示预计到账时间",
  "suggested_text": "补充到账时间字段", "evidence": "画板：赎回详情", "confidence": 0.9},
 {"title": "稿子多了个按钮", "diff_type": "稿有PRD无", "ui_text": "一键清仓",
  "suggested_text": "PRD 补写一键清仓入口", "evidence": "画板：持仓页", "confidence": 0.72},
 {"title": "疑似差异", "diff_type": "文案不一致", "ui_text": "确认赎回",
  "prd_text": "提交赎回", "suggested_text": "统一为确认赎回", "evidence": "画板A",
  "confidence": 0.5}
]}"""


def test_ui_points_use_same_bands_as_change_extraction() -> None:
    points, stats = points_from_json(_UI_JSON, min_conf=0.70, flag_conf=0.85)
    assert stats["candidates"] == 3 and stats["dropped"] == 1 and stats["flagged"] == 1
    assert len(points) == 2
    assert points[0].band == BAND_NORMAL and points[0].change_type == "删除条款"
    # 稿有 PRD 无 → 新增条款（复用 easyprd 已有的注释/渲染分支）
    assert points[1].band == BAND_FLAGGED
    assert points[1].is_new_clause and points[1].change_type == "新增条款"


def test_ui_bad_output_is_reported_not_silently_empty() -> None:
    points, stats = points_from_json("抱歉，我不能判断", min_conf=0.7, flag_conf=0.85)
    assert points == [] and "非 JSON" in str(stats.get("error"))


def test_ui_matcher_empty_frame_does_not_call_model() -> None:
    """稿子素材为空时不该白跑一次模型：直接报「没取到文本层」。"""
    matcher = UiMatcher(types.SimpleNamespace(api_key="k", base_url="", max_retries=0,
                                              model="m", timeout_sec=5),
                        PrdWatchConfig())
    points, stats = asyncio.run(matcher.match("", "PRD 正文"))
    assert points == [] and "素材为空" in str(stats.get("error"))


# ── 5. 知识台账 Story 主键幂等 ─────────────────────────────────────
def test_kb_ensure_story_updates_instead_of_duplicating() -> None:
    kb = KnowledgeLedger("baseTok")
    rows: list[dict] = [{"record_id": "recA", COLS["story_id"]: "8821",
                         COLS["story_name"]: "申购调整", COLS["prd"]: ""}]
    upserts: list[tuple[str, str]] = []

    async def _list(*_a: object, **_kw: object) -> list[dict]:
        return list(rows)

    async def _upsert(_base: str, _table: str, fields: dict, *,
                      record_id: str = "", identity: str = "user") -> str:
        upserts.append((record_id, fields.get(COLS["story_name"], "")))
        return record_id or "recNew"

    import app.domains.prd_watch.kb as mod

    _patch(mod, list_records=_list)
    _patch(mod, upsert_record=_upsert)
    rid = asyncio.run(kb.ensure_story({COLS["story_name"]: "申购调整 v2"}, "8821"))
    assert rid == "recA", "同一个 Story 必须更新已有记录"
    assert upserts == [("recA", "申购调整 v2")]


def test_kb_find_story_refuses_ambiguous_name() -> None:
    """关键词命中多条时返回 None——写错需求比漏一条贵得多。"""
    kb = KnowledgeLedger("baseTok")

    async def _list(*_a: object, **_kw: object) -> list[dict]:
        return [
            {"record_id": "recA", COLS["story_id"]: "1", COLS["story_name"]: "RWA货架详情"},
            {"record_id": "recB", COLS["story_id"]: "2", COLS["story_name"]: "RWA货架申购"},
        ]

    import app.domains.prd_watch.kb as mod

    _patch(mod, list_records=_list)
    assert asyncio.run(kb.find_story("RWA货架")) is None
    assert asyncio.run(kb.find_story("1")) is not None      # StoryID 精确命中仍可用


def test_kb_add_change_requires_story_record_id() -> None:
    kb = KnowledgeLedger("baseTok")
    assert asyncio.run(kb.add_change(StoryRecord(story_id="1"),
                                     ChangeRecord(title="x"))) == ""
