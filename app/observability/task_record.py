"""任务档案 — 一次用户请求的完整编排记录。

对齐 agent-dev-standards Observability 支柱：每次编排（路由 → 执行 → 结构化
门禁 → 聚合）落一条结构化 TaskRecord 到 logs/tasks/{date}.jsonl，支撑：
- 按 agent 聚合的表现分析（调用量/成功率/耗时/门禁拦截率）
- 用户负反馈回填（feedback 字段），定位「哪类任务效果不好」
- EDD 闭环优化（.agent/loop/engine.py 可消费）

结果质量不走第二次 LLM 裁决：执行方自评 results_quality，编排层只落档。
写入方：CrewOrchestrator.execute()（编排结束时组装落盘）。
回填方：app/orchestrator/feedback.py（负反馈检测命中后 attach_feedback）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("observability.task_record")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
TASKS_LOG_DIR = PROJECT_DIR / "logs" / "tasks"
DECISIONS_LOG_DIR = PROJECT_DIR / "logs" / "decisions"
# Agent 回复全文（控制台「报告」tab 消费）——与 tasks/ 分离，避免 jsonl 单行膨胀：
# 主档案保留 200 字 preview 供列表快览，全文按 task_id 单文件寻址
REPLIES_LOG_DIR = PROJECT_DIR / "logs" / "task_replies"


class DecisionRecorder:
    """路由决策日志（SPEC-20260912 R4）— logs/decisions/{date}.jsonl。

    每条路由决策一行 JSON，与 TaskRecord 同 task_id 关联；
    支撑事后回答「这条消息当时为什么路由到 A」。
    """

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or DECISIONS_LOG_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, decision: dict[str, Any]) -> None:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        path = self._dir / f"{today}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(decision, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write decision record: %s", e)


@dataclass
class TaskRecord:
    """一次用户请求的完整编排档案。

    Attributes:
        task_id: 与 trace_id 同值，串联 logs/agents/ 明细 span。
        final_agent: 最终产出回复的 agent（role 名）；direct-answer/help 为自答场景。
        route_chain: 完整路由尝试链，如 ["dev-bot", "db-agent"]。
        results_quality: 执行方自评（ok/unclear/failed），缺省 ok。
        retry_with: 执行方建议的换道目标，只记录不执行。
        gate_blocked: 结构化门禁是否拦截（error/⚠️ 前缀/空响应，不调 LLM）。
        status: success / error / failed。
        feedback: 负反馈回填 {"negative": true, "text": ..., "matched": ...}，初始 None。
    """

    task_id: str
    ts: str = field(default_factory=lambda: datetime.now(TZ).isoformat())
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    query: str = ""
    final_agent: str = ""
    route_chain: list[str] = field(default_factory=list)
    results_quality: str = "ok"
    retry_with: str | None = None
    gate_blocked: bool = False
    status: str = "success"
    duration_ms: float = 0.0
    response_preview: str = ""
    error: str = ""
    tokens_total: int = 0
    cost_usd: float = 0.0
    model: str = ""
    llm_calls: int = 0              # 本次编排的 LLM 调用次数（spans 中带 model 的 span 数）
    merged_count: int = 1          # debounce 合并条数（>1 表示多连发合并）
    feedback: dict[str, Any] | None = None
    # ── 意图路由决策（SPEC-20260912 R4）──
    intent_summary: str = ""       # 模型复述的意图（首次路由）
    confidence: float = 0.0        # 首次路由置信度（fallback/降级后为实际生效值）
    demoted: bool = False          # 是否被低置信度降级为 clarify
    clarify_question: str = ""     # 澄清反问文本（demoted/clarify 时非空）

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class TaskRecorder:
    """TaskRecord 落盘 / 最近查询 / 负反馈回填 / 聚合统计。"""

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or TASKS_LOG_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── 写入 ──

    def write(self, record: TaskRecord) -> None:
        """追加一条任务档案到当日 jsonl。"""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        path = self._dir / f"{today}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except Exception as e:
            logger.error("Failed to write task record: %s", e)

    # ── 最近一条（负反馈回填目标）──

    def find_latest(
        self, chat_id: str, sender_id: str, days: int = 3
    ) -> tuple[Path, int] | None:
        """找该 (chat_id, sender_id) 最近一条记录，返回 (文件路径, 行号)。

        从今天往前最多扫 days 天；行号从 0 起。
        """
        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            path = self._dir / f"{date}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").strip().split("\n")
            except Exception:
                continue
            for i in range(len(lines) - 1, -1, -1):
                if not lines[i]:
                    continue
                try:
                    entry = json.loads(lines[i])
                except Exception:
                    continue
                if (
                    entry.get("chat_id") == chat_id
                    and entry.get("sender_id") == sender_id
                ):
                    return path, i
        return None

    def attach_feedback_by_task(
        self, task_id: str, feedback: dict[str, Any], *, days: int = 3
    ) -> bool:
        """按 task_id 精确回填反馈（卡片按钮走这条）。

        为什么不复用 `attach_feedback(chat_id, sender_id)`：那条是「找该用户最近
        一条档案」，靠时间和人推断属于哪轮——按钮自带 task_id，不该退化成猜。
        span 的 days 只是兜底范围（跨零点的轮次落在前一天）。
        """
        if not task_id:
            return False
        for offset in range(max(1, days)):
            date = (datetime.now(TZ) - timedelta(days=offset)).strftime("%Y-%m-%d")
            path = self._dir / f"{date}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").strip().splitlines()
            except OSError as e:
                logger.error("Failed to read tasks for %s: %s", date, e)
                continue
            for line_no, line in enumerate(lines):
                if not line or task_id not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("task_id") != task_id:
                    continue
                entry["feedback"] = self._merge_feedback(entry.get("feedback"), feedback)
                lines[line_no] = json.dumps(entry, ensure_ascii=False)
                try:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                except OSError as e:
                    logger.error("Failed to write feedback for %s: %s", task_id, e)
                    return False
                logger.info("Feedback(%s) attached to task %s",
                            feedback.get("source", "text"), task_id)
                return True
        return False

    @staticmethod
    def _merge_feedback(existing: Any, feedback: dict[str, Any]) -> dict[str, Any]:
        """合并反馈：保留首条（最接近原始不满），追加新文本与来源。"""
        if not isinstance(existing, dict):
            return dict(feedback)
        merged = dict(existing)
        texts = merged.get("texts") or [merged.get("text", "")]
        new_text = feedback.get("text", "")
        if new_text:
            texts.append(new_text)
            merged["texts"] = [t for t in texts if t]
        # 任一来源判定为负即为负：按钮点 👎 之后夹带的文字反馈不该把它翻正
        if feedback.get("negative"):
            merged["negative"] = True
        sources = merged.setdefault("sources", [])
        # 首条自带的 source 也要进来源列表：否则「这条是按钮判的还是文字判的」
        # 无从回溯（existing 是首条，它的 source 在这一步之前没被登记过）。
        for src in (merged.get("source"), feedback.get("source")):
            if src and src not in sources:
                sources.append(src)
        return merged

    def attach_feedback(
        self, chat_id: str, sender_id: str, feedback: dict[str, Any]
    ) -> bool:
        """把负反馈回填到该用户最近一条任务记录的 feedback 字段。

        Returns:
            True=回填成功；False=未找到可关联的任务记录。
        """
        hit = self.find_latest(chat_id, sender_id)
        if not hit:
            return False
        path, line_no = hit
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            entry = json.loads(lines[line_no])
            # 已有反馈则保留首条（更接近原始不满），追加新文本与来源
            feedback = self._merge_feedback(entry.get("feedback"), feedback)
            entry["feedback"] = feedback
            lines[line_no] = json.dumps(entry, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info(
                "Feedback attached to task %s: %s",
                entry.get("task_id"), feedback.get("text", "")[:50],
            )
            return True
        except Exception as e:
            logger.error("Failed to attach feedback: %s", e)
            return False

    # ── 聚合统计（报表用） ──

    def summarize(self, days: int = 7) -> dict[str, Any]:
        """聚合最近 N 天任务档案：按 agent 统计表现 + 负反馈明细。"""
        by_agent: dict[str, dict[str, Any]] = {}
        negative_cases: list[dict[str, Any]] = []
        total = 0
        gate_blocked = 0
        self_failed = 0

        for d in range(days):
            date = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y-%m-%d")
            path = self._dir / f"{date}.jsonl"
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    total += 1
                    if entry.get("gate_blocked"):
                        gate_blocked += 1
                    if entry.get("results_quality") == "failed":
                        self_failed += 1
                    agent = entry.get("final_agent") or "unknown"
                    s = by_agent.setdefault(agent, {
                        "count": 0, "errors": 0,
                        "total_duration_ms": 0.0, "gate_blocked": 0,
                        "self_failed": 0,
                        "negative": 0,
                    })
                    s["count"] += 1
                    if entry.get("status") not in ("success",):
                        s["errors"] += 1
                    s["total_duration_ms"] += entry.get("duration_ms", 0)
                    if entry.get("gate_blocked"):
                        s["gate_blocked"] += 1
                    if entry.get("results_quality") == "failed":
                        s["self_failed"] += 1
                    fb = entry.get("feedback")
                    if fb and fb.get("negative"):
                        s["negative"] += 1
                        negative_cases.append({
                            "ts": entry.get("ts"),
                            "task_id": entry.get("task_id", ""),
                            "agent": agent,
                            "query": (entry.get("query") or "")[:80],
                            "response": (entry.get("response_preview") or "")[:80],
                            "feedback_text": (fb.get("texts") or [""])[0][:60],
                            "sources": fb.get("sources") or [],
                        })
            except Exception as e:
                logger.error("Failed to read tasks for %s: %s", date, e)

        for _agent, s in by_agent.items():
            n = s["count"]
            s["error_rate"] = round(s["errors"] / n, 3) if n else 0.0
            s["gate_blocked_rate"] = round(s["gate_blocked"] / n, 3) if n else 0.0
            s["self_failed_rate"] = round(s["self_failed"] / n, 3) if n else 0.0
            s["negative_rate"] = round(s["negative"] / n, 3) if n else 0.0
            s["avg_duration_ms"] = round(s["total_duration_ms"] / n, 1) if n else 0.0

        return {
            "period_days": days,
            "total_tasks": total,
            "gate_blocked_tasks": gate_blocked,
            "gate_blocked_rate": round(gate_blocked / total, 3) if total else 0.0,
            "self_failed_tasks": self_failed,
            "self_failed_rate": round(self_failed / total, 3) if total else 0.0,
            "negative_count": len(negative_cases),
            "agents": by_agent,
            "recent_negative_cases": negative_cases[-20:],
        }


# 全局 recorder（与 tracer 同级单例）
task_recorder = TaskRecorder()
decision_recorder = DecisionRecorder()


class ReplyStore:
    """Agent 回复全文存储 — logs/task_replies/{task_id}.md。

    TaskRecord 的 response_preview 只有 200 字（列表快览用）；本存储落全文，
    控制台「报告」tab 按 task_id 拉取渲染。markdown 原样保存，读取即文件内容。
    """

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or REPLIES_LOG_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_id(task_id: str) -> str:
        """task_id → 文件名安全片段（防路径穿越）。"""
        return "".join(c for c in task_id if c.isalnum() or c in "-_")[:80]

    def write(self, task_id: str, reply: str, meta: dict[str, Any] | None = None) -> None:
        """保存一条回复全文（YAML 前置头 + 正文）。"""
        if not task_id or not reply.strip():
            return
        safe_id = self._safe_id(task_id)
        if not safe_id:
            return
        header = "---\n" + "".join(f"{k}: {v}\n" for k, v in (meta or {}).items()) + "---\n"
        try:
            (self._dir / f"{safe_id}.md").write_text(header + reply, encoding="utf-8")
        except Exception as e:
            logger.error("Failed to write reply %s: %s", safe_id, e)

    def read(self, task_id: str) -> str | None:
        """按 task_id 读全文；不存在返回 None。"""
        safe_id = self._safe_id(task_id)
        if not safe_id:
            return None
        path = self._dir / f"{safe_id}.md"
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed to read reply %s: %s", safe_id, e)
            return None


# 全局回复存储单例（CrewOrchestrator 落盘、Dashboard API 读取共用）
reply_store = ReplyStore()
