"""easyprd 本地台账 — 三张表落在本机 SQLite（`logs/prd_watch/ledger.db`）。

为什么不用多维表格：这三张表（群↔Story 绑定 / PRD 变更建议 CR / 需求知识地图）
是 **bot 自己的运行状态**，不是给人协同编辑的表格。放本地的好处是零配置、零凭证、
读写不走网络；代价是换机要搬文件，且没有「在飞书上打开就能改」的入口——
需要人工看时 `ledger_dump` 工具会把表读成人话。

## 与 base 版的关系

`ledger.Ledger` / `cr_store.CrStore` / `kb.KnowledgeLedger` 仍保留（接口与单测靠它们），
但**运行时选择权在上面一层**：`service.PrdWatchService` 只要配置了 `local_ledger`
就用本模块，不看 base 坐标。base 坐标继续配着也不会被读，避免同一件事两个事实源。

## 不变量

1. **Story ID 是需求台账的唯一主键**：`story` 建 UNIQUE 索引，`upsert_story` 只更新。
2. **CR 编号唯一**：`cr` 表主键即 `cr_id`，重复写入合并而不追加（决策状态机只能有一行）。
3. **写失败不冒泡**：与 base 版同口径——评论已经进 PRD 了，台账写不进去不回滚那条变更。
4. **时间统一 HKT**：`date` / `at` 都是 `YYYY-MM-DD HH:MM` 或 `YYYY-MM-DD`，不存时区偏移。

表结构见 `_SCHEMA`；加列时走 `_migrate()`（`PRAGMA table_info` 比对后 ALTER），
不要写「删库重建」——那会把用户攒的变更记录一起清掉。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("prd_watch.local_ledger")

TZ = timezone(timedelta(hours=8))

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = PROJECT_DIR / "logs" / "prd_watch" / "ledger.db"

# 状态取值与 cr_store 保持一致（卡片/回执文案都按这几个字判断）
STATUS_PENDING = "pending"
STATUS_ADOPTED = "已采纳"
STATUS_IGNORED = "已忽略"
STATUS_ADJUSTED = "已调整"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS binding (
    story_id    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    story_title TEXT DEFAULT '',
    story_url   TEXT DEFAULT '',
    prd_token   TEXT DEFAULT '',
    prd_url     TEXT DEFAULT '',
    status      TEXT DEFAULT '',
    space_name  TEXT DEFAULT '',
    story_type  TEXT DEFAULT 'Story',
    owners      TEXT DEFAULT '{}',
    source      TEXT DEFAULT 'local',
    updated     TEXT DEFAULT '',
    PRIMARY KEY (story_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_binding_chat ON binding(chat_id);

CREATE TABLE IF NOT EXISTS cr (
    cr_id          TEXT PRIMARY KEY,
    status         TEXT DEFAULT 'pending',
    title          TEXT DEFAULT '',
    change_type    TEXT DEFAULT '',
    current_text   TEXT DEFAULT '',
    suggested_text TEXT DEFAULT '',
    story_id       TEXT DEFAULT '',
    prd_token      TEXT DEFAULT '',
    block_id       TEXT DEFAULT '',
    comment_id     TEXT DEFAULT '',
    chat_id        TEXT DEFAULT '',
    source         TEXT DEFAULT '',
    source_url     TEXT DEFAULT '',
    confidence     REAL DEFAULT 0,
    band           TEXT DEFAULT '',
    diff_kind      TEXT DEFAULT '',
    decided_by     TEXT DEFAULT '',
    decided_at     TEXT DEFAULT '',
    updated        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cr_status ON cr(status);

CREATE TABLE IF NOT EXISTS story (
    story_id       TEXT PRIMARY KEY,
    story_name     TEXT DEFAULT '',
    story_url      TEXT DEFAULT '',
    prd_url        TEXT DEFAULT '',
    biz            TEXT DEFAULT '',
    priority       TEXT DEFAULT '',
    status         TEXT DEFAULT '',
    latest_summary TEXT DEFAULT '',
    latest_meeting TEXT DEFAULT '',
    meeting_count  INTEGER DEFAULT 0,
    parent_ids     TEXT DEFAULT '[]',
    related_ids    TEXT DEFAULT '[]',
    updated        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meeting (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_title  TEXT DEFAULT '',
    meeting_id     TEXT DEFAULT '',
    meeting_date   TEXT DEFAULT '',
    organizer      TEXT DEFAULT '',
    minutes_digest TEXT DEFAULT '',
    minutes_url    TEXT DEFAULT '',
    note_url       TEXT DEFAULT '',
    story_id       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_meeting_story ON meeting(story_id);

CREATE TABLE IF NOT EXISTS change (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id       TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    content        TEXT DEFAULT '',
    before_text    TEXT DEFAULT '',
    after_text     TEXT DEFAULT '',
    source         TEXT DEFAULT '',
    result         TEXT DEFAULT '',
    change_date    TEXT DEFAULT '',
    proposer       TEXT DEFAULT '',
    trace          TEXT DEFAULT '',
    prd_url        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_change_story ON change(story_id);
"""


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def _conn(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL：主进程轮询读、MCP 子进程写，两个进程同时开着同一个库是常态。
    # 默认 rollback journal 下后写的一方会拿到 database is locked。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """列级增量迁移：只加缺的列，不动已有数据。"""
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                logger.info("[local_ledger] %s.%s 补列", table, name)
    conn.commit()


# 建表之后新增的列写这里（形如 {"表": {"列": "TEXT DEFAULT ''"}}）。
# 为什么不直接改 _SCHEMA：`CREATE TABLE IF NOT EXISTS` 对已存在的表不起作用，
# 只改那里等于「新机器有、老机器没有」，是最难查的一类漂移。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {}


@dataclass
class LocalBinding:
    """绑定行（字段与 ledger.StoryBinding 同名，转换只在 `as_binding` 一处）。"""

    story_id: str = ""
    chat_id: str = ""
    story_title: str = ""
    story_url: str = ""
    prd_token: str = ""
    prd_url: str = ""
    status: str = ""
    space_name: str = ""
    story_type: str = "Story"
    owners: dict[str, str] = field(default_factory=dict)
    source: str = "local"
    updated: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.chat_id and self.prd_token and self.story_id)

    def as_binding(self) -> Any:
        """→ `ledger.StoryBinding`（下游渲染/评论只认那个类型）。"""
        from app.domains.prd_watch.ledger import StoryBinding

        return StoryBinding(
            story_id=self.story_id, story_title=self.story_title,
            story_url=self.story_url, prd_token=self.prd_token,
            chat_id=self.chat_id, owners=dict(self.owners), source=self.source,
            status=self.status, space_name=self.space_name,
            story_type=self.story_type, prd_url=self.prd_url)


@dataclass
class LocalStory:
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
    updated: str = ""

    def as_record(self) -> Any:
        """→ `kb.StoryRecord`（`analyze_requirement` 的渲染函数只认它）。

        `record_id` 直接用 story_id：本地表没有 record_id 这层概念，
        而下游只用它做「关联反查」，用主键替代语义一致。
        """
        from app.domains.prd_watch.kb import StoryRecord

        return StoryRecord(
            record_id=self.story_id, story_id=self.story_id,
            story_name=self.story_name, story_url=self.story_url,
            prd_url=self.prd_url, biz=self.biz, priority=self.priority,
            status=self.status, latest_summary=self.latest_summary,
            latest_meeting=self.latest_meeting, meeting_count=self.meeting_count,
            parent_ids=list(self.parent_ids), related_ids=list(self.related_ids))


class LocalLedger:
    """本机 SQLite 台账。所有方法同步执行——sqlite 本地读写是微秒级，
    包成 async 只会让调用方多一层 await 而没有并发收益。调用方用
    `asyncio.to_thread` 包一层即可（见 service）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self._ready = False
        try:
            with _conn(self.path) as conn:
                conn.executescript(_SCHEMA)
                _migrate(conn)
            self._ready = True
        except Exception as e:
            logger.warning("[local_ledger] 初始化失败(%s): %s", self.path, e)

    @property
    def ready(self) -> bool:
        return self._ready

    def _db(self) -> sqlite3.Connection:
        return _conn(self.path)

    # ── 绑定 ─────────────────────────────────────────────────────
    def upsert_binding(self, binding: LocalBinding) -> bool:
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO binding (story_id, chat_id, story_title, story_url,"
                    " prd_token, prd_url, status, space_name, story_type, owners,"
                    " source, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(story_id, chat_id) DO UPDATE SET"
                    " story_title=excluded.story_title, story_url=excluded.story_url,"
                    " prd_token=excluded.prd_token, prd_url=excluded.prd_url,"
                    " status=excluded.status, space_name=excluded.space_name,"
                    " story_type=excluded.story_type, owners=excluded.owners,"
                    " source=excluded.source, updated=excluded.updated",
                    (binding.story_id, binding.chat_id, binding.story_title,
                     binding.story_url, binding.prd_token, binding.prd_url,
                     binding.status, binding.space_name, binding.story_type,
                     json.dumps(binding.owners, ensure_ascii=False),
                     binding.source, binding.updated or _now()))
            return True
        except Exception as e:
            logger.warning("[local_ledger] 绑定写入失败(%s): %s", binding.story_id, e)
            return False

    def bindings_for_chat(self, chat_id: str) -> list[LocalBinding]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM binding WHERE chat_id = ?",
                                (chat_id,)).fetchall()
        return [_binding_of(r) for r in rows]

    def binding_for_story(self, story_id: str) -> LocalBinding | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM binding WHERE story_id = ? ORDER BY updated DESC LIMIT 1",
                (story_id,)).fetchone()
        return _binding_of(row) if row else None

    def all_bindings(self) -> list[LocalBinding]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM binding ORDER BY updated DESC").fetchall()
        return [_binding_of(r) for r in rows]

    def delete_binding(self, story_id: str, chat_id: str = "") -> int:
        with self._db() as conn:
            if chat_id:
                cur = conn.execute(
                    "DELETE FROM binding WHERE story_id = ? AND chat_id = ?",
                    (story_id, chat_id))
            else:
                cur = conn.execute("DELETE FROM binding WHERE story_id = ?",
                                   (story_id,))
            return cur.rowcount

    # ── CR 状态机 ────────────────────────────────────────────────
    def save_cr(self, row: dict[str, Any]) -> bool:
        """按 cr_id 幂等写（只更新传来的字段，其余保持）。"""
        cr_id = str(row.get("cr_id") or "")
        if not cr_id:
            return False
        from app.domains.prd_watch.cr_store import CrRow

        fields = CrRow.__dataclass_fields__
        payload = {k: v for k, v in row.items() if k in fields and k != "record_id"}
        payload["cr_id"] = cr_id
        payload.setdefault("updated", _now())
        cols = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k != "cr_id")
        try:
            with self._db() as conn:
                conn.execute(
                    f"INSERT INTO cr ({cols}) VALUES ({marks}) "
                    f"ON CONFLICT(cr_id) DO UPDATE SET {updates}",
                    tuple(payload.values()))
            return True
        except Exception as e:
            logger.warning("[local_ledger] CR 写入失败(%s): %s", cr_id, e)
            return False

    def cr_rows(self, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cr"
        args: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        with self._db() as conn:
            rows = conn.execute(sql + " ORDER BY updated DESC", args).fetchall()
        return [dict(r) for r in rows]

    def get_cr(self, cr_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM cr WHERE cr_id = ?", (cr_id,)).fetchone()
        return dict(row) if row else None

    def set_cr_status(self, cr_id: str, status: str, *,
                      decided_by: str = "") -> bool:
        at = _now()
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE cr SET status=?, decided_by=?, decided_at=?, updated=?"
                " WHERE cr_id = ?", (status, decided_by, at, at, cr_id))
            if cur.rowcount == 0:
                # 只有状态、没有正文的行（例如只点了按钮没走识别）也允许先落状态
                conn.execute(
                    "INSERT INTO cr (cr_id, status, decided_by, decided_at, updated)"
                    " VALUES (?,?,?,?,?)", (cr_id, status, decided_by, at, at))
        return True

    # ── 需求知识地图 ─────────────────────────────────────────────
    def upsert_story(self, story: LocalStory) -> bool:
        if not story.story_id:
            return False
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO story (story_id, story_name, story_url, prd_url, biz,"
                    " priority, status, latest_summary, latest_meeting, meeting_count,"
                    " parent_ids, related_ids, updated)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(story_id) DO UPDATE SET"
                    " story_name=excluded.story_name, story_url=excluded.story_url,"
                    " prd_url=excluded.prd_url, biz=excluded.biz,"
                    " priority=excluded.priority, status=excluded.status,"
                    " latest_summary=excluded.latest_summary,"
                    " latest_meeting=excluded.latest_meeting,"
                    " meeting_count=excluded.meeting_count,"
                    " parent_ids=excluded.parent_ids, related_ids=excluded.related_ids,"
                    " updated=excluded.updated",
                    (story.story_id, story.story_name, story.story_url, story.prd_url,
                     story.biz, story.priority, story.status, story.latest_summary,
                     story.latest_meeting, story.meeting_count,
                     json.dumps(story.parent_ids, ensure_ascii=False),
                     json.dumps(story.related_ids, ensure_ascii=False),
                     story.updated or _now()))
            return True
        except Exception as e:
            logger.warning("[local_ledger] 需求写入失败(%s): %s", story.story_id, e)
            return False

    def stories(self) -> list[LocalStory]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM story ORDER BY updated DESC").fetchall()
        return [_story_of(r) for r in rows]

    def get_story(self, story_id: str) -> LocalStory | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM story WHERE story_id = ?",
                               (story_id,)).fetchone()
        return _story_of(row) if row else None

    def add_meeting(self, *, title: str, meeting_id: str, date: str,
                    organizer: str, digest: str, minutes_url: str,
                    note_url: str, story_id: str) -> bool:
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO meeting (meeting_title, meeting_id, meeting_date,"
                    " organizer, minutes_digest, minutes_url, note_url, story_id)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (title, meeting_id, date, organizer, digest, minutes_url,
                     note_url, story_id))
            return True
        except Exception as e:
            logger.warning("[local_ledger] 会议写入失败(%s): %s", title[:20], e)
            return False

    def meetings_for(self, story_id: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM meeting WHERE story_id = ? ORDER BY meeting_date DESC",
                (story_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_change(self, *, story_id: str, title: str, content: str, before: str,
                   after: str, source: str, result: str, date: str, proposer: str,
                   trace: str, prd_url: str) -> bool:
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT INTO change (story_id, title, content, before_text,"
                    " after_text, source, result, change_date, proposer, trace, prd_url)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (story_id, title, content, before, after, source, result, date,
                     proposer, trace, prd_url))
            return True
        except Exception as e:
            logger.warning("[local_ledger] 变更记录写入失败(%s): %s", story_id, e)
            return False

    def changes_for(self, story_id: str) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM change WHERE story_id = ? ORDER BY change_date DESC, id DESC",
                (story_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── 运维视图 ─────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        with self._db() as conn:
            out: dict[str, int] = {}
            for table in ("binding", "cr", "story", "meeting", "change"):
                out[table] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            out["cr_pending"] = int(conn.execute(
                "SELECT COUNT(*) FROM cr WHERE status = ?",
                (STATUS_PENDING,)).fetchone()[0])
        return out

    def dump(self, table: str = "binding", limit: int = 20) -> str:
        """人读版导出（`ledger_dump` 工具用）。不认识的表名直接拒绝。"""
        allowed = {"binding", "cr", "story", "meeting", "change"}
        if table not in allowed:
            return f"⚠️ 不支持的表：{table}（可选 {', '.join(sorted(allowed))}）"
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?",
                (max(1, min(limit, 100)),)).fetchall()
        if not rows:
            return f"📭 `{table}` 暂无记录"
        head = " | ".join(rows[0].keys())
        lines = [f"📋 `{table}` 最近 {len(rows)} 条", head, "---"]
        for r in rows:
            lines.append(" | ".join(_short(v) for v in tuple(r)))
        return "\n".join(lines)


# ── 行转换 ───────────────────────────────────────────────────────

def _short(value: Any, limit: int = 40) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _binding_of(row: sqlite3.Row) -> LocalBinding:
    try:
        owners = json.loads(row["owners"] or "{}")
    except (TypeError, ValueError):
        owners = {}
    return LocalBinding(
        story_id=str(row["story_id"] or ""), chat_id=str(row["chat_id"] or ""),
        story_title=str(row["story_title"] or ""),
        story_url=str(row["story_url"] or ""),
        prd_token=str(row["prd_token"] or ""), prd_url=str(row["prd_url"] or ""),
        status=str(row["status"] or ""), space_name=str(row["space_name"] or ""),
        story_type=str(row["story_type"] or "Story"),
        owners=owners if isinstance(owners, dict) else {},
        source=str(row["source"] or "local"), updated=str(row["updated"] or ""))


def _story_of(row: sqlite3.Row) -> LocalStory:
    def _ids(key: str) -> list[str]:
        try:
            val = json.loads(row[key] or "[]")
            return [str(x) for x in val] if isinstance(val, list) else []
        except (TypeError, ValueError):
            return []

    return LocalStory(
        story_id=str(row["story_id"] or ""),
        story_name=str(row["story_name"] or ""),
        story_url=str(row["story_url"] or ""),
        prd_url=str(row["prd_url"] or ""),
        biz=str(row["biz"] or ""), priority=str(row["priority"] or ""),
        status=str(row["status"] or ""),
        latest_summary=str(row["latest_summary"] or ""),
        latest_meeting=str(row["latest_meeting"] or ""),
        meeting_count=int(row["meeting_count"] or 0),
        parent_ids=_ids("parent_ids"), related_ids=_ids("related_ids"),
        updated=str(row["updated"] or ""))


# 让 dataclass 能被测试直接 to_dict
def as_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
