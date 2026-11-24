"""运行时护栏单测：并发闸 + 有界排队 + 任务生命周期持久化 + 历史落盘回填。

运行：python -m pytest tests/guardrails/runtime/test_guard.py -q（无 pytest-asyncio，用 anyio 插件）
"""

import asyncio
import json

import pytest

from app.guardrails.runtime import RuntimeGuard, TaskLifecycleRecord, TaskLifecycleStore
from app.orchestrator.history import SessionHistory

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rec(task_id: str = "t1", **kw) -> TaskLifecycleRecord:
    return TaskLifecycleRecord(task_id=task_id, **kw)


# ── RuntimeGuard：并发上限 ────────────────────────────────────────────


async def test_semaphore_limits_concurrency(tmp_path):
    store = TaskLifecycleStore(log_dir=tmp_path)
    guard = RuntimeGuard(max_concurrent=2, max_queue=10, queue_timeout_sec=5.0, persist=store)
    running = 0
    peak = 0

    async def worker(i: int):
        nonlocal running, peak
        rec = _rec(f"t{i}")
        ok, _ = await guard.start_task(rec)
        assert ok
        nonlocal_running = running + 1
        running = nonlocal_running
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        guard.finish_task(rec)

    await asyncio.gather(*(worker(i) for i in range(6)))
    assert peak <= 2


async def test_queue_full_rejects_fast(tmp_path):
    """并发占满 + 队列满 → 新任务快速失败，不阻塞。"""
    store = TaskLifecycleStore(log_dir=tmp_path)
    guard = RuntimeGuard(max_concurrent=1, max_queue=1, queue_timeout_sec=5.0, persist=store)

    rec_holder = _rec("holder")
    ok, _ = await guard.start_task(rec_holder)  # 占住唯一并发额度
    assert ok

    queued = _rec("queued")
    # 排队会阻塞等待额度，放后台起跑；等它进入队列（waiting=1）再继续
    queue_task = asyncio.create_task(guard.start_task(queued))
    for _ in range(50):
        if guard.waiting >= 1:
            break
        await asyncio.sleep(0.01)
    assert guard.waiting == 1

    rejected = _rec("rejected")
    ok, msg = await guard.start_task(rejected)  # 队满，应立即拒绝
    assert not ok
    assert "任务较多" in msg
    assert rejected.outcome == "busy_rejected"

    # 释放 holder → 排队者获得额度正常返回
    guard.finish_task(rec_holder)
    ok, _ = await queue_task
    assert ok
    guard.finish_task(queued)


async def test_queue_timeout(tmp_path):
    """并发占满且排队等待超时 → 返回超时文案。"""
    store = TaskLifecycleStore(log_dir=tmp_path)
    guard = RuntimeGuard(max_concurrent=1, max_queue=5, queue_timeout_sec=0.05, persist=store)

    holder = _rec("holder")
    await guard.start_task(holder)
    waiter = _rec("waiter")
    ok, msg = await guard.start_task(waiter)
    assert not ok
    assert "超时" in msg
    assert waiter.outcome == "queue_timeout"
    guard.finish_task(holder)


# ── 生命周期持久化：running → finished / interrupted 扫描 ─────────────


async def test_lifecycle_persist_and_scan(tmp_path):
    store = TaskLifecycleStore(log_dir=tmp_path)
    guard = RuntimeGuard(max_concurrent=2, max_queue=5, persist=store)

    rec = _rec("t-a", chat_id="oc_x", sender_id="ou_1", query_preview="查订单")
    ok, _ = await guard.start_task(rec)
    assert ok
    guard.finish_task(rec, outcome="replied")

    # 文件应有两条：running + finished，task_id 相同
    content = (tmp_path / store._today_path().name).read_text(encoding="utf-8")
    lines = [json.loads(line) for line in content.strip().splitlines()]
    assert [r["status"] for r in lines] == ["running", "finished"]

    # finished 不应被扫描为中断
    assert store.scan_interrupted() == []


async def test_scan_interrupted_across_restarts(tmp_path):
    """遗留 running 档案 → 重启扫描识别为 interrupted。"""
    store = TaskLifecycleStore(log_dir=tmp_path)
    # 模拟上次进程中断：只写了 running，没有 finished
    store.write(_rec("lost-1", chat_id="oc_x", query_preview="发布 hub-platform"))
    store.write(TaskLifecycleRecord(task_id="done-1", status="finished", outcome="replied"))
    store.write(_rec("lost-2", chat_id="oc_y", query_preview="查数据库"))

    interrupted = store.scan_interrupted()
    assert {r.task_id for r in interrupted} == {"lost-1", "lost-2"}


async def test_scan_handles_legacy_lines(tmp_path):
    """旧档案含未知字段/坏行 → 跳过不崩。"""
    f = tmp_path / "2026-01-01.jsonl"
    f.write_text(
        '{"task_id":"a","status":"running"}\n'
        'not-json\n'
        '{"task_id":"b","status":"running","future_field":1}\n',
        encoding="utf-8",
    )
    store = TaskLifecycleStore(log_dir=tmp_path)
    interrupted = store.scan_interrupted()
    assert [r.task_id for r in interrupted] == ["a"]


async def test_notify_text():
    recs = [
        TaskLifecycleRecord(task_id="a", sender_name="张三", query_preview="查订单"),
        TaskLifecycleRecord(task_id="b", query_preview=""),
    ]
    text = RuntimeGuard.notify_text(recs)
    assert "中断" in text and "查订单" in text and "张三" in text
    assert RuntimeGuard.notify_text([]) == ""


# ── SessionHistory 落盘与回填 ─────────────────────────────────────────


async def test_history_persist_and_restore(tmp_path):
    h1 = SessionHistory(max_turns=2, persist_dir=tmp_path)
    await h1.record("oc_x", "ou_1", "第一问", "第一答")
    await h1.record("oc_x", "ou_1", "第二问", "第二答")
    await h1.record("oc_x", "ou_1", "第三问", "第三答")  # 环形：第一轮被挤掉

    snap = await h1.snapshot("oc_x", "ou_1")
    assert snap[0]["content"] == "第二问"

    # 新实例（模拟重启）→ 惰性回填
    h2 = SessionHistory(max_turns=2, persist_dir=tmp_path)
    snap2 = await h2.snapshot("oc_x", "ou_1")
    assert snap2 == snap


async def test_history_clear_removes_file(tmp_path):
    h = SessionHistory(max_turns=2, persist_dir=tmp_path)
    await h.record("oc_x", "ou_1", "q", "a")
    await h.clear("oc_x", "ou_1")
    assert await h.snapshot("oc_x", "ou_1") == []
    h2 = SessionHistory(max_turns=2, persist_dir=tmp_path)
    assert await h2.snapshot("oc_x", "ou_1") == []


async def test_history_isolated_users(tmp_path):
    h = SessionHistory(max_turns=2, persist_dir=tmp_path)
    await h.record("oc_x", "ou_1", "u1问", "u1答")
    await h.record("oc_x", "ou_2", "u2问", "u2答")
    assert (await h.snapshot("oc_x", "ou_1"))[0]["content"] == "u1问"
    assert (await h.snapshot("oc_x", "ou_2"))[0]["content"] == "u2问"
