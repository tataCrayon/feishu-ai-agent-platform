"""运行时护栏 — 全局并发上限 + 有界排队 + 任务生命周期持久化。

对齐 config.toml [projects.runtime_guard]：
- 并发闸：Semaphore 限制同时执行的编排任务数（cc-connect 通道每任务 fork
  一个 Claude Code 子进程，不设上限会压垮本地服务器）。
- 有界排队：超出并发的任务进 asyncio.Queue 等待；队满或等待超时快速失败
  （回复"系统繁忙"），优于无界堆积。
- 任务持久化：任务起跑前落 running 档案，结束落 finished；进程重启时遗留
  的 running 档案标记 interrupted，供启动扫描后主动告知用户（任务不静默丢）。

与 logs/tasks/ TaskRecord 的关系：TaskRecord 是编排完成后的结果档案（旁路
观测）；本模块的 lifecycle 档案是"任务活着"的状态机（起跑/结束/中断），
粒度更粗，只服务并发治理与中断恢复。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("runtime.guard")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parents[3]  # app/guardrails/runtime/ -> feishu-agent-platform/

# 任务状态机
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_INTERRUPTED = "interrupted"


@dataclass
class TaskLifecycleRecord:
    """一条任务生命周期档案（logs/runtime_tasks/{date}.jsonl 每行一条）。"""

    task_id: str
    status: str = STATUS_RUNNING
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    query_preview: str = ""
    queued_ms: float = 0.0     # 排队等待时长（进入执行时回填）
    duration_ms: float = 0.0   # 总耗时（执行结束时回填）
    outcome: str = ""          # replied / busy_rejected / queue_timeout / error
    updated_at: str = field(default_factory=lambda: datetime.now(TZ).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class TaskLifecycleStore:
    """任务生命周期档案读写（JSONL append + 启动扫描）。"""

    def __init__(self, log_dir: Path | None = None):
        self._dir = log_dir or (PROJECT_DIR / "logs" / "runtime_tasks")
        self._dir.mkdir(parents=True, exist_ok=True)

    def _today_path(self) -> Path:
        return self._dir / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"

    def write(self, record: TaskLifecycleRecord) -> None:
        """追加一条生命周期档案；失败不影响主流程（旁路产物）。"""
        try:
            with open(self._today_path(), "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except Exception as e:
            logger.error("Failed to write lifecycle record: %s", e)

    def mark_interrupted(self, record: TaskLifecycleRecord) -> None:
        """把一条 running 档案状态改为 interrupted 后重写。"""
        record.status = STATUS_INTERRUPTED
        record.updated_at = datetime.now(TZ).isoformat()
        self.write(record)

    def scan_interrupted(self) -> list[TaskLifecycleRecord]:
        """扫描历史档案：每个 task_id 只保留最后一条记录，
        其中 status=running 的视为上次进程中断的任务（按时间倒序）。
        """
        last_by_task: dict[str, TaskLifecycleRecord] = {}
        try:
            for path in sorted(self._dir.glob("*.jsonl")):
                try:
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = TaskLifecycleRecord(**json.loads(line))
                            except TypeError:
                                continue  # 字段演进兼容：旧档案缺字段/多字段忽略
                            last_by_task[rec.task_id] = rec
                except Exception:
                    logger.warning("Failed to read lifecycle file %s", path)
        except Exception as e:
            logger.error("Lifecycle scan failed: %s", e)
        interrupted = [
            r for r in last_by_task.values() if r.status == STATUS_RUNNING
        ]
        interrupted.sort(key=lambda r: r.updated_at, reverse=True)
        return interrupted


class RuntimeGuard:
    """并发闸 + 有界队列 + 任务生命周期落盘（可整体关闭回旧版行为）。

    用法（main.py）：
        guard = RuntimeGuard(config)
        ok, err = await guard.start_task(rec)   # 排队等并发额度
        if not ok:
            await reply(err or "系统繁忙"); return
        try:
            ... 编排执行 ...
        finally:
            guard.finish_task(rec, outcome="replied")
    """

    def __init__(
        self,
        max_concurrent: int = 6,
        max_queue: int = 20,
        queue_timeout_sec: float = 120.0,
        persist: TaskLifecycleStore | None = None,
    ):
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._max_queue = max(0, max_queue)
        self._queue_timeout = max(0.0, queue_timeout_sec)
        self._persist = persist
        self._waiting = 0  # 当前排队中的任务数（含持有额度前）

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def would_queue(self) -> bool:
        """当前新任务是否需要排队（并发额度已占满）。

        供 ack 回执在申请额度前探测：额度空闲 → "正在处理"，
        占满 → "正在排队处理"，让用户在等待期就知道任务状态。
        """
        return self._sem.locked()

    async def start_task(self, rec: TaskLifecycleRecord) -> tuple[bool, str]:
        """申请执行额度：超出并发时排队，队满/超时返回 (False, 原因文案)。

        注意本调用在有并发空位时立即返回，否则**阻塞排队**直至获得额度
        （返回 True）、等待超时或队满拒绝（返回 False + 用户文案）。
        队满判定在有并发空位时跳过——有空位就不该拒绝。

        获得额度后把任务落 running 档案（persist 开启时）。
        """
        # 队满快速失败：仅在需要排队（无空闲额度）且队列已满时触发。
        # Semaphore.locked() 同步判定，必须在任何 await 之前完成，
        # 否则多个并发调用方都会误以为自己排得上。
        needs_queue = self._sem.locked()
        if needs_queue and self._waiting >= self._max_queue:
            logger.warning(
                "[guard] queue full (%d), rejecting task %s", self._waiting, rec.task_id
            )
            rec.outcome = "busy_rejected"
            return False, "😵 当前任务较多，请稍后再试"

        started = time.monotonic()
        if needs_queue:
            self._waiting += 1
        try:
            if needs_queue and self._queue_timeout > 0:
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=self._queue_timeout
                )
            else:
                await self._sem.acquire()
        except TimeoutError:
            rec.outcome = "queue_timeout"
            return False, "😵 等待超时，请稍后再试"
        finally:
            if needs_queue:
                self._waiting -= 1

        rec.queued_ms = (time.monotonic() - started) * 1000
        if self._persist:
            self._persist.write(rec)
        return True, ""

    def finish_task(self, rec: TaskLifecycleRecord, outcome: str = "replied") -> None:
        """释放并发额度并把任务落 finished 档案（persist 开启时）。"""
        self._sem.release()
        rec.status = STATUS_FINISHED
        rec.outcome = outcome
        rec.updated_at = datetime.now(TZ).isoformat()
        if self._persist:
            self._persist.write(rec)

    @staticmethod
    def notify_text(records: list[TaskLifecycleRecord]) -> str:
        """把中断任务列表组装成给用户的补通知文案。"""
        if not records:
            return ""
        lines = ["⚠️ 检测到上次服务重启时有任务被中断，如需继续请重新发送："]
        for r in records[:10]:
            name = f" {r.sender_name}" if r.sender_name else ""
            lines.append(f"• {r.query_preview or '（内容未留存）'}（{name}）")
        if len(records) > 10:
            lines.append(f"…等共 {len(records)} 条")
        return "\n".join(lines)
