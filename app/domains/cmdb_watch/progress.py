"""指定工单关注的进度聚合 — 纯函数，零 IO。

「关注 xx 工单的执行情况」这句话的语义需要先钉死，否则实现会各说各话：

- **开始执行** = 这张单里**第一次出现**「执行中」的任务。工单本身没有全局状态字段
  （`lock_status` 是锁定标记），只能用任务状态聚合。
- **执行完成** = 所有任务都到终态（成功/失败/取消）。**失败也算完成**——
  用户要的是「发完了没有」，不是「成功没有」；失败会单独标红（`has_failure`），
  卡片头部换红色，文案提示去核对日志。
- **NACOS / 定时任务没有中间态**（一次性提交），所以只有「待执行 → 终态」，
  它们不会触发「开始执行」，只会推进「完成」。

为什么不做成「每个任务都提醒」：关注整单的人关心的是「开始发」和「发完了」两个
时间点，逐任务刷屏会把这两个信号淹掉——按姓名订阅那条链路（`Subscriber`）
才是逐任务的。
"""

from __future__ import annotations

from app.domains.cmdb_watch.client import Order
from app.domains.cmdb_watch.store import PHASE_DONE, PHASE_PENDING, PHASE_RUNNING

# 与 client 的状态归类同口径，但这里只要「这三态」的聚合视图
_EVENT_SUMMARY = "watch_started"
_EVENT_DONE = "watch_done"
_EVENT_DONE_FAILED = "watch_done_failed"


def order_phase(order: Order) -> str:
    """整单阶段：pending（还没开始）/ running（有任务在跑）/ done（全部终态）。

    空工单（还没填任务）算 pending：这时候说「完成了」是错的。
    """
    tasks = order.tasks
    if not tasks:
        return PHASE_PENDING
    if any(t.running for t in tasks):
        return PHASE_RUNNING
    if all(t.done for t in tasks):
        return PHASE_DONE
    return PHASE_PENDING


def has_failure(order: Order) -> bool:
    """是否有失败任务（取消也计入「未成功」，用户都该知道）。"""
    from app.domains.cmdb_watch.watcher import _is_failure

    return any(t.done and _is_failure(t) for t in order.tasks)


def progress_text(order: Order) -> str:
    """一行进度：`已完成 2/4 · 执行中 1 · 待执行 1`（0 的项不写，未开始写「尚未开始」）。"""
    total = len(order.tasks)
    if not total:
        return "工单尚未填写任务"
    done = sum(1 for t in order.tasks if t.done)
    running = sum(1 for t in order.tasks if t.running)
    pending = total - done - running
    if not done and not running:
        return f"尚未开始（共 {total} 项）"
    parts = [f"已完成 {done}/{total}"]
    if running:
        parts.append(f"执行中 {running}")
    if pending:
        parts.append(f"待执行 {pending}")
    if done and has_failure(order):
        parts.append("含失败")
    return " · ".join(parts)


def watch_event(prev_phase: str, order: Order) -> str:
    """根据上一轮阶段与当前整单状态判定要发的事件；空串 = 不发。

    只在**跨阶段**时发，且「完成」优先于「开始」：一次轮询里既跨过 running
    又跨到 done（工单执行很快）时，用户该收到的是「已完成」，而不是过时的「开始执行」。
    """
    phase = order_phase(order)
    if phase == PHASE_DONE and prev_phase != PHASE_DONE:
        return _EVENT_DONE_FAILED if has_failure(order) else _EVENT_DONE
    if phase == PHASE_RUNNING and prev_phase == PHASE_PENDING:
        return _EVENT_SUMMARY
    return ""


def watch_phase_for_notify(order: Order) -> str:
    """记录进 OrderWatch.phase 的值（与 `order_phase` 同口径，单独留个名字方便测试）。"""
    return order_phase(order)


def task_failure_names(order: Order, limit: int = 3) -> list[str]:
    """失败任务名（卡片/文字回执里点名，最多 limit 条）。"""
    from app.domains.cmdb_watch.watcher import _is_failure

    names = [f"{t.kind}#{t.task_id} {t.name}" for t in order.tasks
             if t.done and _is_failure(t)]
    return names[:limit]
