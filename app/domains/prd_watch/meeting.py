"""easyprd 会议素材入口 — 把「一场会」变成一条可识别的讨论素材。

素材优先级（实测结论，别反过来）：
1. **智能纪要 docx**（`note +detail` → `docs +fetch`）：会议自动生成，两种身份都能读；
2. **妙记摘要+待办**（`minutes +detail --summary --todo`）：结构干净，但别人拥有的
   妙记常回 `No read permission`（需 `+apply-permission`，机器人不该替人申请权限）；
3. **会议搜索**（`minutes +search` / `vc +search`）：只在自动扫描时用，且必须带
   关键词——不带过滤条件会被服务端拒。

刻意不做：**不靠会议标题猜需求归属**。标题里出现「申购」不等于这场会改的是申购那篇
PRD；归属要么由调用方显式给 Story（`ingest_meeting`），要么由识别器在候选集内判，
候选集本身来自群↔Story 绑定这条人工台账。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.protocols.lark_cli import LarkCliError, _run

logger = logging.getLogger("prd_watch.meeting")

# 妙记 URL 形如 `/{minutes}/{token}`；纪要/文档形如 `/{docx|wiki}/{token}`
_MINUTE_RE = re.compile(r"/minutes/([A-Za-z0-9]+)")
_DOC_RE = re.compile(r"/(?:docx|wiki|docs)/([A-Za-z0-9]+)")

# 送识别上限：纪要动辄上万字，识别只需要结论/待办/变更信号
MAX_MATERIAL_CHARS = 9000


@dataclass
class MeetingMaterial:
    """一场会能拿到的素材。取不到就留空 + 在 `errors` 里说清为什么。"""

    title: str = ""
    text: str = ""
    minute_token: str = ""
    note_doc_token: str = ""
    meeting_id: str = ""
    meeting_date: str = ""
    organizer: str = ""
    minutes_url: str = ""
    notes_url: str = ""
    kind: str = ""                      # note=智能纪要 / minute=妙记
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())

    @property
    def source_label(self) -> str:
        """卡片「变更依据」那行的文字（写会议标题，不写裸 token）。"""
        what = "智能纪要" if self.kind == "note" else "妙记"
        return f"会议（{what}）：{self.title}" if self.title else f"会议（{what}）"

    @property
    def source_url(self) -> str:
        return self.notes_url or self.minutes_url


def classify_link(link: str) -> tuple[str, str]:
    """链接 → (kind, token)；kind ∈ {"minute", "doc", ""}。"""
    text = (link or "").strip()
    m = _MINUTE_RE.search(text)
    if m:
        return "minute", m.group(1)
    m = _DOC_RE.search(text)
    if m:
        return "doc", m.group(1)
    return "", ""


async def from_minute_token(token: str, *, base_url: str = "") -> MeetingMaterial:
    """妙记 → 摘要 + 待办。"""
    mat = MeetingMaterial(
        minute_token=token,
        minutes_url=f"{base_url.rstrip('/')}/{token}" if base_url else "")
    try:
        payload = await _run([
            "minutes", "+detail", "--minute-tokens", token,
            "--summary", "--todo", "--as", "user", "--format", "json",
        ])
    except LarkCliError as e:
        hint = f"（缺 scope: {','.join(e.missing_scopes)}）" if e.missing_scopes else ""
        mat.errors.append(f"妙记读取失败{hint}: {str(e)[:140]}")
        return _ensure_kind(mat, "minute")
    except Exception as e:
        mat.errors.append(f"妙记读取异常: {str(e)[:140]}")
        return _ensure_kind(mat, "minute")

    data = payload.get("data") or {}
    items = data.get("items") or data.get("minutes") or []
    minute = items[0] if isinstance(items, list) and items else data
    if not isinstance(minute, dict):
        mat.errors.append("妙记返回结构无法识别")
        return _ensure_kind(mat, "minute")
    # 权限失败写在响应体里而不是 HTTP 错误：不读它就会变成「摘要为空」（实测）
    inner = str(minute.get("error") or "").strip()
    if inner:
        mat.errors.append(f"妙记不可读: {inner[:140]}")
        return _ensure_kind(mat, "minute")
    mat.title = str(minute.get("title") or "").strip()
    parts: list[str] = []
    if mat.title:
        parts.append(f"会议：{mat.title}")
    # 摘要/待办在 `artifacts` 下（实测 `data.minutes[0].artifacts.summary`），
    # 顶层同名字段只在旧返回形态里出现——两种都认，别只认一种
    artifacts = minute.get("artifacts") if isinstance(minute.get("artifacts"), dict) else {}
    summary = _flatten(minute.get("summary") or minute.get("ai_summary")
                       or (artifacts or {}).get("summary") or "")
    if summary:
        parts.append("会上结论：" + summary)
    todos = _todo_lines(minute.get("todos") or minute.get("todo")
                        or (artifacts or {}).get("todo")
                        or (artifacts or {}).get("todos"))
    if todos:
        parts.append("待办：" + "；".join(todos))
    mat.text = "\n".join(parts)[:MAX_MATERIAL_CHARS]
    if not mat.text:
        mat.errors.append("妙记没有摘要/待办内容")
    return _ensure_kind(mat, "minute")


async def from_note_doc(token: str, *, base_url: str = "") -> MeetingMaterial:
    """智能纪要 docx → 正文（同一场会优先试这条，可读性最高）。"""
    mat = MeetingMaterial(
        note_doc_token=token, kind="note",
        notes_url=f"{base_url.rstrip('/')}/{token}" if base_url else "")
    try:
        payload = await _run([
            "docs", "+fetch", "--doc", token, "--as", "user",
            "--doc-format", "markdown", "--format", "json",
        ])
    except Exception as e:
        mat.errors.append(f"纪要读取失败: {str(e)[:140]}")
        return mat
    doc = (payload.get("data") or {}).get("document") or {}
    content = str(doc.get("content") or "").strip()
    if not content:
        mat.errors.append("纪要正文为空")
        return mat
    mat.title = str(doc.get("title") or "").strip() or _first_line(content)
    mat.text = content[:MAX_MATERIAL_CHARS]
    return mat


async def from_meeting_id(meeting_id: str, *, minutes_base_url: str = "",
                          notes_base_url: str = "") -> MeetingMaterial:
    """meeting_id → `vc +detail` 拿 note_id / minute_token → 取素材。"""
    try:
        payload = await _run([
            "vc", "+detail", "--meeting-ids", meeting_id, "--as", "user",
            "--format", "json",
        ])
    except Exception as e:
        mat = MeetingMaterial(meeting_id=meeting_id)
        mat.errors.append(f"会议详情读取失败: {str(e)[:140]}")
        return mat
    data = payload.get("data") or {}
    items = data.get("items") or data.get("meetings") or []
    item = items[0] if isinstance(items, list) and items else data
    if not isinstance(item, dict):
        return MeetingMaterial(meeting_id=meeting_id,
                               errors=["会议详情结构无法识别"])
    note_doc = await note_doc_token(str(item.get("note_id") or ""))
    minute_token = str(item.get("minute_token") or "")
    mat = MeetingMaterial(meeting_id=meeting_id)
    if note_doc:
        mat = await from_note_doc(note_doc, base_url=notes_base_url)
        mat.meeting_id = meeting_id
    if not mat.ok and minute_token:
        fallback = await from_minute_token(minute_token, base_url=minutes_base_url)
        fallback.meeting_id = meeting_id
        fallback.errors = mat.errors + fallback.errors
        mat = fallback
    if not mat.title:
        mat.title = str(item.get("topic") or "")
    mat.organizer = str(item.get("organizer") or item.get("host") or "")
    mat.meeting_date = str(item.get("start_time") or "")[:10]
    return mat


async def from_link(link: str, *, minutes_base_url: str = "",
                    notes_base_url: str = "") -> MeetingMaterial:
    """用户贴什么就读什么：妙记链接 / 纪要 docx 链接 / 裸 token。

    裸 token 判不出形态：先按妙记读，读不到再按 docx 读——两条都失败时 errors
    里会有两个原因，而不是一句「读不到」。
    """
    kind, token = classify_link(link)
    if kind == "minute":
        return await from_minute_token(token, base_url=minutes_base_url)
    if kind == "doc":
        return await from_note_doc(token, base_url=notes_base_url)
    raw = (link or "").strip()
    if not raw:
        return MeetingMaterial(errors=["没有给出链接或 token"])
    first = await from_minute_token(raw, base_url=minutes_base_url)
    if first.ok:
        return first
    second = await from_note_doc(raw, base_url=notes_base_url)
    second.errors = first.errors + second.errors
    return second


async def search_minutes(query: str, *, start: str = "", end: str = "",
                         limit: int = 10) -> list[dict[str, str]]:
    """按关键词搜妙记。`--query` 与 `--owner-ids` 是**与**关系，所以 owner 不在这里叠。

    返回 [] 表示搜不到或调用失败（失败已记 warning）；调用方据此说「没搜到相关会议」。
    """
    if not (query or "").strip():
        return []
    args = ["minutes", "+search", "--query", query.strip(),
            "--as", "user", "--format", "json",
            "--page-size", str(max(1, min(30, limit)))]
    if start:
        args += ["--start", start]
    if end:
        args += ["--end", end]
    try:
        payload = await _run(args)
    except Exception as e:
        logger.warning("妙记搜索失败(%s): %s", query[:20], str(e)[:140])
        return []
    items = ((payload.get("data") or {}).get("items") or [])
    out: list[dict[str, str]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        meta = it.get("meta_data") or {}
        out.append({
            "token": str(it.get("token") or ""),
            "title": _clean_title(str(it.get("display_info") or "")),
            "url": str(meta.get("app_link") or ""),
            "desc": str(meta.get("description") or ""),
        })
    return out


async def note_doc_token(note_id: str) -> str:
    """note_id → 纪要文档 token（`note +detail` 的参数是**单数**，一次一个）。"""
    if not note_id:
        return ""
    try:
        payload = await _run(["note", "+detail", "--note-id", note_id,
                              "--as", "user", "--format", "json"])
    except Exception as e:
        logger.info("纪要 token 查询失败(%s): %s", note_id, str(e)[:100])
        return ""
    data = payload.get("data") or {}
    item = data.get("note") or data
    if not isinstance(item, dict):
        return ""
    return str(item.get("note_doc_token") or item.get("doc_token") or "")


def _ensure_kind(mat: MeetingMaterial, kind: str) -> MeetingMaterial:
    mat.kind = kind
    return mat


def _clean_title(display: str) -> str:
    """`display_info` 形如「标题<h>词</h>\\n<b>关键词:</b>…」→ 第一行标题。"""
    first = (display or "").split("\n", 1)[0]
    return re.sub(r"<[^>]+>", "", first).strip()


def _first_line(content: str) -> str:
    for line in (content or "").splitlines():
        text = line.strip()
        if text:
            return text.lstrip("#").strip()[:40]
    return ""


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value if v)
    if isinstance(value, dict):
        return " ".join(_flatten(v) for k, v in value.items()
                        if k not in ("type", "id"))
    return ""


def _todo_lines(todos: Any) -> list[str]:
    items = todos if isinstance(todos, list) else ([todos] if todos else [])
    out: list[str] = []
    for t in items:
        if isinstance(t, dict):
            text = _flatten(t.get("content") or t.get("text") or "")
            owner = _flatten(t.get("owner") or t.get("assignee") or "")
            if text:
                out.append(text + (f"（{owner}）" if owner else ""))
        elif isinstance(t, str) and t.strip():
            out.append(" ".join(t.split()))
    return out[:20]
