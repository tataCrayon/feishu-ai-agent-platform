"""定时任务数据模型与档案存储 — 每条任务一个目录，manifest.json 即状态。

落盘布局（logs/scheduled_tasks/ 整体 gitignore，运行产物不入库）：

    logs/scheduled_tasks/approved/task-.../     在调度中（active；新建默认落这里）
    logs/scheduled_tasks/rejected/task-.../     评估驳回（超出能力面）
    logs/scheduled_tasks/paused/task-.../       暂停（可恢复）
    logs/scheduled_tasks/done/task-.../         过期归档（终态）
    logs/scheduled_tasks/cancelled/task-.../    手动取消（终态）

`pending/` 是**历史目录**：审批门下线前新建的任务先落那里等管理员点按钮，
现在新建直接进 approved/，启动时 loop 会把残留的 pending 收编（否则它们既
不会被调度，也没有人能再点通过）。

manifest.json 记录全生命周期字段（创建人/评估结论/执行统计）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("scheduler.store")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # app/ops/scheduler/ -> feishu-agent-platform/
TASKS_DIR = PROJECT_DIR / "logs" / "scheduled_tasks"

# 状态机：received → approved（创建即生效）→ paused ⇄ approved；
# approved/paused → cancelled（手动取消，终态）；approved → done（过期归档）；
# received → rejected（超出能力面，评估阶段驳回）
STATUS_RECEIVED = "received"
STATUS_PENDING = "pending"      # 历史状态：审批门下线前的新建档案落这里
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

# 业务硬约束：扫描间隔下限 10 分钟（需求红线，评估与入库双重校验）
MIN_INTERVAL_MINUTES = 10

# 目标类型
TARGET_GROUP = "group"    # 群聊（chat_id，oc_ 开头）
TARGET_USER = "user"      # 私聊（open_id，ou_ 开头）

# 用户输入的 ID 形态 → 归一化目标类型
_ID_PREFIXES = {"oc_": TARGET_GROUP, "ou_": TARGET_USER}


@dataclass
class ScheduledTask:
    """一条定时任务的全生命周期档案。"""

    task_id: str  # task-YYYYMMDD-HHMMSS-xxxx
    status: str = STATUS_RECEIVED

    # ── 执行语义（超出能力面的请求在评估阶段驳回）──
    instruction: str = ""        # 用户原话（调度触发时作为 agent query 执行）
    agent: str = "aihot"         # 执行方：技能目录里的能力名（空 = 模型自答）
    target_type: str = ""        # group / user
    target_id: str = ""          # chat_id（oc_）/ open_id（ou_）
    target_name: str = ""        # 展示名（群名/用户名，创建时人工填写）
    schedule_kind: str = "every"  # every = 每 N 分钟；daily = 每天固定 HH:MM
    interval_minutes: int = 0    # every 型：间隔分钟（≥10）
    daily_time: str = ""         # daily 型："HH:MM"
    expire_date: str = ""        # 过期日 "YYYY-MM-DD"；空 = 长期

    # ── 创建人 ──
    creator_id: str = ""
    creator_name: str = ""
    source_chat_id: str = ""     # 创建请求所在会话（回执用）
    source_message_id: str = ""

    # ── 评估（创建时判定是否超出能力）──
    eval_ok: bool = True
    eval_reason: str = ""        # 驳回原因 / 通过说明

    # ── 审批（历史字段：审批门下线后由创建入口直接写空）──
    decided_by: str = ""
    decided_at: str = ""

    # ── 执行统计 ──
    run_count: int = 0
    last_run_at: str = ""        # "YYYY-MM-DD HH:MM:SS"
    last_run_ok: bool = True
    last_error: str = ""
    next_run_ts: float = 0.0     # 下次应触发的 epoch 秒
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        """一行人话描述（卡片/列表复用）。"""
        if self.schedule_kind == "daily":
            sched = f"每天 {self.daily_time}"
        else:
            sched = f"每 {self.interval_minutes} 分钟"
        target = self.target_name or self.target_id
        return f"{sched} → {target}"

    def validate_schedule(self) -> str:
        """调度参数合法性校验；返回错误说明，空串=通过。

        间隔 < 10 分钟在此直接判非法（不只靠评估，入库前再拦一道）。
        """
        if self.schedule_kind == "daily":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.daily_time or ""):
                return "daily_time 必须是 HH:MM（24 小时制）"
            return ""
        if self.schedule_kind != "every":
            return f"不支持的调度类型: {self.schedule_kind}"
        if not isinstance(self.interval_minutes, int) or self.interval_minutes < MIN_INTERVAL_MINUTES:
            return f"扫描间隔不得低于 {MIN_INTERVAL_MINUTES} 分钟"
        return ""


class ScheduledTaskStore:
    """定时任务档案的落盘与状态迁移。目录迁移即状态迁移。"""

    def __init__(self, tasks_dir: Path = TASKS_DIR):
        self._root = tasks_dir

    # ── 路径约定 ──

    def _dir_for(self, status: str, task_id: str) -> Path:
        return self._root / status / task_id

    def record_dir(self, task: ScheduledTask) -> Path:
        return self._dir_for(task.status, task.task_id)

    # ── 创建 ──

    def new_task(
        self,
        *,
        instruction: str,
        agent: str,
        target_type: str,
        target_id: str,
        target_name: str,
        schedule_kind: str,
        interval_minutes: int = 0,
        daily_time: str = "",
        expire_date: str = "",
        creator_id: str = "",
        creator_name: str = "",
        source_chat_id: str = "",
        source_message_id: str = "",
    ) -> ScheduledTask:
        """生成新档案落盘到 approved/（创建即纳入调度）。task_id 时间戳派生保证唯一。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        task_id = f"task-{ts}-{int(time.time() * 1000) % 10000:04d}"
        task = ScheduledTask(
            task_id=task_id,
            status=STATUS_APPROVED,
            instruction=instruction,
            agent=agent,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            schedule_kind=schedule_kind,
            interval_minutes=interval_minutes,
            daily_time=daily_time,
            expire_date=expire_date,
            creator_id=creator_id,
            creator_name=creator_name,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        d = self.record_dir(task)
        d.mkdir(parents=True, exist_ok=True)
        self._write_manifest(task)
        return task

    # ── 查询 ──

    def get(self, task_id: str) -> ScheduledTask | None:
        """按 id 在各状态目录（含 done/cancelled）中查找并读取 manifest。"""
        for status in (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
                       STATUS_PAUSED, STATUS_DONE, STATUS_CANCELLED):
            manifest = self._dir_for(status, task_id) / "manifest.json"
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    return ScheduledTask(**data)
                except Exception as e:
                    logger.warning("[scheduler] manifest read failed %s: %s", manifest, e)
                    return None
        return None

    def list_by_status(self, *statuses: str) -> list[ScheduledTask]:
        """列出指定状态的全部档案（按创建时间升序）。"""
        out: list[ScheduledTask] = []
        for status in statuses:
            d = self._root / status
            if not d.is_dir():
                continue
            for sub in sorted(d.iterdir()):
                manifest = sub / "manifest.json"
                if not manifest.exists():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    out.append(ScheduledTask(**data))
                except Exception as e:
                    logger.warning("[scheduler] manifest parse failed %s: %s", sub, e)
        out.sort(key=lambda t: t.created_at)
        return out

    def find_by_message(self, message_id: str) -> ScheduledTask | None:
        """按来源消息 id 查（同一条消息重复提交时复用 pending 档案）。"""
        for task in self.list_by_status(STATUS_PENDING):
            if task.source_message_id and task.source_message_id == message_id:
                return task
        return None

    def next_task_id_seq(self, prefix: str = "task") -> str:
        """诊断用：生成一个新 id（不落盘）。"""
        return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"

    # ── 状态迁移 ──

    def update(self, task: ScheduledTask, new_status: str | None = None) -> None:
        """更新字段并写 manifest；new_status 非空时迁移目录。"""
        old_dir = self.record_dir(task)
        if new_status:
            task.status = new_status
        new_dir = self.record_dir(task)
        if new_dir != old_dir and old_dir.exists():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_dir.exists():
                shutil.rmtree(new_dir)
            shutil.move(str(old_dir), str(new_dir))
        self._write_manifest(task)

    def archive(self, task: ScheduledTask, reason: str = "") -> None:
        """归档到 done/（过期终态），reason 记入 eval_reason。"""
        if reason:
            task.eval_reason = reason
        self.update(task, STATUS_DONE)

    def cancel(self, task: ScheduledTask, reason: str = "") -> None:
        """手动取消（终态）：仅 approved/paused 可取消，理由记入 eval_reason。"""
        if reason:
            task.eval_reason = reason
        self.update(task, STATUS_CANCELLED)

    # ── 内部 ──

    def _write_manifest(self, task: ScheduledTask) -> None:
        d = self.record_dir(task)
        d.mkdir(parents=True, exist_ok=True)
        manifest = d / "manifest.json"
        tmp = manifest.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(manifest)


def normalize_target(raw_target: str) -> tuple[str, str]:
    """把用户给的目标 ID 归一化为 (target_type, target_id)。

    识别 oc_/ou_ 前缀；其余形态（人名/群名/@手机号）返回 ("", 原文)，
    由评估阶段驳回——bot 不做名称反查（能力面之外）。
    """
    raw = (raw_target or "").strip()
    for prefix, ttype in _ID_PREFIXES.items():
        if raw.startswith(prefix):
            return ttype, raw
    return "", raw


__all__ = [
    "MIN_INTERVAL_MINUTES",
    "STATUS_APPROVED",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_PAUSED",
    "STATUS_PENDING",
    "STATUS_RECEIVED",
    "STATUS_REJECTED",
    "TARGET_GROUP",
    "TARGET_USER",
    "ScheduledTask",
    "ScheduledTaskStore",
    "normalize_target",
]
