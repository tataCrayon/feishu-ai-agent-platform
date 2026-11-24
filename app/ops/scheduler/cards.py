"""定时任务卡片 — 创建回执与生命周期回执。

审批门已下线（用户要求去掉）：创建即纳入调度，卡片只做「已建好 / 已推翻」的
告知，不再有按钮回调，也不再需要 card_action handler。评估驳回仍然保留——
那拦的是「我们做不到的事」（间隔 <10 分钟、执行方不在能力面），不是权限。
"""

from __future__ import annotations

from typing import Any

from app.ops.scheduler.evaluator import EvalReport
from app.ops.scheduler.store import ScheduledTask


def build_created_card(task: ScheduledTask, eval_report: EvalReport) -> dict[str, Any]:
    """创建成功回执：调度参数 + 下一次触发时间 + 怎么停。"""
    from lark_channel import CardBuilder

    b = CardBuilder()
    b.header(f"定时任务已创建 · {task.describe()}", template="green")
    b.markdown(
        f"- 执行指令：{task.instruction[:120]}\n"
        f"- 执行方：{task.agent}\n"
        f"- 创建人：{task.creator_name or task.creator_id}\n"
        f"- 下次执行：{_next_run_text(task)}"
    )
    lines = eval_report.summary_lines()
    if lines:
        b.markdown("<font color='grey'>" + "\n".join(f"- {line}" for line in lines) + "</font>")
    b.note("扫描间隔下限 10 分钟 · 说「停掉这条定时任务」即可暂停")
    payload = b.build()
    return dict(payload.data)


def build_rejected_card(task: ScheduledTask, eval_report: EvalReport) -> dict[str, Any]:
    """评估驳回回执：逐条列出超出能力面的原因 + 可调整方向。"""
    from lark_channel import CardBuilder

    b = CardBuilder()
    b.header("定时任务创建被驳回", template="red")
    b.markdown(
        f"- 执行指令：{task.instruction[:120]}\n"
        "- 原因：\n"
        + "\n".join(f"  - {line}" for line in eval_report.block_lines())
    )
    b.markdown("<font color='grey'>可调整后重新描述，例如「每 30 分钟总结一次日报发到本群」</font>")
    payload = b.build()
    return dict(payload.data)


def build_lifecycle_card(task: ScheduledTask, headline: str, detail: str = "") -> dict[str, Any]:
    """生命周期回执（暂停/恢复/取消）：一行标题 + 可选说明。"""
    from lark_channel import CardBuilder

    b = CardBuilder()
    ok = task.status not in ("rejected", "cancelled")
    b.header(f"{headline} · {task.describe()}", template="blue" if ok else "grey")
    b.markdown(f"- 任务：{task.task_id}\n- 当前状态：{task.status}")
    if detail:
        b.markdown(f"<font color='grey'>{detail}</font>")
    payload = b.build()
    return dict(payload.data)


def _next_run_text(task: ScheduledTask) -> str:
    if task.next_run_ts <= 0:
        return "下个扫描周期排期（≤10 分钟）"
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(task.next_run_ts))


__all__ = ["build_created_card", "build_lifecycle_card", "build_rejected_card"]
