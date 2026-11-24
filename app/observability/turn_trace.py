"""会话轨迹 — 一轮对话「用户问了什么、怎么处理的、答得怎么样」的单文件全文。

## 为什么单独一层（与 tasks/ 的分工）

`logs/tasks/{date}.jsonl` 是**指标面**：每轮一行、字段固定，供 Dashboard 聚合
（次数/成功占比/耗时分布）。它刻意只留 200 字预览——单行 JSON 塞进完整工具
入参和回复全文会让整个文件不可扫、也扛不住按天翻查。

迭代优化要的是**过程面**：同一轮里模型调了哪些工具、每个工具给了什么参数、
返回了什么、最终答了什么。这些只在内存里存活一次（`_Turn` 随会话丢弃），
出了问题连复现都没法复现。所以每轮另落一个 `.md`：

    logs/turns/{date}/{task_id}.md      # 给人看（也会喂给模型做复盘）
    logs/turns/{date}.index.jsonl       # 给程序筛（一行一条，字段与 tasks 对齐）

`.md` 给人读，`.index.jsonl` 用来机器筛「调过某工具 / 某能力 / 耗时超过 X」的轮次
再逐个开 `.md`。两者 `task_id` 同值，能互相跳。

## 为什么写 Markdown 而不是再写一份 JSON

复盘时读的是「模型说了什么、调了什么、拿到什么」。JSON 里这些是转义过的长字符串，
人眼和 grep 都不友好；Markdown 的代码块天然隔离工具输出，`grep -l "read_doc"`
就能定位到具体哪几轮碰过读文档能力。

## 失败语义

本模块**不抛**：轨迹是观测面，写不进去不该让用户少拿一条回复（同
`app/dsh/task_record.py`）。写入失败只记 warning，由日志落盘兜住。

隐私边界：正文按需截断（单工具输出 4000 字、整轮 200 万字节上限），
不做内容审查——这是内网自有机器人，原文是排障必需。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("observability.turn_trace")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
TURNS_LOG_DIR = PROJECT_DIR / "logs" / "turns"

# 单个工具输出在轨迹里的保留上限：模型一次 web_search 能返回几万字，
# 全留下会让单文件涨到 MB 级，翻查时反而找不到重点。
_TOOL_OUTPUT_LIMIT = 4000
# 工具入参上限（adapter 已按 2000 截断一次，这里兜的是别的来源）。
_TOOL_INPUT_LIMIT = 2000


@dataclass
class TurnStep:
    """一次工具调用的记录。

    Attributes:
        name: 工具名（dsh 的 title，如 `skill` / `mcp__feishu__send_message`）。
        status: started / completed / failed。
        input: 模型给的入参（JSON 文本，已截断）。
        output: 工具返回的文本（已截断）。
        at: 事件时间（秒级，够定位先后顺序）。
    """

    name: str
    status: str = "started"
    input: str = ""
    output: str = ""
    at: str = ""


@dataclass
class TurnTrace:
    """一轮对话的完整轨迹。

    Attributes:
        task_id: 与 TaskRecord / trace 同值，三处可互跳。
        chat_id / sender_id / sender_name: 谁在哪儿问的。
        query: 用户原文（含被 debounce 合并进来的几条）。
        memory: 本轮注入的长期记忆（空串 = 没有召回）。
        steps: 工具调用序列。
        reply: 最终答复全文。
        status: success / error / no_response。
        error: 异常摘要。
        duration_ms: 本轮耗时。
    """

    task_id: str
    query: str = ""
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    memory: str = ""
    steps: list[TurnStep] = field(default_factory=list)
    reply: str = ""
    status: str = "success"
    error: str = ""
    duration_ms: float = 0.0
    ts: str = field(default_factory=lambda: datetime.now(TZ).isoformat())

    def tools(self) -> list[str]:
        """去重后的工具名序列（索引筛选用）。"""
        seen: list[str] = []
        for step in self.steps:
            if step.name and step.name not in seen:
                seen.append(step.name)
        return seen


class TurnTraceRecorder:
    """按天分目录落 Markdown 轨迹 + 单行索引。"""

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or TURNS_LOG_DIR

    def _day_dir(self, ts: str = "") -> Path:
        day = (ts or datetime.now(TZ).isoformat())[:10]
        return self._dir / day

    def write(self, trace: TurnTrace) -> None:
        """落一轮轨迹（Markdown + 索引行）。失败只记日志。"""
        if not trace.task_id:
            return
        safe_id = "".join(c for c in trace.task_id if c.isalnum() or c in "-_")[:80]
        if not safe_id:
            return
        day_dir = self._day_dir(trace.ts)
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            (day_dir / f"{safe_id}.md").write_text(_render_markdown(trace), encoding="utf-8")
            # 索引按月切：按天切会让 `--days 30` 要开 30 个文件，按月一次读完
            month = trace.ts[:7]
            with open(self._dir / f"{month}.index.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(_index_row(trace), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[turn_trace] 写入失败（不影响回复）: %s", e)

    def read(self, task_id: str, day: str = "") -> str | None:
        """读回一轮轨迹原文；day 省略时回看最近 3 天（跨零点的那条会落在前一天）。"""
        safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")[:80]
        if not safe_id:
            return None
        if day:
            path = self._dir / day / f"{safe_id}.md"
            return path.read_text(encoding="utf-8") if path.is_file() else None
        now = datetime.now(TZ)
        for offset in range(3):
            day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            path = self._dir / day / f"{safe_id}.md"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    def prune(self, keep_days: int = 90) -> int:
        """删除超过 keep_days 的天目录，返回删掉的轮次数。

        轨迹会持续增长（每轮一个 .md）；不清理的话一年后 `logs/turns/` 里是
        几十万个小文件，`--days 30` 的索引扫描都会变慢。清理按天粒度，
        索引文件（按月）留着不删——它很小，且是「哪些轮次存在过」的唯一记录。
        """
        if not self._dir.is_dir():
            return 0
        cutoff = (datetime.now(TZ) - timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d")
        removed = 0
        for day_dir in sorted(self._dir.iterdir()):
            if not day_dir.is_dir() or day_dir.name >= cutoff:
                continue
            try:
                removed += sum(1 for _ in day_dir.glob("*.md"))
                for f in day_dir.iterdir():
                    f.unlink(missing_ok=True)
                day_dir.rmdir()
            except OSError as e:
                logger.warning("[turn_trace] 清理 %s 失败: %s", day_dir, e)
        return removed

    def search(
        self,
        *,
        days: int = 7,
        tool: str = "",
        status: str = "",
        chat_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按索引筛轮次（新→旧）。tool 走子串匹配（`read` 能命中整条前缀）。"""
        out: list[dict[str, Any]] = []
        now = datetime.now(TZ)
        months = sorted({(now - timedelta(days=d)).strftime("%Y-%m")
                         for d in range(max(1, days))}, reverse=True)
        cutoff = (now - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
        for month in months:
            path = self._dir / f"{month}.index.jsonl"
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # 半行（写一半断电）不该让整次检索失败
                if row.get("ts", "")[:10] < cutoff:
                    continue
                if status and row.get("status") != status:
                    continue
                if chat_id and row.get("chat_id") != chat_id:
                    continue
                if tool and not any(tool in t for t in row.get("tools") or []):
                    continue
                out.append(row)
                if len(out) >= limit:
                    return out
        return out


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，原长 {len(text)} 字）"


def _render_markdown(trace: TurnTrace) -> str:
    """一轮轨迹 → 可读 Markdown。顺序即时间顺序：问 → 记忆 → 工具 → 答。"""
    lines = [
        f"# {trace.task_id}",
        "",
        f"- **时间**：{trace.ts}",
        f"- **会话**：{trace.chat_id}　**发送者**：{trace.sender_name or '-'}"
        f"（{trace.sender_id or '-'}）",
        f"- **耗时**：{trace.duration_ms:.0f}ms　**状态**：{trace.status}"
        + (f"　**错误**：{trace.error}" if trace.error else ""),
        f"- **工具链**：{' → '.join(trace.tools()) or '（无工具调用）'}",
        "",
        "## 用户问什么",
        "",
        "```text",
        _clip(trace.query, 4000),
        "```",
    ]
    if trace.memory.strip():
        lines += ["", "## 注入的长期记忆", "", "```text",
                  _clip(trace.memory, 2000), "```"]
    if trace.steps:
        lines += ["", "## 怎么处理的", ""]
        for i, step in enumerate(trace.steps, 1):
            lines.append(f"### {i}. `{step.name or '(未命名)'}` · {step.status}"
                         + (f" · {step.at}" if step.at else ""))
            if step.input:
                lines += ["", "入参：", "```json", _clip(step.input, _TOOL_INPUT_LIMIT), "```"]
            if step.output:
                lines += ["", "返回：", "```text",
                          _clip(step.output, _TOOL_OUTPUT_LIMIT), "```"]
            lines.append("")
    lines += ["", "## 回答", "", "```markdown", _clip(trace.reply, 8000), "```", ""]
    return "\n".join(lines)


def _index_row(trace: TurnTrace) -> dict[str, Any]:
    """索引行：只放筛得动、看得懂的字段（不含全文）。"""
    row = asdict(trace)
    row["steps"] = len(trace.steps)
    row["tools"] = trace.tools()
    row["query"] = _clip(trace.query, 200).replace("\n", " ")
    row["reply"] = _clip(trace.reply, 200).replace("\n", " ")
    row.pop("memory", None)  # 索引里不需要；要看去 .md
    return row


turn_tracer = TurnTraceRecorder()
