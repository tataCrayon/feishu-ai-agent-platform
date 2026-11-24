"""取数策略单测 — 「全量/轻量」分流与成本控制，零网络。

跑法：python -m pytest tests/domains/cmdb_watch/test_polling.py -q

为什么单独测：CMDB 内网接口不快，`action=data` 平均 125ms、最大 183KB
（含整篇 `html_content`）。如果哪天有人把轻量轮询改回全量，功能表面一切正常，
但请求量会翻十倍——这里用调用计数把它钉死。
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
from app.domains.cmdb_watch.store import WatchStore
from app.domains.cmdb_watch.watcher import CmdbWatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_TMP_STATE = str(pathlib.Path(tempfile.gettempdir()) / "feishu_agent_platform_cmdb_watch_test" / "test_polling.json")


def _cleanup() -> None:
    pathlib.Path(_TMP_STATE).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        if os.path.exists(_TMP_STATE + suffix):
            os.remove(_TMP_STATE + suffix)


def _cfg(refresh_sec: int, **kw: Any) -> CmdbWatchConfig:
    base: dict[str, Any] = dict(enabled=True, base_url="http://cmdb.test",
                                state_path=_TMP_STATE, interval_sec=60,
                                scan_limit=10, probe_span=0, initial_scan=0,
                                refresh_sec=refresh_sec)
    base.update(kw)
    return CmdbWatchConfig(**base)


def _deploy(task_id: int, creator: str, state: int, label: str) -> Task:
    return Task(kind="deploy", task_id=task_id, name=f"app-{task_id}", creator=creator,
                state=state, state_label=label, env="prod-k8s")


class _CountingClient(CmdbClient):
    """记录两类接口的调用次数（成本指标）。"""

    def __init__(self, orders: dict[int, Order]):
        super().__init__(base_url="http://cmdb.test", session_id="")
        self._orders = orders
        self.full_calls: list[int] = []
        self.status_calls: list[int] = []

    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        self.full_calls.append(order_id)
        if order_id not in self._orders:
            raise CmdbError(f"#{order_id} 不存在")
        return self._orders[order_id]

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        self.status_calls.append(order_id)
        order = self._orders[order_id]
        return {"deploy_status": {str(t.task_id): [t.state, t.state_label, 0, ""]
                                  for t in order.tasks if t.kind == "deploy"},
                "run_bit": False}

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        return order_id in self._orders

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        return sorted(self._orders, reverse=True)


def _watcher(client: _CountingClient, refresh_sec: int) -> tuple[CmdbWatcher, Any, list]:
    _cleanup()
    sent: list = []

    async def send(target: str, card: dict[str, Any]) -> None:
        sent.append((target, card))

    store = WatchStore(_TMP_STATE)
    store.subscribe("ou_a", "devuser")
    # 冷启动（水位 0）时发现阶段只对齐水位、不全量拉历史；用例要测的是稳态，
    # 所以把水位设成 0 以下一个不存在的 id，让全部桩工单都算「新出现」。
    store.scanned_max_id = -1
    w = CmdbWatcher(_cfg(refresh_sec), client=client, store=store, send_card=send)
    return w, store, sent


def test_tracked_order_polls_lightweight_by_default() -> None:
    """已在跟踪的工单，稳态下每轮只打轻量状态接口，不再整篇拉 data。"""
    order = Order(order_id=100, order_name="发布",
                  tasks=[_deploy(1, "devuser", 0, "未执行")])
    client = _CountingClient({100: order})
    w, _, _ = _watcher(client, refresh_sec=3600)

    async def run() -> None:
        await w.scan_once()      # 首见：全量读一次（要判断创建人）
        await w.scan_once()      # 稳态：轻量
        await w.scan_once()      # 稳态：轻量

    asyncio.run(run())
    assert client.full_calls.count(100) == 1     # 只有首见那次全量
    assert client.status_calls.count(100) == 2   # 后续两轮都是轻量


def test_lightweight_branch_detects_deploy_transitions() -> None:
    """轻量接口的状态码也要能触发开始/完成事件（不然省成本就变哑巴）。"""
    order = Order(order_id=100, order_name="发布",
                  tasks=[_deploy(1, "devuser", 0, "未执行")])
    client = _CountingClient({100: order})
    w, _, sent = _watcher(client, refresh_sec=3600)

    async def run() -> None:
        await w.scan_once()
        client._orders[100] = Order(order_id=100, order_name="发布",
                                    tasks=[_deploy(1, "devuser", 1, "执行中")])
        await w.scan_once()
        client._orders[100] = Order(order_id=100, order_name="发布",
                                    tasks=[_deploy(1, "devuser", 5, "执行成功")])
        await w.scan_once()

    asyncio.run(run())
    assert len(sent) == 3   # 新工单 + 开始 + 完成
    titles = [c["header"]["title"]["content"] for _, c in sent]
    assert titles == ["CMDB 生产工单提醒", "CMDB 任务执行中", "CMDB 任务已完成"]


def test_refresh_interval_triggers_full_reload() -> None:
    """refresh_sec 到点（这里设 0=每轮）时，跟踪中的工单也会重新全量读。"""
    order = Order(order_id=100, order_name="发布",
                  tasks=[_deploy(1, "devuser", 0, "未执行")])
    client = _CountingClient({100: order})
    w, _, _ = _watcher(client, refresh_sec=0)

    async def run() -> None:
        await w.scan_once()
        await w.scan_once()

    asyncio.run(run())
    assert client.full_calls.count(100) == 2
    assert client.status_calls == []


def test_new_order_still_uses_full_read() -> None:
    """新工单必须全量：任务创建人只在 data 里有，轻量接口拿不到。"""
    client = _CountingClient({101: Order(order_id=101, order_name="发布",
                                         tasks=[_deploy(1, "devuser", 0, "未执行")])})
    w, _, sent = _watcher(client, refresh_sec=3600)

    asyncio.run(w.scan_once())
    assert client.full_calls == [101]
    assert len(sent) == 1


def test_discovery_is_list_based_not_id_probe() -> None:
    """有 session 时靠列表接口发现（一次拿全），不做逐 id 探测。"""
    client = _CountingClient({101: Order(order_id=101, order_name="发布", tasks=[])})
    w, _, _ = _watcher(client, refresh_sec=3600)
    stats = w.stats

    def fake_discover() -> set[int]:
        return w._discover_orders(stats)

    found = fake_discover()
    # 键带环境前缀（两套 order_id 空间重叠，见 client 抬头）；UAT 侧首轮只对齐水位
    assert found == {"prod:101"}
    assert client.full_calls == []          # 发现阶段不拉工单正文


def test_stats_expose_full_vs_total() -> None:
    order = Order(order_id=100, order_name="发布",
                  tasks=[_deploy(1, "devuser", 0, "未执行")])
    client = _CountingClient({100: order})
    w, _, _ = _watcher(client, refresh_sec=3600)

    async def run() -> None:
        await w.scan_once()
        await w.scan_once()

    asyncio.run(run())
    assert w.stats.orders_scanned == 2
    assert w.stats.orders_full == 1          # 只有首见那次
    assert "全量 1" in w.stats.summary()


# ── 50 订阅者规模的请求数（性能回归） ────────────────────────

def _bulk_watcher(client: _CountingClient, *, n_subs: int, n_orders: int,
                  refresh_sec: int = 3600,
                  orders: dict[int, Order] | None = None
                  ) -> tuple[CmdbWatcher, WatchStore]:
    """N 个订阅者、N 张跟踪工单的稳态（首见已发生过，只剩轮询）。"""
    _cleanup()
    store = WatchStore(_TMP_STATE)
    users = [f"user{i}.test" for i in range(n_subs)]
    for i, user in enumerate(users):
        store.subscribe(f"ou_{i:03d}", user)
    oids = sorted(orders) if orders is not None else [1000 + i for i in range(n_orders)]
    for i, oid in enumerate(oids):
        # 与桩数据保持一致：任务挂在第一个订阅者名下，state 从桩工单的真实状态取
        stub = (orders or {}).get(oid)
        task_states = {
            t.key: _task_state(t.key, t.creator, t.state, t.state_label)
            for t in (stub.tasks if stub else [])} or {
            f"deploy:{i}": _task_state(f"deploy:{i}", users[0], 1, "未执行")}
        store.upsert_order(oid, f"批量发布 {i}", task_states)
    store.scanned_max_id = max(oids) if oids else 0
    w = CmdbWatcher(_cfg(refresh_sec), client=client, store=store)
    return w, store


def _task_state(key: str, creator: str, state: int, label: str) -> Any:
    from app.domains.cmdb_watch.store import TaskState

    return TaskState(state=state, label=label, creator=creator,
                     name=key, kind=key.split(":", 1)[0])


def test_50_subscribers_request_count_stays_flat() -> None:
    """订阅人数 1 → 50 不改变每轮请求数：请求只跟「还在动的工单」成正比。

    这条是把性能目标钉死：50 个订阅者、50 张跟踪工单一律「已收口」时，
    一轮只该有 1 次列表发现 + 0 次轮询（没有 deploy 还在跑）。
    """
    orders = {1000 + i: Order(order_id=1000 + i, order_name=f"批量发布 {i}", tasks=[])
              for i in range(50)}
    # 桩任务全挂 users[0] 名下；「全部收口」由下面统一改 state 达成
    client = _CountingClient(orders)
    w, store = _bulk_watcher(client, n_subs=50, n_orders=50, orders=orders)
    # 全部任务已到终态
    for state in store.orders.values():
        for task in state.tasks.values():
            task.state = 3

    asyncio.run(w.scan_once())
    assert client.full_calls == []
    assert client.status_calls == []
    assert w.stats.orders_scanned == 0

    # 1 个订阅者时的一致对照：同样的「全部收口」场景，请求数一样
    client1 = _CountingClient(dict(orders))
    w1, store1 = _bulk_watcher(client1, n_subs=1, n_orders=50, orders=orders)
    for state in store1.orders.values():
        for task in state.tasks.values():
            task.state = 3
    asyncio.run(w1.scan_once())
    assert len(client1.full_calls) + len(client1.status_calls) == 0


def test_request_count_scales_with_live_orders_not_subscribers() -> None:
    """10 张「还在跑」的工单：1 人订阅与 50 人订阅的轮询次数相同（各 10 次）。"""
    def build(n_subs: int) -> tuple[_CountingClient, CmdbWatcher]:
        orders = {2000 + i: Order(
            order_id=2000 + i, order_name=f"发布 {i}",
            tasks=[Task(kind="deploy", task_id=i, name=f"app-{i}", creator="user0.test",
                        state=0, state_label="未执行")]) for i in range(10)}
        client = _CountingClient(orders)
        w, _ = _bulk_watcher(client, n_subs=n_subs, n_orders=10, orders=orders)
        return client, w

    # 第一轮是新单发现（全量读），第二轮起才是稳态轮询——这里比的是稳态
    def steady(client: _CountingClient, w: CmdbWatcher) -> None:
        async def run() -> None:
            await w.scan_once()
            client.full_calls.clear()
            client.status_calls.clear()
            await w.scan_once()
        asyncio.run(run())

    per_one, w_one = build(1)
    steady(per_one, w_one)
    per_fifty, w_fifty = build(50)
    steady(per_fifty, w_fifty)

    assert len(per_one.status_calls) == 10
    assert len(per_fifty.status_calls) == 10
    assert len(per_one.full_calls) == len(per_fifty.full_calls) == 0


def test_closed_orders_drop_out_of_polling_budget() -> None:
    """任务全部收口的工单不再每轮请求（只保留在跟踪集合里）。"""
    order = Order(order_id=100, order_name="发布",
                  tasks=[_deploy(1, "devuser", 0, "未执行")])
    client = _CountingClient({100: order})
    w, _, _ = _watcher(client, refresh_sec=3600)

    async def run() -> None:
        await w.scan_once()                     # 首见：全量读
        client._orders[100] = Order(order_id=100, order_name="发布",
                                    tasks=[_deploy(1, "devuser", 3, "执行成功")])
        await w.scan_once()                     # 轻量：拿到终态
        await w.scan_once()                     # 已收口：不再问

    asyncio.run(run())
    assert client.status_calls.count(100) == 1
    assert "prod:100" in w.store.orders               # 仍在跟踪集合（用户可能还要看进度）
    assert not w.store.needs_polling()


def test_new_order_read_once_per_round() -> None:
    """本轮刚全量读过的新单，第二轮循环不再重复请求（同轮只问一次）。"""
    client = _CountingClient({101: Order(order_id=101, order_name="发布",
                                         tasks=[_deploy(1, "devuser", 0, "未执行")])})
    w, _, _ = _watcher(client, refresh_sec=3600)

    asyncio.run(w.scan_once())
    assert client.full_calls == [101]
    assert client.status_calls == []
