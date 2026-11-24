"""easyprd 台账 — Story↔PRD↔群绑定 + 变更记录落库（多维表格）。

两条硬约束（设计稿 §3.3 / §6）：
1. 绑定关系**只从台账读**，绝不由消息内容或 LLM 猜测群→Story：绑错就是写错 PRD。
2. 台账坐标没配齐 → 整个能力关闭（`Ledger.disabled_reason` 给出人话原因），
   不用默认值硬跑。

lark-cli `base` 域命令的真实字段名以用户建表为准，故本模块把列名做成可配映射
（`COLUMNS`），列名变更只改这一处；未接通前用 `MemoryLedger` 顶住 watcher 与
渲染的单测，不阻塞主线。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import PrdWatchConfig
from app.protocols.lark_cli import _run

logger = logging.getLogger("prd_watch.ledger")

TZ = timezone(timedelta(hours=8))

# 台账列名 → 语义键。改列名只动这里（渲染与写回共用）。
COLUMNS = {
    "story_id": "StoryID",
    "story_title": "需求名称",
    "story_url": "Story链接",
    "prd_token": "PRD文档token",
    "chat_id": "关联群chat_id",
    "owners": "负责人",
    "cr_id": "CR编号",
    "decision": "决策状态",
    "comment_id": "评论ID",
}

_DECISION_ADOPT = ("采纳", "同意", "通过", "adopt")
_DECISION_IGNORE = ("忽略", "不做", "reject")
_ADJUST = ("调整", "adjust")


def _default_owners() -> dict[str, str]:
    return {}


@dataclass
class StoryBinding:
    """一个群的 PRD 绑定（监听旁路的唯一依据）。"""

    story_id: str = ""
    story_title: str = ""
    story_url: str = ""
    prd_token: str = ""
    chat_id: str = ""
    owners: dict[str, str] = field(default_factory=_default_owners)  # 角色 → 姓名
    owner_emails: dict[str, str] = field(default_factory=dict)       # 角色 → email
    # 绑定来源：meegle=Story 上读到的（标题/PRD/参与人自动），ledger=多维表格手填
    source: str = "ledger"
    status: str = ""
    # 卡片第一行用（「空间名 · Story · #ID」）。台账手填路径没有这两个值就留空，
    # 渲染层会跳过缺项，不会编出假空间名。
    space_name: str = ""
    story_type: str = "Story"
    prd_url: str = ""
    # 聊天里点按钮要看的两项：评论 id（回复/关闭用）与来源标签（卡片「变更依据」用）。
    # 台账（多维表格）路径没有这两列，所以只在内存/自测台路径上有值。
    comment_id: str = ""
    source_ref: str = ""

    @property
    def usable(self) -> bool:
        """缺 PRD token 就没法定位文档，整条绑定作废（不猜）。"""
        return bool(self.chat_id and self.prd_token and self.story_id)


class Ledger:
    """多维表格读写（base 域）。坐标不全时整体禁用。"""

    def __init__(self, config: PrdWatchConfig):
        self._cfg = config

    @property
    def disabled_reason(self) -> str:
        if not self._cfg.ledger_base_token:
            return "未配置 [projects.prd_watch].ledger_base_token"
        if not self._cfg.ledger_change_table:
            return "未配置 ledger_change_table（变更记录表）"
        if not self._cfg.ledger_bind_table:
            return "未配置 ledger_bind_table（需求台账：群↔Story↔PRD 绑定）"
        return ""

    async def binding_for_chat(self, chat_id: str) -> StoryBinding | None:
        """按 chat_id 取**第一个**绑定（单 Story 群的旧语义，保留给回落路径）。

        查不到 = 该群未授权监听（返回 None，不猜）。一群多 Story 时要用
        `bindings_for_chat`，否则第二个 Story 的 PRD 永远不会被评论。
        """
        rows = await self.bindings_for_chat(chat_id)
        return rows[0] if rows else None

    async def bindings_for_chat(self, chat_id: str) -> list[StoryBinding]:
        """一个群的**全部**绑定（一群可对应多个 Story / 多篇 PRD）。

        一场「接口对齐」会同时改货架/申购/赎回/订单/持仓五篇
        PRD。旧模型一群一 Story，会把整场会的结论塞进一篇文档 = 错位评论，
        比漏报更糟。台账里同一 chat_id 有多行是合法形态，全部取回交给识别器判归属。
        """
        reason = self.disabled_reason
        if reason:
            logger.debug("台账禁用: %s", reason)
            return []
        if not chat_id:
            return []
        rows = await self._search(self._cfg.ledger_bind_table,
                                 {COLUMNS["chat_id"]: chat_id})
        out: list[StoryBinding] = []
        for r in rows:
            b = _to_binding(r, chat_id)
            if b.usable and all(b.story_id != x.story_id for x in out):
                out.append(b)
        return out

    async def binding_for_story(self, story_id: str) -> StoryBinding | None:
        """按 StoryID 取绑定（妙记通道用：会议只知 Story，不知群）。"""
        if self.disabled_reason or not story_id:
            return None
        rows = await self._search(self._cfg.ledger_bind_table,
                                 {COLUMNS["story_id"]: story_id})
        for r in rows:
            b = _to_binding(r, str(r.get(COLUMNS["chat_id"]) or ""))
            if b.usable:
                return b
        return None

    async def record_change(self, row: dict[str, object]) -> bool:
        """写一条变更记录（pending）。失败只告警：台账不可达不得挡住评论落地。"""
        reason = self.disabled_reason
        if reason:
            logger.info("台账未启用，变更记录只进日志: %s", row.get(COLUMNS["cr_id"]))
            return False
        ok = await self._upsert(self._cfg.ledger_change_table, row,
                               match_key=COLUMNS["cr_id"])
        if not ok:
            logger.warning("变更记录写台账失败: %s", row.get(COLUMNS["cr_id"]))
        return ok

    async def set_decision(self, cr_id: str, decision: str,
                           note: str = "") -> bool:
        row: dict[str, object] = {COLUMNS["cr_id"]: cr_id, COLUMNS["decision"]: decision}
        if note:
            row["决策备注"] = note[:200]
        return await self._upsert(self._cfg.ledger_change_table, row,
                                  match_key=COLUMNS["cr_id"])

    async def pending_rows(self) -> list[dict[str, object]]:
        """待决记录（回复轮询用）。台账禁用时返回空。"""
        if self.disabled_reason:
            return []
        rows = await self._list(self._cfg.ledger_change_table)
        return [r for r in rows
                if str(r.get(COLUMNS["decision"]) or "").startswith("pending")
                or not r.get(COLUMNS["decision"])]

    # ── base 域调用（统一走 lark_cli._run，勿另起 subprocess）──────
    async def _search(self, table: str, cond: dict[str, object]) -> list[dict]:
        payload = await _run([
            "base", "+record-search", "--base-token", self._cfg.ledger_base_token,
            "--table-id", table, "--filter", json.dumps(cond, ensure_ascii=False),
            "--as", "user", "--format", "json",
        ])
        return _rows(payload)

    async def _list(self, table: str) -> list[dict]:
        payload = await _run([
            "base", "+record-list", "--base-token", self._cfg.ledger_base_token,
            "--table-id", table, "--as", "user", "--format", "json",
        ])
        return _rows(payload)

    async def _upsert(self, table: str, row: dict[str, object],
                      *, match_key: str) -> bool:
        try:
            await _run([
                "base", "+record-upsert", "--base-token", self._cfg.ledger_base_token,
                "--table-id", table, "--content",
                json.dumps(row, ensure_ascii=False),
                "--match-field", match_key,
                "--as", "user", "--format", "json",
            ])
            return True
        except Exception as e:  # LarkCliError 或未预期都按失败处理，不冒泡打断评论
            logger.warning("base upsert 失败(%s/%s): %s", table,
                           row.get(match_key), str(e)[:150])
            return False


class MemoryLedger(Ledger):
    """无台账坐标时的进程内替身：接口一致，数据不落库（灰度期/单测用）。

    存在意义：watcher 与渲染链路不该因为「用户还没建表」就写不了代码。
    代价：重启即丢，所以 dry_run 之外不得长期用它。
    """

    def __init__(self, config: PrdWatchConfig):
        super().__init__(config)
        # chat_id → 该群的**全部**绑定（一群多 Story 是合法形态，见 bindings_for_chat）
        self._bindings: dict[str, list[StoryBinding]] = {}
        self._changes: dict[str, dict[str, object]] = {}

    def add_binding(self, binding: StoryBinding) -> None:
        bucket = self._bindings.setdefault(binding.chat_id, [])
        if all(binding.story_id != b.story_id for b in bucket):
            bucket.append(binding)

    @property
    def disabled_reason(self) -> str:  # 替身不查表，永不因坐标禁用
        return ""

    async def bindings_for_chat(self, chat_id: str) -> list[StoryBinding]:
        return [b for b in self._bindings.get(chat_id, []) if b.usable]

    async def binding_for_chat(self, chat_id: str) -> StoryBinding | None:
        rows = await self.bindings_for_chat(chat_id)
        return rows[0] if rows else None

    async def binding_for_story(self, story_id: str) -> StoryBinding | None:
        for bucket in self._bindings.values():
            for b in bucket:
                if b.story_id == story_id and b.usable:
                    return b
        return None

    async def record_change(self, row: dict[str, object]) -> bool:
        self._changes[str(row.get(COLUMNS["cr_id"]))] = row
        return True

    async def set_decision(self, cr_id: str, decision: str,
                           note: str = "") -> bool:
        row = self._changes.get(cr_id)
        if row is None:
            return False
        row[COLUMNS["decision"]] = decision
        if note:
            row["决策备注"] = note
        return True

    async def pending_rows(self) -> list[dict[str, object]]:
        return [r for r in self._changes.values()
                if str(r.get(COLUMNS["decision"]) or "").startswith("pending")]

    async def get_row(self, cr_id: str) -> dict[str, object] | None:
        return self._changes.get(cr_id)

    def dump(self) -> str:
        return json.dumps(list(self._changes.values()), ensure_ascii=False, indent=1)


def _rows(payload: object) -> list[dict]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else {}
    items = (data or {}).get("items") or (data or {}).get("records") or []
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        fields = it.get("fields") if isinstance(it, dict) else None
        if isinstance(fields, dict):
            out.append(fields)
    return out


def _to_binding(row: dict[str, object], chat_id: str) -> StoryBinding:
    owners = row.get(COLUMNS["owners"])
    parsed: dict[str, str] = {}
    if isinstance(owners, str):
        for seg in re.split(r"[,;、]+", owners):
            if ":" in seg or "：" in seg:
                k, v = re.split(r"[:：]", seg, maxsplit=1)[:2]
                parsed[k.strip()] = v.strip()
    elif isinstance(owners, dict):
        parsed = {str(k): str(v) for k, v in owners.items()}
    return StoryBinding(
        story_id=str(row.get(COLUMNS["story_id"]) or ""),
        story_title=str(row.get(COLUMNS["story_title"]) or ""),
        story_url=str(row.get(COLUMNS["story_url"]) or ""),
        prd_token=str(row.get(COLUMNS["prd_token"]) or ""),
        chat_id=chat_id,
        owners=parsed,
    )


def classify_decision(text: str) -> str:
    """产品回复 → 决策词。识别不了的返回空串（交人工，不自作主张）。"""
    t = str(text or "").strip().lower()
    if not t:
        return ""
    if any(k in t for k in _DECISION_ADOPT):
        return "adopt"
    if any(k in t for k in _ADJUST):
        return "adjust"
    if any(k in t for k in _DECISION_IGNORE):
        return "ignore"
    return ""


def pending_expired(updated_at: datetime | None, ttl_days: int) -> bool:
    """pending 超时判定（工作日按自然日简化；催一次不催二次）。"""
    if updated_at is None:
        return False
    return datetime.now(TZ) - updated_at >= timedelta(days=ttl_days)
