"""指定工单关注单测 — 整单进度聚合与「开始/完成」两次提醒，零网络。

跑法：python -m pytest tests/domains/cmdb_watch/test_watch.py -q

语义在这里被钉死（改之前先看 `progress.py` 抬头）：
- 开始执行 = 单里第一次出现「执行中」的任务
- 执行完成 = 所有任务都到终态（**失败也算完成**，只是卡片标红）
- 关注时已经开始的单子不补发「开始」；已经结束的单子不发任何卡
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
from typing import Any

from app.config import CmdbWatchConfig
from app.domains.cmdb_watch.client import CmdbClient, CmdbError, Order, Task, ENV_PROD
from app.domains.cmdb_watch.progress import (
    has_failure,
    order_phase,
    progress_text,
    watch_event,
)
from app.domains.cmdb_watch.store import (
    PHASE_DONE,
    PHASE_PENDING,
    PHASE_RUNNING,
    WatchStore,
)
from app.domains.cmdb_watch.watcher import CmdbWatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_TMP_STATE = str(pathlib.Path(tempfile.gettempdir()) / "feishu_agent_platform_cmdb_watch_test" / "test_watch.json")


def _cleanup() -> None:
    pathlib.Path(_TMP_STATE).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        if os.path.exists(_TMP_STATE + suffix):
            os.remove(_TMP_STATE + suffix)


def _task(task_id: int, state: int, label: str, kind: str = "sql",
          creator: str = "someone") -> Task:
    return Task(kind=kind, task_id=task_id, name=f"任务{task_id}", creator=creator,
                state=state, state_label=label, env="PROD-FLP")


def _order(tasks: list[Task], order_id: int = 100) -> Order:
    return Order(order_id=order_id, order_name="星路_正常发布_测试_2026-11-04", tasks=tasks)


class _FakeClient(CmdbClient):
    def __init__(self, orders: dict[int, Order]):
        super().__init__(base_url="http://cmdb.test", session_id="")
        self.orders = orders
        self.fail_ids: set[int] = set()
        self.full_calls: list[int] = []

    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        self.full_calls.append(order_id)
        if order_id in self.fail_ids or order_id not in self.orders:
            raise CmdbError(f"#{order_id} 不存在")
        return self.orders[order_id]

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        return {"deploy_status": {}, "run_bit": False}

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        return order_id in self.orders

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        return sorted(self.orders, reverse=True)


def _watcher(client: _FakeClient, **kw: Any) -> tuple[CmdbWatcher, list]:
    _cleanup()
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(target: str, card: dict[str, Any]) -> None:
        sent.append((target, card))

    cfg = CmdbWatchConfig(enabled=True, base_url="http://cmdb.test",
                          state_path=_TMP_STATE, probe_span=0, refresh_sec=3600,
                          watch_error_limit=kw.pop("watch_error_limit", 3), **kw)
    store = WatchStore(_TMP_STATE)
    store.scanned_max_id = -1
    return CmdbWatcher(cfg, client=client, store=store, send_card=send), sent


def _titles(sent: list) -> list[str]:
    return [c["header"]["title"]["content"] for _, c in sent]


# ── 阶段聚合（纯函数）────────────────────────────────────────

def test_phase_pending_running_done() -> None:
    assert order_phase(_order([])) == PHASE_PENDING
    assert order_phase(_order([_task(1, 1, "未执行"), _task(2, 1, "未执行")])) == PHASE_PENDING
    assert order_phase(_order([_task(1, 3, "执行成功"), _task(2, 1, "未执行")])) == PHASE_PENDING
    assert order_phase(_order([_task(1, 3, "执行成功"), _task(2, 2, "执行中")])) == PHASE_RUNNING
    assert order_phase(_order([_task(1, 3, "执行成功"), _task(2, 4, "执行失败")])) == PHASE_DONE


def test_failure_counts_as_done() -> None:
    """失败也算「完成」——用户问的是「发完了没有」，失败另标红。"""
    order = _order([_task(1, 4, "执行失败")])
    assert order_phase(order) == PHASE_DONE
    assert has_failure(order)
    assert watch_event(PHASE_RUNNING, order) == "watch_done_failed"


def test_watch_event_transitions() -> None:
    running = _order([_task(1, 2, "执行中")])
    done = _order([_task(1, 3, "执行成功")])
    assert watch_event(PHASE_PENDING, running) == "watch_started"
    assert watch_event(PHASE_RUNNING, running) == ""
    assert watch_event(PHASE_RUNNING, done) == "watch_done"
    assert watch_event(PHASE_DONE, done) == ""
    # 一次轮询里跨过 running 直接到 done：只发「完成」，不发过时的「开始」
    assert watch_event(PHASE_PENDING, done) == "watch_done"


def test_progress_text() -> None:
    assert progress_text(_order([])) == "工单尚未填写任务"
    assert progress_text(_order([_task(1, 1, "未执行")])) == "尚未开始（共 1 项）"
    mixed = _order([_task(1, 3, "执行成功"), _task(2, 2, "执行中"), _task(3, 1, "未执行")])
    assert progress_text(mixed) == "已完成 1/3 · 执行中 1 · 待执行 1"
    assert "含失败" in progress_text(_order([_task(1, 4, "执行失败"), _task(2, 3, "执行成功")]))


# ── 关注生命周期（端到端，桩客户端）──────────────────────────

def test_watch_full_lifecycle() -> None:
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        await w.scan_once()                     # 未开始 → 不发
        client.orders[100] = _order([_task(1, 2, "执行中")])
        await w.scan_once()                     # 开始执行
        await w.scan_once()                     # 状态没变 → 不发
        client.orders[100] = _order([_task(1, 3, "执行成功")])
        await w.scan_once()                     # 完成
        await w.scan_once()                     # 已完成 → 不发

    asyncio.run(run())
    assert _titles(sent) == ["CMDB 工单开始执行", "CMDB 工单执行完成"]
    assert all(t == "ou_a" for t, _ in sent)
    watch = w.store.order_watches["prod:100:ou_a"]
    assert watch.notified_started and watch.notified_done


def test_watch_started_before_subscribe_does_not_backfill() -> None:
    """关注时这张单已经在跑 → 只发「完成」，不补发「开始」。"""
    client = _FakeClient({100: _order([_task(1, 2, "执行中")])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        await w.scan_once()
        client.orders[100] = _order([_task(1, 3, "执行成功")])
        await w.scan_once()

    asyncio.run(run())
    assert _titles(sent) == ["CMDB 工单执行完成"]


def test_watch_finished_order_sends_nothing() -> None:
    client = _FakeClient({100: _order([_task(1, 3, "执行成功")])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        await w.scan_once()

    asyncio.run(run())
    assert sent == []
    assert w.store.order_watches["prod:100:ou_a"].notified_done


def test_watch_failure_is_red_card() -> None:
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        client.orders[100] = _order([_task(1, 4, "执行失败")])
        await w.scan_once()

    asyncio.run(run())
    assert sent[0][1]["header"]["template"] == "red"
    assert "存在失败任务" in _card_text(sent[0][1])


def test_multiple_watchers_one_order_each_gets_card() -> None:
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        await w.add_order_watch(100, "ou_b", order=client.orders[100])
        client.orders[100] = _order([_task(1, 2, "执行中")])
        await w.scan_once()

    asyncio.run(run())
    assert sorted(t for t, _ in sent) == ["ou_a", "ou_b"]


def test_watch_without_name_subscription_still_polls() -> None:
    """只关注工单、没有按姓名订阅时也必须轮询（历史 bug 形态：无人订阅即不扫）。"""
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, sent = _watcher(client)
    assert w.store.subscribers == {}

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        client.orders[100] = _order([_task(1, 2, "执行中")])
        stats = await w.scan_once()
        assert stats.orders_scanned >= 1

    asyncio.run(run())
    assert _titles(sent) == ["CMDB 工单开始执行"]


def test_watched_order_is_read_fully_every_round() -> None:
    """被关注的工单每轮都全量读（轻量接口没有 SQL/NACOS 状态）。"""
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, _ = _watcher(client)
    w.stats.last_full_refresh_ts = 9e18   # 关掉周期性全量校准，只看关注带来的全量

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        client.full_calls.clear()
        await w.scan_once()
        await w.scan_once()

    asyncio.run(run())
    assert client.full_calls.count(100) == 2


def test_watch_error_drops_after_limit() -> None:
    """工单被删/无权限时连续失败到阈值就自动停止关注，不无限重试。"""
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, _ = _watcher(client, watch_error_limit=3)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        client.fail_ids = {100}
        for _ in range(3):
            await w.scan_once()

    asyncio.run(run())
    assert "prod:100:ou_a" not in w.store.order_watches


def test_unwatch_removes_only_that_order() -> None:
    client = _FakeClient({
        100: _order([_task(1, 1, "未执行")], 100),
        101: _order([_task(2, 1, "未执行")], 101),
    })
    w, sent = _watcher(client)

    async def run() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])
        await w.add_order_watch(101, "ou_a", order=client.orders[101])
        await w.remove_order_watch(100, "ou_a")
        client.orders[101] = _order([_task(2, 2, "执行中")], 101)
        await w.scan_once()

    asyncio.run(run())
    assert "prod:100:ou_a" not in w.store.order_watches
    assert "prod:101:ou_a" in w.store.order_watches
    assert len(sent) == 1


def test_watch_persists_across_reload() -> None:
    client = _FakeClient({100: _order([_task(1, 1, "未执行")])})
    w, _sent = _watcher(client)

    async def first() -> None:
        await w.add_order_watch(100, "ou_a", order=client.orders[100])

    asyncio.run(first())
    reloaded = WatchStore(_TMP_STATE)
    reloaded.load()
    watch = reloaded.order_watches["prod:100:ou_a"]
    assert watch.order_id == 100 and watch.phase == PHASE_PENDING
    assert watch.notified_done is False


def _card_text(card: dict[str, Any]) -> str:
    from app.domains.cmdb_watch.render import card_text

    return card_text(card)
