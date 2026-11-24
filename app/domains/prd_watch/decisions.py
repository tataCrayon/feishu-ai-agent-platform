"""easyprd 决策闭环 — 轮询评论回复 → 采纳/忽略/调整 → 回写评论 + 台账 + 群通知。

为什么轮询不用事件：评论回复事件需要应用侧订阅「评论和回复」，本机 lark-cli 登录态
还没有该能力，且 SDK 的 on_comment 在本仓从未接通过（设计稿 §3.3 决策 5）。轮询用
现成 `drive +list-replies`，确定性高，代价是分钟级延迟——决策本来就是人在群里看到
卡片后才回，不等即时。

红线（设计稿 §6）：本期「采纳」只回写状态并通知，**不改 PRD 正文**。正文由产品自己
贴，或等 Q3 签收后另开权限。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.cr_store import STATUS_PENDING, CrStore
from app.domains.prd_watch.ledger import COLUMNS, Ledger, classify_decision
from app.domains.prd_watch.render import StoryMeta, build_decision_card
from app.protocols.lark_cli import LarkCli, LarkCliError

logger = logging.getLogger("prd_watch.decisions")

TZ = timezone(timedelta(hours=8))

# 决策 → 评论回复文案 + 台账状态 + 群通知
_MARKS: dict[str, tuple[str, str, str]] = {
    "adopt": ("✅ 已采纳 —— PRD 正文请产品/研发按建议更新（我不代改正文）。",
              "已采纳", "✅ PRD变更已采纳"),
    "ignore": ("❌ 已忽略 —— 本条不再跟进。", "已忽略", "🚫 PRD变更已忽略"),
    "adjust": ("🔄 已调整 —— 以产品改后的正文为准，本条关闭。",
               "已调整", "🔄 PRD变更已按调整处理"),
}

_DECISION_KEYS = (COLUMNS["cr_id"], COLUMNS["decision"], COLUMNS["comment_id"],
                  "PRD文档token", "StoryID", "关联群chat_id")


@dataclass
class DecisionOutcome:
    """一次轮询的结果（单测断言与日志都靠它，不靠 print）。"""

    checked: int = 0
    applied: list[str] = field(default_factory=list)
    pending: int = 0
    reminded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"待决 {self.checked} 条：已定 {len(self.applied)}、"
                f"仍 pending {self.pending}、超时提醒 {len(self.reminded)}"
                + (f"，异常 {len(self.errors)}" if self.errors else ""))


class DecisionPoller:
    """台账 pending → 读评论回复 → 落决策。"""

    def __init__(
        self,
        config: PrdWatchConfig,
        ledger: Ledger,
        *,
        lark: LarkCli | None = None,
        notify: Callable[[str, Any], Awaitable[Any]] | None = None,
        binding: Any = None,
        cr_store: Any = None,
    ):
        self._cfg = config
        self._ledger = ledger
        # CR 表优先（新形态，带评论 id / 块 id / 分带）；旧台账作为灰度期兜底。
        self._cr = cr_store or CrStore(config.cr_base_token, config.cr_table)
        self._lark = lark or LarkCli()
        self._notify = notify
        # Story↔PRD参与人 的事实源（Meegle 装配时由 service 注入）。决策回执卡片要显示
        # 「空间 · Story · #ID」与可点标题，而**变更记录行里没有这些列**（多维表格的列名
        # 由管理员建表决定，硬加列会让整行 upsert 失败——比少一行卡片严重得多），
        # 所以按 StoryID 现查绑定；查不到就退化成「#ID」，不编假空间名。
        self._binding = binding
        self._seen_replies: dict[str, set[str]] = {}  # cr_id → 已处理 reply_id
        self._reminded: set[str] = set()              # 超时只催一次

    async def _meta(self, row: dict) -> StoryMeta:
        story_id = str(row.get(COLUMNS["story_id"]) or "")
        if self._binding is not None and story_id:
            try:
                b = await self._binding.binding_for_story(story_id)
            except Exception as e:
                logger.info("[prd_watch] 决策卡片取绑定失败(%s)，退化为纯 ID", str(e)[:80])
                b = None
            if b is not None:
                return StoryMeta(
                    story_id=b.story_id, story_title=b.story_title,
                    story_url=b.story_url, space_name=b.space_name,
                    story_type=b.story_type, status=b.status)
        return StoryMeta(story_id=story_id)

    async def poll_once(self) -> DecisionOutcome:
        out = DecisionOutcome()
        # 两处 pending 都读，按 CR 编号去重：CR 表里的行优先（带齐坐标），
        # 旧台账里 CR 表没有的再补上——灰度期不能因为换了库就漏掉老评论。
        rows: list[dict] = []
        seen: set[str] = set()
        for cr in await self._pending_cr_rows():
            rows.append(cr)
            seen.add(str(cr.get(COLUMNS["cr_id"])))
        for legacy in await self._ledger.pending_rows():
            cr_id = str(legacy.get(COLUMNS["cr_id"]) or "")
            if cr_id and cr_id not in seen:
                rows.append(legacy)
                seen.add(cr_id)
        out.checked = len(rows)
        for row in rows:
            cr_id = str(row.get(COLUMNS["cr_id"]) or "")
            comment_id = str(row.get(COLUMNS["comment_id"]) or "")
            doc = str(row.get("PRD文档token") or "")
            if not cr_id:
                continue
            if not comment_id or not doc:
                # 评论 id 或文档坐标缺一个就无从回查：计数并跳过，不猜
                out.pending += 1
                continue
            try:
                replies = await self._lark.list_comment_replies(
                    doc, comment_id, identity=self._cfg.comment_identity)
            except LarkCliError as e:
                out.errors.append(f"{cr_id}: {str(e)[:80]}")
                continue
            except Exception as e:
                out.errors.append(f"{cr_id}: {str(e)[:80]}")
                continue

            seen = self._seen_replies.setdefault(cr_id, set())
            hit = False
            for r in replies:
                rid = r.get("reply_id") or ""
                if not rid or rid in seen:
                    seen.add(rid)
                    continue
                seen.add(rid)
                if r.get("is_body"):
                    # 评论本体是我们自己写的，正文里写着「采纳 → 回复『采纳』」的操作说明；
                    # 当成回复读会把每一个 pending 在首轮全部判成已采纳。跳过，但仍记 seen。
                    continue
                decision = classify_decision(r.get("text", ""))
                if not decision:
                    continue
                await self._apply(decision, row, r, out)
                hit = True
                break
            if not hit:
                out.pending += 1
                await self._maybe_remind(row, out)
        return out

    async def _pending_cr_rows(self) -> list[dict]:
        """CR 表里的 pending → 旧行形态（下游按 `PRD文档token` / `评论ID` 取坐标）。"""
        if not self._cr.ready:
            return []
        try:
            entries = await self._cr.pending()
        except Exception as e:
            logger.info("[prd_watch] CR 表读取失败，仅用旧台账轮询: %s", str(e)[:120])
            return []
        return [{
            COLUMNS["cr_id"]: r.cr_id,
            COLUMNS["status"]: r.status or STATUS_PENDING,
            COLUMNS["comment_id"]: r.comment_id,
            "PRD文档token": r.prd_token,
            "StoryID": r.story_id,
            "关联群chat_id": r.chat_id,
            "变更标题": r.title,
            "更新时间": r.updated,
        } for r in entries]

    async def _apply(self, decision: str, row: dict, reply: dict,
                     out: DecisionOutcome) -> None:
        cr_id = str(row.get(COLUMNS["cr_id"]) or "")
        doc = str(row.get("PRD文档token") or "")
        comment_id = str(row.get(COLUMNS["comment_id"]) or "")
        mark, ledger_state, _title = _MARKS[decision]

        # 1) 台账先定状态：评论回写失败也不能让状态停在 pending（否则下轮重复处理）
        note = f"{reply.get('text', '')[:60]}（{reply.get('user_id', '')}）"
        ok = await self._cr.set_status(cr_id, ledger_state, note=note)
        if not ok:
            ok = await self._ledger.set_decision(cr_id, ledger_state, note=note)
        if not ok:
            out.errors.append(f"{cr_id}: 台账状态未更新")

        # 2) 评论回复标记 + 忽略/调整顺带 resolve
        #    resolve 必须用**评论 id**：拿回复的 reply_id 去调会报 "fail"（实测），
        #    而回执里只有 reply_id，所以评论 id 从列表接口带出来。
        try:
            await self._lark.reply_comment(doc, comment_id, mark,
                                           identity=self._cfg.comment_identity)
            if decision != "adopt":
                await self._lark.resolve_comment(
                    doc, str(reply.get("id") or comment_id),
                    identity=self._cfg.comment_identity)
        except Exception as e:
            out.errors.append(f"{cr_id}: 评论回写失败 {str(e)[:60]}")

        # 3) 群通知（采纳的措辞必须点明「正文还没改」，别让研发以为已生效）
        chat_id = str(row.get("关联群chat_id") or "")
        if self._notify is not None and chat_id:
            body = build_decision_card(
                decision=decision, cr_id=cr_id,
                title=str(row.get("变更标题") or ""),
                meta=await self._meta(row),
                confirmer=str(reply.get("user_id") or ""))
            try:
                await self._notify(chat_id, body)
            except Exception as e:
                out.errors.append(f"{cr_id}: 群通知失败 {str(e)[:60]}")
        out.applied.append(cr_id)
        logger.info("[prd_watch] %s → %s（回复人 %s）", cr_id, ledger_state,
                    reply.get("user_id") or "?")

    async def _maybe_remind(self, row: dict, out: DecisionOutcome) -> None:
        if self._cfg.decision_ttl_days <= 0 or self._notify is None:
            return
        cr_id = str(row.get(COLUMNS["cr_id"]) or "")
        if cr_id in self._reminded:
            return
        updated = _row_time(row)
        if updated is None:
            return
        if datetime.now(TZ) - updated < timedelta(days=self._cfg.decision_ttl_days):
            return
        self._reminded.add(cr_id)
        chat_id = str(row.get("关联群chat_id") or "")
        if not chat_id:
            return
        try:
            await self._notify(chat_id, build_decision_card(
                decision="timeout", cr_id=cr_id,
                title=str(row.get("变更标题") or ""),
                meta=await self._meta(row), confirmer="",
                extra=f"已挂 {self._cfg.decision_ttl_days} 天未确认，"
                      "PRD 上那条评论回「采纳/忽略/调整：说明」就行。"))
            out.reminded.append(cr_id)
        except Exception as e:
            out.errors.append(f"{cr_id}: 提醒失败 {str(e)[:60]}")


def _row_time(row: dict) -> datetime | None:
    raw = row.get("创建时间") or row.get("更新时间")
    if isinstance(raw, (int, float)) and raw > 0:
        ts = raw / 1000 if raw > 1e11 else raw
        try:
            return datetime.fromtimestamp(ts, TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            # CR 表里存「YYYY-MM-DD HH:MM」（人读格式，方便在表里看），
            # fromisoformat 认不出这种「空格分隔 + 缺秒」的形态
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw[:19], fmt).replace(tzinfo=TZ)
                except ValueError:
                    continue
            return None
    return None
