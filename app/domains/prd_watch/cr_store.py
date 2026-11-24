"""easyprd 变更记录台账（独立 CR 表）— 决策状态机的持久化层。

为什么与需求知识台账的「变更记录」表分开：那是**时序归档**（这个需求怎么一步步
演化的，人读的叙事），这里是**待办状态机**（这条评论等谁拍板、评论 id 在哪、超时
要不要催）。两者字段、生命周期、写频率都不同，混在一张表里会让「已采纳」这种状态
被写进叙事字段，台账就不好读了。

降级：CR 表坐标没配 → 用 `MemoryCrStore`（进程内，重启即丢）。**不静默**：
`ready` 为 False 时 snapshot 会说出来，状态面板会提示「决策闭环只在内存里」。

写失败一律不抛：评论已经写进 PRD 了，台账写失败只意味着"无法轮询决策"，
不该让这条变更回滚或让整批失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.protocols.lark_base import cell_text, list_records, upsert_record

logger = logging.getLogger("prd_watch.cr")

TZ = timezone(timedelta(hours=8))

# CR 表列名（本能力自建表，列名即契约；改这里要同步 docs/features/easyprd-prd-change.md）
COLUMNS: dict[str, str] = {
    "cr_id": "CR编号",
    "status": "决策状态",
    "decided_by": "决策人",
    "decided_at": "决策时间",
    "change_type": "变更类型",
    "title": "变更标题",
    "current_text": "变更前原文",
    "suggested_text": "建议表述",
    "story_id": "StoryID",
    "prd_token": "PRD文档token",
    "block_id": "块ID",
    "comment_id": "评论ID",
    "chat_id": "关联群chat_id",
    "source": "来源",
    "source_url": "来源链接",
    "confidence": "置信度",
    "band": "置信分带",
    "diff_kind": "差异来源",
    "updated": "更新时间",
}

# 决策状态取值（与 card_actions / decisions 文案里的中文状态一致）
STATUS_PENDING = "pending"
STATUS_ADOPTED = "已采纳"
STATUS_IGNORED = "已忽略"
STATUS_ADJUSTED = "已调整"

STATUSES = (STATUS_PENDING, STATUS_ADOPTED, STATUS_IGNORED, STATUS_ADJUSTED)


@dataclass
class CrRow:
    """CR 表一行。字段与 COLUMNS 同名，避免各层各叫一套。"""

    cr_id: str = ""
    status: str = STATUS_PENDING
    title: str = ""
    change_type: str = ""
    current_text: str = ""
    suggested_text: str = ""
    story_id: str = ""
    prd_token: str = ""
    block_id: str = ""
    comment_id: str = ""
    chat_id: str = ""
    source: str = ""
    source_url: str = ""
    confidence: float = 0.0
    band: str = ""
    diff_kind: str = ""          # 群讨论 / 会议 / UI对账 —— 决定卡片文案与阈值来源
    decided_by: str = ""
    decided_at: str = ""
    updated: str = ""
    record_id: str = ""

    @classmethod
    def from_record(cls, row: dict[str, Any]) -> CrRow:
        def _f(key: str) -> str:
            return cell_text(row.get(COLUMNS[key]))

        try:
            confidence = float(_f("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            cr_id=_f("cr_id"), status=_f("status") or STATUS_PENDING,
            title=_f("title"), change_type=_f("change_type"),
            current_text=_f("current_text"), suggested_text=_f("suggested_text"),
            story_id=_f("story_id"), prd_token=_f("prd_token"),
            block_id=_f("block_id"), comment_id=_f("comment_id"),
            chat_id=_f("chat_id"), source=_f("source"), source_url=_f("source_url"),
            confidence=confidence, band=_f("band"), diff_kind=_f("diff_kind"),
            decided_by=_f("decided_by"), decided_at=_f("decided_at"),
            updated=_f("updated"), record_id=str(row.get("record_id") or ""),
        )

    def to_record(self) -> dict[str, Any]:
        """→ base 的字段 map（只写有值且非只读的列）。"""
        body: dict[str, Any] = {
            COLUMNS["cr_id"]: self.cr_id,
            COLUMNS["status"]: self.status,
            COLUMNS["title"]: self.title,
            COLUMNS["story_id"]: self.story_id,
        }
        optional = {
            "change_type": self.change_type, "current_text": self.current_text,
            "suggested_text": self.suggested_text, "prd_token": self.prd_token,
            "block_id": self.block_id, "comment_id": self.comment_id,
            "chat_id": self.chat_id, "source": self.source,
            "source_url": self.source_url, "band": self.band,
            "diff_kind": self.diff_kind, "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }
        for key, value in optional.items():
            if value:
                body[COLUMNS[key]] = value
        if self.confidence:
            body[COLUMNS["confidence"]] = round(self.confidence, 2)
        body[COLUMNS["updated"]] = self.updated or datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        return body


class CrStore:
    """CR 台账读写门面。坐标不全时整体降级为内存（`ready` 为 False）。"""

    def __init__(self, base_token: str = "", table_id: str = "",
                 identity: str = "user"):
        self.base_token = base_token
        self.table_id = table_id
        self.identity = identity

    @property
    def ready(self) -> bool:
        return bool(self.base_token and self.table_id)

    async def all_rows(self) -> list[CrRow]:
        if not self.ready:
            return []
        rows = await list_records(self.base_token, self.table_id,
                                  identity=self.identity)
        return [CrRow.from_record(r) for r in rows if r.get("record_id")]

    async def pending(self) -> list[CrRow]:
        return [r for r in await self.all_rows() if r.status == STATUS_PENDING
                or not r.status]

    async def get(self, cr_id: str) -> CrRow | None:
        for row in await self.all_rows():
            if row.cr_id == cr_id:
                return row
        return None

    async def save(self, row: CrRow) -> bool:
        """按 CR 编号幂等写入（先查 record_id 再写）。失败返回 False，不抛。"""
        if not self.ready:
            return False
        if not row.record_id:
            try:
                existing = await self.get(row.cr_id)
            except Exception as e:      # 读失败不阻断写：宁可多一行也不丢状态
                logger.info("CR 表查询失败(%s)，按新建写: %s", row.cr_id, str(e)[:100])
                existing = None
            row.record_id = existing.record_id if existing else ""
        try:
            await upsert_record(self.base_token, self.table_id, row.to_record(),
                                record_id=row.record_id, identity=self.identity)
            return True
        except Exception as e:
            logger.warning("CR 表写入失败(%s): %s", row.cr_id, str(e)[:160])
            return False

    async def set_status(self, cr_id: str, status: str, *, decided_by: str = "",
                         note: str = "") -> bool:
        """改状态 + 记决策人与时间。状态先落、再回写评论（顺序见 decisions.py）。"""
        if status not in STATUSES:
            return False
        row = CrRow(cr_id=cr_id, status=status,
                    decided_by=decided_by or note,
                    decided_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))
        if not row.decided_by:
            row.decided_by = note
        return await self.save(row)


class MemoryCrStore(CrStore):
    """无 CR 表坐标时的替身（进程内；重启即丢，snapshot 里会明说）。"""

    def __init__(self) -> None:
        super().__init__("", "")
        self._rows: dict[str, CrRow] = {}

    @property
    def ready(self) -> bool:
        return True

    async def all_rows(self) -> list[CrRow]:
        return list(self._rows.values())

    async def get(self, cr_id: str) -> CrRow | None:
        return self._rows.get(cr_id)

    async def save(self, row: CrRow) -> bool:
        old = self._rows.get(row.cr_id)
        if old is not None:
            # 合并而不是覆盖：save 常只带少量字段（改状态时）
            merged = CrRow(**{**old.__dict__, **{k: v for k, v in row.__dict__.items()
                                                 if v not in ("", 0.0, None)}})
            self._rows[row.cr_id] = merged
        else:
            self._rows[row.cr_id] = row
        return True

    async def set_status(self, cr_id: str, status: str, *, decided_by: str = "",
                         note: str = "") -> bool:
        if status not in STATUSES:
            return False
        row = self._rows.get(cr_id) or CrRow(cr_id=cr_id)
        row.status = status
        row.decided_by = decided_by or note
        row.decided_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        row.updated = row.decided_at
        self._rows[cr_id] = row
        return True


@dataclass
class PendingSnapshot:
    """状态面板/巡检用的只读快照。"""

    total: int = 0
    pending: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    ready: bool = True


async def snapshot(store: CrStore) -> PendingSnapshot:
    rows = await store.all_rows()
    snap = PendingSnapshot(total=len(rows), ready=store.ready)
    for r in rows:
        snap.by_status[r.status or STATUS_PENDING] = snap.by_status.get(
            r.status or STATUS_PENDING, 0) + 1
        if r.status == STATUS_PENDING or not r.status:
            snap.pending += 1
        kind = r.diff_kind or "群讨论"
        snap.by_kind[kind] = snap.by_kind.get(kind, 0) + 1
    return snap
