"""调度服务总装 — 创建入口 + 评估 + 调度循环。

模块拆分：
- store.py      档案数据模型与落盘（logs/scheduled_tasks/，目录即状态）
- evaluator.py  创建评估（超出能力面驳回；间隔 <10 分钟红线）
- cards.py      创建/驳回/生命周期回执卡（无按钮：审批门已下线）
- executor.py   到点执行（指令走 dsh 会话或旧编排器，产出发目标会话）
- loop.py       调度循环（approved 拾取/排期/过期归档/残留 pending 收编）
"""

from app.ops.scheduler.evaluator import EvalReport, ScheduledTaskEvaluator
from app.ops.scheduler.executor import ScheduledTaskExecutor
from app.ops.scheduler.loop import MIN_SCAN_INTERVAL_SEC, ScheduledTaskLoop
from app.ops.scheduler.scheduler import ScheduledTaskService
from app.ops.scheduler.store import ScheduledTask, ScheduledTaskStore

__all__ = [
    "MIN_SCAN_INTERVAL_SEC",
    "EvalReport",
    "ScheduledTask",
    "ScheduledTaskEvaluator",
    "ScheduledTaskExecutor",
    "ScheduledTaskLoop",
    "ScheduledTaskService",
    "ScheduledTaskStore",
]
