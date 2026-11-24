"""本地台账适配器 — 把同步 SQLite 的 `LocalLedger` 对齐成 base 版的 async 接口。

为什么单独成文件：`service.py` 已经 700 行，而这两个适配器只做「接口对齐 +
线程池包装」，不含业务判断。判断留在各消费方（decision poller / card actions /
handler），免得两套存储各长一套逻辑。

SDK 边界（一眼能看懂的分工）：
- `_LocalCrAdapter`   → 冒充 `cr_store.CrStore`（ready / pending / get / save / set_status）
- `_LocalKnowledgeAdapter` → 冒充 `kb.KnowledgeLedger`（stories / find_story /
  changes_for / meetings_for / add_meeting / add_change / ensure_story）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domains.prd_watch.kb import COLS as KB_COLS
from app.domains.prd_watch.kb import ChangeRecord

logger = logging.getLogger("prd_watch.service.local")

TZ = timezone(timedelta(hours=8))


def new_local_ledger(path: str) -> Any:
    from app.domains.prd_watch.local_ledger import LocalLedger

    return LocalLedger(path or None)


class LocalCrAdapter:
    """`LocalLedger` → `CrStore` 形状。"""

    def __init__(self, local: Any) -> None:
        self._local = local

    @property
    def ready(self) -> bool:
        return bool(self._local is not None and self._local.ready)

    async def all_rows(self) -> list[Any]:
        rows = await asyncio.to_thread(self._local.cr_rows)
        return [_cr_row(r) for r in rows]

    async def pending(self) -> list[Any]:
        from app.domains.prd_watch.cr_store import STATUS_PENDING

        rows = await asyncio.to_thread(self._local.cr_rows, STATUS_PENDING)
        out = [_cr_row(r) for r in rows]
        if not out:
            # 旧版本写进来的行可能没有状态；`CrStore.pending` 也把空状态算 pending，
            # 口径不齐会让这些行永远轮询不到（评论有人回了却一直不闭环）。
            out = [r for r in await self.all_rows() if not r.status]
        return out

    async def get(self, cr_id: str) -> Any:
        row = await asyncio.to_thread(self._local.get_cr, cr_id)
        return _cr_row(row) if row else None

    async def save(self, row: Any) -> bool:
        # 只传非空值：`save` 常只带少量字段（改状态时），全量覆盖会把正文抹成空串
        payload = {k: v for k, v in row.__dict__.items()
                   if k != "record_id" and v not in ("", 0.0, 0, None)}
        payload["cr_id"] = row.cr_id
        return bool(await asyncio.to_thread(self._local.save_cr, payload))

    async def set_status(self, cr_id: str, status: str, *,
                         decided_by: str = "", note: str = "") -> bool:
        from app.domains.prd_watch.cr_store import STATUSES

        if status not in STATUSES:
            return False
        who = decided_by or note
        return bool(await asyncio.to_thread(
            self._local.set_cr_status, cr_id, status, decided_by=who))


def _cr_row(row: dict[str, Any]) -> Any:
    """本地行 → `CrRow`（字段名一致，只挑 CrRow 认识的键）。"""
    from app.domains.prd_watch.cr_store import CrRow

    fields = CrRow.__dataclass_fields__
    return CrRow(**{k: v for k, v in row.items() if k in fields})


class LocalKnowledgeAdapter:
    """`LocalLedger` → `KnowledgeLedger` 形状（读多写少，Story 主键幂等）。"""

    def __init__(self, local: Any) -> None:
        self._local = local

    @property
    def ready(self) -> bool:
        return bool(self._local is not None and self._local.ready)

    async def stories(self) -> list[Any]:
        rows = await asyncio.to_thread(self._local.stories)
        return [r.as_record() for r in rows]

    async def find_story(self, key: str) -> Any | None:
        """StoryID 精确 → PRD 链接唯一命中 → 需求名唯一命中；多候选返回 None。"""
        needle = (key or "").strip()
        if not needle:
            return None
        rows = await self.stories()
        exact = [s for s in rows if s.story_id == needle]
        if exact:
            return exact[0]
        by_prd = [s for s in rows if needle in (s.prd_url or "")]
        if len(by_prd) == 1:
            return by_prd[0]
        by_name = [s for s in rows if needle in (s.story_name or "")]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            logger.info("台账命中 %d 条候选（%s），不自动选", len(by_name), needle[:24])
        return None

    async def story_by_record_id(self, record_id: str) -> Any | None:
        # 本地表的 record_id 就是 story_id（见 LocalStory.as_record）
        return await self.find_story(record_id)

    async def changes_for(self, story: Any) -> list[Any]:
        if story is None:
            return []
        rows = await asyncio.to_thread(self._local.changes_for, story.story_id)
        return [ChangeRecord(
            record_id=str(r.get("id") or ""), title=r.get("title") or "",
            content=r.get("content") or "", before=r.get("before_text") or "",
            after=r.get("after_text") or "", source=r.get("source") or "",
            result=r.get("result") or "", date=(r.get("change_date") or "")[:10],
            proposer=r.get("proposer") or "", trace=r.get("trace") or "",
            prd_url=r.get("prd_url") or "",
            story_refs=[story.story_id]) for r in rows]

    async def meetings_for(self, story: Any) -> list[dict[str, Any]]:
        """键名对齐 base 版的列名——`render_story_card` 是按列名取的。"""
        if story is None:
            return []
        rows = await asyncio.to_thread(self._local.meetings_for, story.story_id)
        return [{KB_COLS["meeting_title"]: r.get("meeting_title") or "",
                 KB_COLS["meeting_date"]: r.get("meeting_date") or "",
                 "record_id": str(r.get("id") or "")} for r in rows]

    async def ensure_story(self, fields: dict[str, Any], story_id: str) -> str:
        if not story_id:
            return ""
        from app.domains.prd_watch.local_ledger import LocalStory

        existing = await asyncio.to_thread(self._local.get_story, story_id)
        merged = LocalStory(**(existing.__dict__ if existing else {}))
        merged.story_id = story_id
        # fields 用的是 base 列名，这里翻译一次；只覆盖非空项
        mapped = {
            "story_name": fields.get(KB_COLS["story_name"]),
            "story_url": fields.get(KB_COLS["story_url"]),
            "prd_url": fields.get(KB_COLS["prd"]),
            "biz": fields.get(KB_COLS["biz"]),
            "priority": fields.get(KB_COLS["priority"]),
            "status": fields.get(KB_COLS["status"]),
            "latest_summary": fields.get(KB_COLS["latest_summary"]),
            "latest_meeting": fields.get(KB_COLS["latest_meeting"]),
        }
        for key, value in mapped.items():
            if value:
                setattr(merged, key, value)
        ok = await asyncio.to_thread(self._local.upsert_story, merged)
        return story_id if ok else ""

    async def add_meeting(self, material: Any, story: Any, result: Any) -> str:
        """一场会 → 会议表 + 需求主表最新结论，行为与 base 版一致。

        没有归属 Story 的会议**不落库**：硬挂一条会制造「这条属于谁」的假信息。
        """
        if story is None:
            return ""
        when = getattr(material, "meeting_date", "") or _today()
        await asyncio.to_thread(
            self._local.add_meeting,
            title=getattr(material, "title", "") or "（未命名会议）",
            meeting_id=getattr(material, "minute_token", "") or "",
            date=when,
            organizer=getattr(material, "organizer", "") or "",
            digest=(getattr(material, "text", "") or "")[:1200],
            minutes_url=getattr(material, "minutes_url", "") or "",
            note_url=getattr(material, "notes_url", "") or "",
            story_id=story.story_id)
        changes = list(getattr(result, "changes", []) or [])
        existing = await asyncio.to_thread(self._local.get_story, story.story_id)
        if existing is not None:
            from app.domains.prd_watch.local_ledger import LocalStory
            from app.domains.prd_watch.kb import _summary_line

            latest = (_summary_line(material, changes, when) + "\n"
                      + (existing.latest_summary or "")).strip()[:4000]
            updated = LocalStory(**existing.__dict__)
            updated.latest_summary = latest
            updated.latest_meeting = when
            updated.meeting_count = existing.meeting_count + 1
            await asyncio.to_thread(self._local.upsert_story, updated)
        for cp in changes[:10]:
            await self.add_change(story, _change_of(cp, material, when))
        return story.story_id

    async def add_change(self, story: Any, change: Any) -> str:
        if story is None:
            return ""
        ok = await asyncio.to_thread(
            self._local.add_change,
            story_id=story.story_id, title=change.title, content=change.content,
            before=change.before, after=change.after, source=change.source,
            result=change.result, date=change.date, proposer=change.proposer,
            trace=change.trace, prd_url=change.prd_url)
        return story.story_id if ok else ""


def _change_of(cp: Any, material: Any, when: str) -> ChangeRecord:
    return ChangeRecord(
        title=getattr(cp, "title", ""),
        content=getattr(cp, "suggested_text", "") or getattr(cp, "title", ""),
        before=getattr(cp, "current_text", ""),
        after=getattr(cp, "suggested_text", ""),
        source="会议纪要", result="待确认", date=when,
        proposer=getattr(cp, "speaker", "") or getattr(material, "organizer", ""),
        trace=f"{getattr(material, 'source_label', '')} {when}".strip())


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")
