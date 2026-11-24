"""执行历史 — 定时任务逐次运行记录（监控数据源）。

每次到点执行（无论成败）追加一行 JSON 到 logs/scheduler_runs/{date}.jsonl，
与 manifest 的滚动统计（run_count/last_run_*）互补：manifest 答「最近一次」，
本文件答「过去 30 天每次跑得怎么样」。写入是同步 JSONL 追加（对齐
observability.task_record 模式），单行小写入不拖慢执行链路。

读取方：控制台 dashboard_service.get_scheduled_tasks()、schedule-agent
get_task 详情。读侧按日期范围读，不做主动清理。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("scheduler.runs")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parents[3]
RUNS_LOG_DIR = PROJECT_DIR / "logs" / "scheduler_runs"


@dataclass
class TaskRunRecord:
    """一次定时任务执行的档案。"""

    task_id: str
    instruction: str = ""          # 截断 120，执行了什么
    agent: str = ""                # 目标执行方
    target_id: str = ""            # 投递目标（oc_/ou_）
    started_at: str = ""           # "%Y-%m-%d %H:%M:%S"
    duration_ms: int = 0
    outcome: str = "fail"          # ok / fail
    reply_preview: str = ""        # 产出文本截断 200
    error: str = ""                # 失败原因（截断 200）
    trace_id: str = ""             # 串联 logs/agents/ 明细 span
    schema_ver: int = field(default=1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["instruction"] = self.instruction[:120]
        d["reply_preview"] = self.reply_preview[:200]
        d["error"] = self.error[:200]
        return d


class RunRecorder:
    """逐次执行记录写入器 — logs/scheduler_runs/{date}.jsonl。"""

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or RUNS_LOG_DIR

    def write(self, rec: TaskRunRecord) -> None:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        path = self._dir / f"{today}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write scheduler run record: %s", e)

    def read_recent(self, days: int = 30, task_id: str | None = None) -> list[dict]:
        """读最近 N 天的执行记录（新→旧）；task_id 过滤单任务详情用。

        读侧容错：缺文件/坏行跳过，绝不抛出（旁路数据不阻断主流程）。
        """
        out: list[dict] = []
        now = datetime.now(TZ)
        for i in range(days):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            path = self._dir / f"{day}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if task_id and d.get("task_id") != task_id:
                    continue
                out.append(d)
        out.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return out


# 全局单例（executor / dashboard / handler 共用）
run_recorder = RunRecorder()


__all__ = ["RunRecorder", "TaskRunRecord", "run_recorder"]
