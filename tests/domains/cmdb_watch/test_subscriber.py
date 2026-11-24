"""订阅链路单测 — 自助订阅 / 一人一张卡 / 退订，零网络。

跑法：python -m pytest tests/domains/cmdb_watch/test_subscriber.py -q

覆盖（都是真实会翻车的场景）：
- 用户名清洗：邮箱前缀、@ 前缀、带装饰的写法统一归一
- 不像用户名的输入被拒（中文、超长、单字符）
- 两个人订阅不同用户名 → 各自只收到自己的任务，各一张卡、各一个推送目标
- 同一 CMDB 用户名被两个飞书账号订阅 → 两个人都收到
- 后订阅的人能拿到「工单已存在但任务未执行」的新工单卡
- 退订后不再收卡；退订只影响本人
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
from app.domains.cmdb_watch.store import MAX_SUBSCRIBERS, WatchStore
from app.domains.cmdb_watch.subscriber import SubscribeError, normalize_cmdb_user
from app.domains.cmdb_watch.watcher import CmdbWatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_TMP_STATE = str(pathlib.Path(tempfile.gettempdir()) / "feishu_agent_platform_cmdb_watch_test" / "test_subscriber.json")


def _cleanup() -> None:
    pathlib.Path(_TMP_STATE).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        if os.path.exists(_TMP_STATE + suffix):
            os.remove(_TMP_STATE + suffix)


def _cfg(**kw: Any) -> CmdbWatchConfig:
    base: dict[str, Any] = dict(enabled=True, base_url="http://cmdb.test",
                                state_path=_TMP_STATE, interval_sec=60,
                                scan_limit=10, probe_span=0, initial_scan=0,
                                refresh_sec=0)
    base.update(kw)
    return CmdbWatchConfig(**base)


def _task(task_id: int, creator: str, state: int = 1, label: str = "未执行") -> Task:
    return Task(kind="sql", task_id=task_id, name=f"[CMDB工单] {creator}_变更",
                creator=creator, state=state, state_label=label)


def _order(order_id: int = 100, tasks: list[Task] | None = None) -> Order:
    return Order(order_id=order_id, order_name="TaaS 测试发布", tasks=tasks or [])


class _FakeClient(CmdbClient):
    def __init__(self, orders: dict[int, Order]):
        super().__init__(base_url="http://cmdb.test", session_id="")
        self._orders = orders

    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        if order_id not in self._orders:
            raise CmdbError(f"#{order_id} 不存在")
        return self._orders[order_id]

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        return order_id in self._orders

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        return sorted(self._orders, reverse=True)

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        """轻量轮询桩：真实接口只回 deploy 状态码。"""
        order = self._orders[order_id]
        return {"deploy_status": {str(t.task_id): [t.state, t.state_label, 0, ""]
                                  for t in order.tasks if t.kind == "deploy"},
                "run_bit": False}


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, target: str, card: dict[str, Any]) -> None:
        self.sent.append((target, card))

    def targets(self) -> list[str]:
        return [t for t, _ in self.sent]

    def for_target(self, target: str) -> list[dict[str, Any]]:
        return [c for t, c in self.sent if t == target]


def _watcher(client: _FakeClient) -> tuple[CmdbWatcher, _Recorder, WatchStore]:
    _cleanup()
    rec = _Recorder()
    store = WatchStore(_TMP_STATE)
    # 水位 -1：跳过「冷启动只对齐水位」分支，让桩里的工单都算本轮新出现
    store.scanned_max_id = -1
    w = CmdbWatcher(_cfg(), client=client, store=store, send_card=rec)
    return w, rec, store


# ── 用户名清洗 ──────────────────────────────────────────────

def test_normalize_cmdb_user_variants() -> None:
    assert normalize_cmdb_user("devuser") == "devuser"
    assert normalize_cmdb_user("  DevUser.Name  ") == "devuser.name"
    assert normalize_cmdb_user("devuser@example.com") == "devuser"
    assert normalize_cmdb_user("@devuser") == "devuser"
    assert normalize_cmdb_user("「devuser」") == "devuser"
    assert normalize_cmdb_user("devuser，") == "devuser"


def test_normalize_rejects_non_username() -> None:
    for bad in ("", "   ", "张三", "z", "a" * 50, "zig ang"):
        try:
            normalize_cmdb_user(bad)
        except SubscribeError:
            continue
        raise AssertionError(f"应当拒绝: {bad!r}")


# ── 一人一张卡 ──────────────────────────────────────────────

def test_two_subscribers_get_own_tasks_only() -> None:
    order = _order(tasks=[_task(11, "devuser"), _task(22, "huibin.li")])
    w, rec, store = _watcher(_FakeClient({100: order}))
    store.subscribe("ou_a", "devuser")
    store.subscribe("ou_b", "huibin.li")

    asyncio.run(w.scan_once())

    assert len(rec.sent) == 2
    a_card = rec.for_target("ou_a")[0]
    b_card = rec.for_target("ou_b")[0]
    from app.domains.cmdb_watch.render import card_text

    a_text, b_text = card_text(a_card), card_text(b_card)
    assert "#11" in a_text and "#22" not in a_text
    assert "#22" in b_text and "#11" not in b_text


def test_two_feishu_accounts_same_cmdb_user_both_notified() -> None:
    order = _order(tasks=[_task(11, "devuser")])
    w, rec, store = _watcher(_FakeClient({100: order}))
    store.subscribe("ou_phone", "devuser")
    store.subscribe("ou_pc", "devuser")

    asyncio.run(w.scan_once())

    assert sorted(rec.targets()) == ["ou_pc", "ou_phone"]


def test_one_person_one_card_per_event() -> None:
    """同一事件对同一个人只发一次（重复轮询不能刷屏）。"""
    order = _order(tasks=[_task(11, "devuser")])
    client = _FakeClient({100: order})
    w, rec, store = _watcher(client)
    store.subscribe("ou_a", "devuser")

    async def run() -> None:
        await w.scan_once()
        client._orders[100] = _order(tasks=[_task(11, "devuser", 2, "执行中")])
        await w.scan_once()
        await w.scan_once()

    asyncio.run(run())
    # 新工单 1 张 + 开始执行 1 张
    assert len(rec.for_target("ou_a")) == 2


def test_late_subscriber_gets_pending_order_card() -> None:
    """工单已在跟踪、任务还没执行时新来一个订阅者 → 下一轮补一张新工单卡。"""
    order = _order(tasks=[_task(11, "devuser")])
    w, rec, _store = _watcher(_FakeClient({100: order}))

    async def run() -> None:
        await w.scan_once()                       # 无人订阅：不扫
        assert rec.sent == []
        await w.add_subscriber("ou_late", "devuser")
        await w.scan_once()                       # 补发新工单卡
        await w.scan_once()                       # 不重复

    asyncio.run(run())
    assert len(rec.sent) == 1
    assert rec.targets() == ["ou_late"]


def test_no_subscribers_means_no_scan() -> None:
    client = _FakeClient({100: _order(tasks=[_task(11, "devuser")])})
    w, rec, _ = _watcher(client)

    stats = asyncio.run(w.scan_once())
    assert stats.orders_scanned == 0
    assert rec.sent == []


def test_unsubscribe_stops_push_and_only_affects_self() -> None:
    order = _order(tasks=[_task(11, "devuser"), _task(22, "huibin.li")])
    client = _FakeClient({100: order})
    w, rec, store = _watcher(client)
    store.subscribe("ou_a", "devuser")
    store.subscribe("ou_b", "huibin.li")

    async def run() -> None:
        await w.scan_once()
        await w.remove_subscriber("ou_a")
        client._orders[100] = _order(tasks=[
            _task(11, "devuser", 2, "执行中"), _task(22, "huibin.li", 2, "执行中")])
        await w.scan_once()

    asyncio.run(run())
    # 第一轮两人各 1 张；第二轮只有 ou_b 的「开始执行」
    assert len(rec.for_target("ou_a")) == 1
    assert len(rec.for_target("ou_b")) == 2


def test_subscriber_limit() -> None:
    store = WatchStore(_TMP_STATE)
    _cleanup()
    for i in range(MAX_SUBSCRIBERS):
        store.subscribe(f"ou_{i}", f"user{i}.test")
    try:
        store.subscribe("ou_overflow", "overflow.test")
    except ValueError:
        pass
    else:
        raise AssertionError("超过上限应当拒绝")


def test_subscription_persists_across_reload() -> None:
    _cleanup()
    store = WatchStore(_TMP_STATE)
    store.subscribe("ou_a", "devuser", display_name="张三")
    store.save()          # 落盘是调用方的事（subscribe 只改内存，同运行时口径）

    reloaded = WatchStore(_TMP_STATE)
    reloaded.load()
    assert reloaded.subscribers["ou_a"].cmdb_user == "devuser"
    assert reloaded.subscribers["ou_a"].display_name == "张三"
    assert reloaded.subscribers["ou_a"].target == "ou_a"   # 默认直投私聊


def test_target_prefers_explicit_chat_id() -> None:
    store = WatchStore(_TMP_STATE)
    _cleanup()
    sub = store.subscribe("ou_a", "devuser", chat_id="oc_group")
    assert sub.target == "oc_group"


# ── 按类型过滤（开放订阅后的新诉求） ─────────────────────────

def _mixed_order() -> Order:
    return _order(tasks=[
        _task(11, "devuser"),
        Task(kind="deploy", task_id=22, name="flp-counter", creator="devuser",
             state=0, state_label="未执行"),
        Task(kind="nacos", task_id=33, name="flp-webportal-app", creator="devuser",
             state=1, state_label="未执行"),
        Task(kind="cron", task_id=44, name="日终任务", creator="devuser",
             state=1, state_label="未执行"),
    ])


def test_kind_filter_only_pushes_selected_kinds() -> None:
    """只订「应用发布」的人：同一张单里他的 SQL/Nacos/定时任务都不该推。"""
    w, rec, store = _watcher(_FakeClient({100: _mixed_order()}))
    store.subscribe("ou_deploy", "devuser", kinds=["deploy"])
    store.subscribe("ou_sql", "devuser", kinds=["sql"])

    asyncio.run(w.scan_once())
    from app.domains.cmdb_watch.render import card_text

    deploy_text = card_text(rec.for_target("ou_deploy")[0])
    sql_text = card_text(rec.for_target("ou_sql")[0])
    assert "flp-counter" in deploy_text and "日终任务" not in deploy_text
    assert "sql" in sql_text.lower() or "变更" in sql_text
    assert "flp-counter" not in sql_text


def test_no_kind_filter_receives_all_kinds() -> None:
    """不传 kinds = 全收，与旧行为一致（兼容已有订阅）。"""
    w, rec, store = _watcher(_FakeClient({100: _mixed_order()}))
    store.subscribe("ou_all", "devuser")

    asyncio.run(w.scan_once())
    from app.domains.cmdb_watch.render import card_text

    text = card_text(rec.for_target("ou_all")[0])
    for name in ("flp-counter", "flp-webportal-app", "日终任务"):
        assert name in text


def test_resubscribe_without_kinds_keeps_filter() -> None:
    """改绑用户名不该把提醒范围重置成全收。"""
    store = WatchStore(_TMP_STATE)
    _cleanup()
    store.subscribe("ou_a", "devuser", kinds=["sql"])
    store.subscribe("ou_a", "devuser")          # 只说用户名 = 只改绑
    assert store.subscribers["ou_a"].kinds == ["sql"]
    store.subscribe("ou_a", "devuser", kinds=[])  # 明确「全部提醒」
    assert store.subscribers["ou_a"].kinds == []


def test_kind_filter_persists_across_reload() -> None:
    _cleanup()
    store = WatchStore(_TMP_STATE)
    store.subscribe("ou_a", "devuser", kinds=["sql", "deploy"])
    store.save()

    reloaded = WatchStore(_TMP_STATE)
    reloaded.load()
    assert reloaded.subscribers["ou_a"].kinds == ["sql", "deploy"]


def test_unknown_kind_is_dropped_not_stored() -> None:
    """手滑传了不存在的类型：丢弃，不留看不见的脏值。"""
    store = WatchStore(_TMP_STATE)
    _cleanup()
    sub = store.subscribe("ou_a", "devuser", kinds=["sql", "bogus"])
    assert sub.kinds == ["sql"]
