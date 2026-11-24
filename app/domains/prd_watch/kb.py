"""需求知识台账（req-knowledge-ledger）— Story 为主键的需求知识地图。

来源：本能力原先是一份独立 Skill（`.user_skills/req-knowledge-ledger`），
内化进 feishu-agent-platform 后由三张多维表格承载：

- `需求台账`：一个 Story 一条（业务线/优先级/状态/PRD/最新结论摘要/自关联关系）
- `会议纪要`：一场会一条（标题/日期/组织者/结论摘要/妙记与纪要链接）
- `变更记录`：一条变更一个时间点（变更前/后/来源/决策结果/来源追溯）

与 easyprd 的分工（**别混**）：
- easyprd 判「哪篇 PRD 哪一段该改 → 写文档评论 → 卡片拍板」，产出是可执行的 CR；
- 本台账判「这个需求讨论到哪了 / 变过几次 / 和哪些需求是一伙的」，产出是知识地图。
  两者的读取可以共用台账表，但判定标准与写入目标不同：easyprd 的 CR 有评论 id
  与决策状态机，台账的变更记录是时序归档，不进 PRD 评论。

硬约束（沿用原 Skill，逐条有据）：
1. Story ID 是唯一主键，一个 Story 不允许两条记录；
2. 拿不准的关联**列候选让人确认**，禁止凭标题强行关联（写错需求比漏关联贵得多）；
3. 最新结论摘要按时间倒序、最新在前；
4. 采集全自动但保留确定性字段：信号弱 → 决策结果 = 待确认，不冒充定论；
5. 变更必须留来源追溯，能在原文里回溯。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.protocols.lark_base import cell_text, first_url, list_records, upsert_record

TZ = timezone(timedelta(hours=8))

logger = logging.getLogger("prd_watch.kb")

# 三张表的默认 table_id（examplecorp 空间实测）。配置里可覆盖。
DEFAULT_TABLES = {
    "story": "tblZkrb5oetJKC9G",
    "meeting": "tbl393iJIflX44ll",
    "change": "tblXldbvV1Htbx3N",
}

# 列名 → 语义键。列名以线上表为准（+field-list 实测），改列名只动这里。
COLS = {
    "story_id": "Story ID",
    "story_name": "需求名称",
    "story_url": "Story链接",
    "prd": "PRD任务文档",
    "biz": "业务线",
    "priority": "优先级",
    "status": "状态",
    "latest_summary": "最新结论摘要",
    "latest_meeting": "最新会议日期",
    "meeting_count": "会议次数",
    "parent": "父版本需求",
    "related": "相关需求",
    "created": "Story创建日期",
    "updated": "最近更新时间",
    # 会议纪要表
    "meeting_title": "会议标题",
    "meeting_id": "会议ID",
    "meeting_date": "会议日期",
    "organizer": "组织者",
    "minutes_digest": "会议结论摘要",
    "minutes_url": "妙记链接",
    "note_url": "智能纪要链接",
    "meeting_story": "关联需求",
    # 变更记录表
    "change_title": "变更主题",
    "change_content": "变更内容",
    "change_before": "变更前",
    "change_after": "变更后",
    "change_source": "变更来源",
    "change_result": "决策结果",
    "change_date": "发生日期",
    "change_proposer": "提出人",
    "change_trace": "来源追溯",
    "change_prd": "关联PRD",
    "change_story": "关联需求",
}


@dataclass
class StoryRecord:
    """需求台账的一行（字段名与线上表一致，缺项留空不编）。"""

    record_id: str = ""
    story_id: str = ""
    story_name: str = ""
    story_url: str = ""
    prd_url: str = ""
    biz: str = ""
    priority: str = ""
    status: str = ""
    latest_summary: str = ""
    latest_meeting: str = ""
    meeting_count: int = 0
    parent_ids: list[str] = field(default_factory=list)
    related_ids: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.story_id and self.record_id)


@dataclass
class ChangeRecord:
    """变更记录的一行（时序归档，一个时间点一条）。"""

    record_id: str = ""
    title: str = ""
    content: str = ""
    before: str = ""
    after: str = ""
    source: str = ""
    result: str = ""
    date: str = ""
    proposer: str = ""
    trace: str = ""
    prd_url: str = ""
    story_refs: list[str] = field(default_factory=list)


def _link_ids(value: Any) -> list[str]:
    """link 单元格 → record_id 列表（base 返回 `[{"id": "rec_xxx"}]`）。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
    return out


def _to_story(row: dict[str, Any]) -> StoryRecord:
    count = row.get(COLS["meeting_count"])
    try:
        meeting_count = int(float(cell_text(count) or 0))
    except (TypeError, ValueError):
        meeting_count = 0
    return StoryRecord(
        record_id=str(row.get("record_id") or ""),
        story_id=cell_text(row.get(COLS["story_id"])),
        story_name=cell_text(row.get(COLS["story_name"])),
        story_url=first_url(row.get(COLS["story_url"])),
        prd_url=first_url(row.get(COLS["prd"])),
        biz=cell_text(row.get(COLS["biz"])),
        priority=cell_text(row.get(COLS["priority"])),
        status=cell_text(row.get(COLS["status"])),
        latest_summary=cell_text(row.get(COLS["latest_summary"])),
        latest_meeting=cell_text(row.get(COLS["latest_meeting"]))[:10],
        meeting_count=meeting_count,
        parent_ids=_link_ids(row.get(COLS["parent"])),
        related_ids=_link_ids(row.get(COLS["related"])),
    )


def _to_change(row: dict[str, Any]) -> ChangeRecord:
    return ChangeRecord(
        record_id=str(row.get("record_id") or ""),
        title=cell_text(row.get(COLS["change_title"])),
        content=cell_text(row.get(COLS["change_content"])),
        before=cell_text(row.get(COLS["change_before"])),
        after=cell_text(row.get(COLS["change_after"])),
        source=cell_text(row.get(COLS["change_source"])),
        result=cell_text(row.get(COLS["change_result"])),
        date=cell_text(row.get(COLS["change_date"]))[:10],
        proposer=cell_text(row.get(COLS["change_proposer"])),
        trace=cell_text(row.get(COLS["change_trace"])),
        prd_url=first_url(row.get(COLS["change_prd"])),
        story_refs=_link_ids(row.get(COLS["change_story"])),
    )


class KnowledgeLedger:
    """需求知识台账的读写门面（读多写少，写入一律走「先查再写」）。"""

    def __init__(self, base_token: str, tables: dict[str, str] | None = None,
                 identity: str = "user"):
        self.base_token = base_token
        self.tables = dict(DEFAULT_TABLES)
        self.tables.update({k: v for k, v in (tables or {}).items() if v})
        self.identity = identity

    @property
    def ready(self) -> bool:
        return bool(self.base_token)

    # ── 读 ───────────────────────────────────────────────────────
    async def stories(self) -> list[StoryRecord]:
        rows = await list_records(self.base_token, self.tables["story"],
                                  identity=self.identity)
        out = [_to_story(r) for r in rows]
        return [s for s in out if s.story_id]

    async def find_story(self, key: str) -> StoryRecord | None:
        """按 StoryID / 需求名关键词 / PRD 链接找**唯一**一条。

        多候选时返回 None 并记日志：调用方要的是"确定是哪条"，
        随便挑一条会把变更写到别的需求上（原 Skill 硬约束 2）。
        """
        needle = (key or "").strip()
        if not needle:
            return None
        rows = await self.stories()
        exact = [s for s in rows if s.story_id == needle]
        if exact:
            return exact[0]
        by_prd = [s for s in rows
                  if needle and needle in s.prd_url]
        if len(by_prd) == 1:
            return by_prd[0]
        by_name = [s for s in rows if needle and needle in s.story_name]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            logger.info("台账命中 %d 条候选（%s），不自动选：%s", len(by_name),
                        needle[:24], [s.story_id for s in by_name][:5])
        return None

    async def changes_for(self, story: StoryRecord | None) -> list[ChangeRecord]:
        """该需求的变更记录（按发生日期倒序）。"""
        if story is None or not story.record_id:
            return []
        rows = await list_records(self.base_token, self.tables["change"],
                                  identity=self.identity)
        out = [_to_change(r) for r in rows
               if story.record_id in _link_ids(r.get(COLS["change_story"]))]
        return sorted(out, key=lambda c: c.date, reverse=True)

    async def meetings_for(self, story: StoryRecord | None) -> list[dict[str, Any]]:
        """该需求的会议纪要行（原样返回，字段名以线上表为准，按会议日期倒序）。"""
        if story is None or not story.record_id:
            return []
        rows = await list_records(self.base_token, self.tables["meeting"],
                                  identity=self.identity)
        out = [r for r in rows
               if story.record_id in _link_ids(r.get(COLS["meeting_story"]))]
        return sorted(out, key=lambda r: cell_text(r.get(COLS["meeting_date"])),
                      reverse=True)

    async def story_by_record_id(self, record_id: str) -> StoryRecord | None:
        if not record_id:
            return None
        for s in await self.stories():
            if s.record_id == record_id:
                return s
        return None

    # ── 写 ───────────────────────────────────────────────────────
    async def ensure_story(self, fields: dict[str, Any],
                           story_id: str) -> str:
        """Story 主键幂等：已存在则更新（只更新传入的字段），否则新建。

        为什么不让上层自己判断：Story ID 是唯一主键这条不变量只能有一处执行，
        散到调用方迟早出现两条同 Story 的记录（原 Skill 硬约束 1）。
        """
        if not self.ready or not story_id:
            return ""
        existing = await self.find_story(story_id)
        body = dict(fields)
        body[COLS["story_id"]] = story_id
        try:
            return await upsert_record(
                self.base_token, self.tables["story"], body,
                record_id=existing.record_id if existing else "",
                identity=self.identity)
        except Exception as e:
            logger.warning("需求台账写入失败(%s): %s", story_id, str(e)[:160])
            return ""

    async def add_meeting(self, material: Any, story: StoryRecord | None,
                          result: Any) -> str:
        """一场会写进「会议纪要」表 + 回写需求主表的会议统计。

        - 会议记录只在**有绑定 Story** 时挂 link：没有归属的会议进不了知识地图，
          硬挂一条会制造「这条会议属于谁」的假信息；
        - 需求主表的「最新结论摘要」按时间倒序拼接、最新在前（原 Skill 硬约束 3）；
        - 变更记录只从识别结果里取，且**不写决策状态机**——那是 easyprd CR 表的事。
        """
        if not self.ready:
            return ""
        when = material.meeting_date or datetime.now(TZ).strftime("%Y-%m-%d")
        body: dict[str, Any] = {
            COLS["meeting_title"]: material.title or "（未命名会议）",
            COLS["meeting_date"]: when,
            COLS["organizer"]: material.organizer,
        }
        if material.minute_token:
            body[COLS["meeting_id"]] = material.minute_token
        if material.minutes_url:
            body[COLS["minutes_url"]] = material.minutes_url
        if material.notes_url:
            body[COLS["note_url"]] = material.notes_url
        digest = (material.text or "")[:1200]
        if digest:
            body[COLS["minutes_digest"]] = digest
        if story is not None and story.record_id:
            body[COLS["meeting_story"]] = [{"id": story.record_id}]
        try:
            record_id = await upsert_record(self.base_token, self.tables["meeting"],
                                            body, identity=self.identity)
        except Exception as e:
            logger.warning("会议纪要写入失败(%s): %s", material.title[:20], str(e)[:160])
            record_id = ""
        if story is None or not story.record_id:
            return record_id
        changes = list(getattr(result, "changes", []) or [])
        summary_line = _summary_line(material, changes, when)
        latest = (summary_line + "\n" + (story.latest_summary or "")).strip()[:4000]
        try:
            await upsert_record(self.base_token, self.tables["story"], {
                COLS["latest_summary"]: latest,
                COLS["latest_meeting"]: when,
                COLS["meeting_count"]: story.meeting_count + 1,
                COLS["updated"]: datetime.now(TZ).strftime("%Y-%m-%d"),
            }, record_id=story.record_id, identity=self.identity)
        except Exception as e:
            logger.warning("需求主表回写失败(%s): %s", story.story_id, str(e)[:160])
        for cp in changes[:10]:
            await self.add_change(story, ChangeRecord(
                title=getattr(cp, "title", ""),
                content=getattr(cp, "suggested_text", "") or getattr(cp, "title", ""),
                before=getattr(cp, "current_text", ""),
                after=getattr(cp, "suggested_text", ""),
                source="会议纪要", result="待确认", date=when,
                proposer=getattr(cp, "speaker", "") or material.organizer,
                trace=f"{material.source_label} {when}".strip(),
                prd_url=story.prd_url,
            ))
        return record_id

    async def add_change(self, story: StoryRecord | None,
                         change: ChangeRecord) -> str:
        """给需求挂一条变更记录（时序归档；不做近义去重——去重是 easyprd 的事）。"""
        if not self.ready or story is None or not story.record_id:
            return ""
        body: dict[str, Any] = {
            COLS["change_title"]: change.title,
            COLS["change_content"]: change.content,
            COLS["change_before"]: change.before,
            COLS["change_after"]: change.after,
            COLS["change_date"]: change.date,
            COLS["change_proposer"]: change.proposer,
            COLS["change_trace"]: change.trace,
            COLS["change_story"]: [{"id": story.record_id}],
        }
        if change.source:
            body[COLS["change_source"]] = change.source
        if change.result:
            body[COLS["change_result"]] = change.result
        if change.prd_url:
            body[COLS["change_prd"]] = change.prd_url
        try:
            return await upsert_record(self.base_token, self.tables["change"], body,
                                       identity=self.identity)
        except Exception as e:
            logger.warning("变更记录写入失败(%s): %s", story.story_id, str(e)[:160])
            return ""


def _summary_line(material: Any, changes: list[Any], when: str) -> str:
    """`【最新】11/16 会议标题：要点。` —— 时间倒序拼接时占一行。

    只用识别出的标题（不改写原文事实，原 Skill 硬约束 4）；识别没出变更时
    写会议标题本身，而不是编一句"本次会议讨论了…"。
    """
    date_bit = when[5:].replace("-", "/") if len(when) >= 10 else when
    title = str(getattr(material, "title", "") or "会议")
    points = "、".join(str(getattr(c, "title", "") or "").strip()
                       for c in changes[:3] if getattr(c, "title", ""))
    body = f"【最新】{date_bit} {title}：{points}。" if points else f"【最新】{date_bit} {title}。"
    return body


def render_story_card(story: StoryRecord, changes: list[ChangeRecord],
                      meetings: list[dict[str, Any]]) -> str:
    """一个需求的知识地图（人读版）：状态 → PRD → 最新结论 → 变更时序 → 历次会议。"""
    lines = [
        f"📚 需求知识地图 · {story.story_name or story.story_id}",
        f"Story：#{story.story_id}" + (f"（{story.status}）" if story.status else ""),
    ]
    facts = [f"{k}：{v}" for k, v in
             (("业务线", story.biz), ("优先级", story.priority)) if v]
    if facts:
        lines.append("｜".join(facts))
    if story.prd_url:
        lines.append(f"PRD：{story.prd_url}")
    elif story.story_url:
        lines.append(f"Story：{story.story_url}")
    if story.latest_summary:
        lines += ["", "【最新结论】", story.latest_summary]
    if changes:
        lines += ["", f"【变更历史】共 {len(changes)} 条（新→旧）"]
        for c in changes[:8]:
            head = f"· {c.date}｜{c.title}"
            if c.result:
                head += f"〔{c.result}〕"
            lines.append(head)
            if c.before or c.after:
                lines.append(f"    {c.before or '（未记录）'} → {c.after or '（未记录）'}")
            if c.trace:
                lines.append(f"    来源：{c.trace}")
        if len(changes) > 8:
            lines.append(f"    …另有 {len(changes) - 8} 条见台账")
    if meetings:
        lines += ["", f"【历次会议】共 {len(meetings)} 场"]
        for m in meetings[:6]:
            title = cell_text(m.get(COLS["meeting_title"]))
            date = cell_text(m.get(COLS["meeting_date"]))[:10]
            lines.append(f"· {date}｜{title or '（无标题）'}")
    if story.parent_ids or story.related_ids:
        lines += ["", "【需求关系】",
                  f"父版本 {len(story.parent_ids)} 条 / 相关 {len(story.related_ids)} 条"
                  "（点开台账记录可跳转）"]
    return "\n".join(lines)
