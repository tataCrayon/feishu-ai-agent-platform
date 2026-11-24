"""UAT 工单支持单测 — 双环境识别、两套编号空间隔离、卡片与链接区分，零网络。

跑法：python -m pytest tests/domains/cmdb_watch/test_uat_env.py -q

守的是三件事：
- 生产与 UAT 是**两套独立的自增 order_id**，同号不同单 → 所有键、去重、链接都要带环境
- 环境来源优先级：链接里的 > 工具参数 > 生产；链接识别必须先判 UatOrderDetail
- 接口名前缀只有一处规则（`api_prefix`），UAT 走 `Uat*Api`
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from typing import Any

from app.config import CmdbWatchConfig
from app.domains.cmdb_watch.client import (
    ENV_PROD,
    ENV_UAT,
    CmdbClient,
    CmdbError,
    Order,
    Task,
    api_prefix,
    env_of_order_page,
    normalize_env,
    split_order_key,
)
from app.domains.cmdb_watch.render import card_text, order_url
from app.domains.cmdb_watch.store import WatchStore
from app.domains.cmdb_watch.watcher import CmdbWatcher

_TMP = str(pathlib.Path(tempfile.gettempdir()) / "feishu_agent_platform_cmdb_watch_test" / "uat_env.json")


def _cleanup() -> None:
    pathlib.Path(_TMP).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        p = pathlib.Path(_TMP + suffix)
        if p.exists():
            p.unlink()


def _task(state: int = 1, label: str = "未执行", creator: str = "devuser") -> Task:
    return Task(kind="sql", task_id=1, name="任务", creator=creator,
                state=state, state_label=label)


def _order(order_id: int, env: str, tasks: list[Task] | None = None) -> Order:
    return Order(order_id=order_id, order_name="星路_测试_2026-11-13",
                 tasks=tasks or [], env=env)


class _EnvClient(CmdbClient):
    """按 (env, order_id) 存单的桩：同号在两个环境里可以是两张不同的单。"""

    def __init__(self, orders: dict[tuple[str, int], Order]):
        super().__init__(base_url="http://cmdb.test", session_id="")
        self.orders = orders
        self.fetched: list[tuple[str, int]] = []

    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        self.fetched.append((env, order_id))
        order = self.orders.get((env, order_id))
        if order is None:
            raise CmdbError(f"{env}#{order_id} 不存在")
        return order

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        return {"deploy_status": {}, "run_bit": False}

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        return (env, order_id) in self.orders

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        return sorted((i for e, i in self.orders if e == env), reverse=True)


def _watcher(client: _EnvClient, *, refresh_sec: int = 3600) -> tuple[CmdbWatcher, list]:
    _cleanup()
    sent: list = []

    async def send(target: str, card: dict[str, Any]) -> None:
        sent.append((target, card))

    cfg = CmdbWatchConfig(enabled=True, base_url="http://cmdb.test", state_path=_TMP,
                          probe_span=0, refresh_sec=refresh_sec)
    store = WatchStore(_TMP)
    store.scanned_max = {ENV_PROD: -1, ENV_UAT: -1}
    store.subscribe("ou_devuser", "devuser")
    return CmdbWatcher(cfg, client=client, store=store, send_card=send), sent


# ── 纯函数：环境识别 ────────────────────────────────────────

def test_order_page_detects_env_uat_first() -> None:
    """`UatOrderDetail` 里也含 `orderdetail`：判定顺序反了会永远判成生产。"""
    assert env_of_order_page("http://cmdb.x.local/order/UatOrderDetail?order_id=1924") == ENV_UAT
    assert env_of_order_page("http://cmdb.x.local/order/OrderDetail?order_id=5910") == ENV_PROD
    assert env_of_order_page("随便一段话") == ""


def test_normalize_env_defaults_to_prod() -> None:
    assert normalize_env("uat") == ENV_UAT
    assert normalize_env("UAT") == ENV_UAT
    assert normalize_env("") == ENV_PROD
    assert normalize_env("prod") == ENV_PROD


def test_api_prefix_only_uat_gets_prefix() -> None:
    assert api_prefix(ENV_UAT) == "Uat"
    assert api_prefix(ENV_PROD) == ""


def test_split_order_key_reads_env_and_legacy_keys() -> None:
    assert split_order_key("uat:1924") == (ENV_UAT, 1924)
    assert split_order_key("prod:5910") == (ENV_PROD, 5910)
    # 老档案里的裸 id 按生产读
    assert split_order_key("5784") == (ENV_PROD, 5784)


def test_order_url_points_at_the_right_page() -> None:
    base = "http://cmdb.test"
    assert order_url(1924, base, ENV_UAT).endswith("/order/UatOrderDetail?order_id=1924")
    assert order_url(5910, base, ENV_PROD).endswith("/order/OrderDetail?order_id=5910")


def test_fetch_order_hits_uat_api_for_uat_env(monkeypatch) -> None:
    """环境不是「口头参数」：它决定打哪个接口名。"""
    calls: list[str] = []

    def _fake_get(self: CmdbClient, api: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(api)
        return {"result": True, "data": {"order_id": params["order_id"],
                                         "order_name": "UAT 单", "client": []}}

    monkeypatch.setattr(CmdbClient, "_get", _fake_get)
    client = CmdbClient(base_url="http://cmdb.test", session_id="")
    order = client.fetch_order(1924, ENV_UAT)
    assert calls == ["UatOrderApi"]
    assert order.env == ENV_UAT and order.key == "uat:1924"

    client.fetch_order(5910, ENV_PROD)
    assert calls[-1] == "OrderApi"


# ── 双环境扫描 ──────────────────────────────────────────────

def test_same_order_id_in_two_envs_are_tracked_separately() -> None:
    """生产 100 与 UAT 100 是两张单：都得跟踪，键不能互相覆盖。"""
    client = _EnvClient({
        (ENV_PROD, 100): _order(100, ENV_PROD, [_task()]),
        (ENV_UAT, 100): _order(100, ENV_UAT, [_task()]),
    })
    w, sent = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert {"prod:100", "uat:100"} <= set(w.store.orders)
    # 两张单各发一张新工单卡：同号不同环境必须各算一次
    assert len(sent) == 2
    titles = [c["header"]["title"]["content"] for _, c in sent]
    assert "CMDB 生产工单提醒" in titles
    assert "CMDB 生产工单提醒 · UAT" in titles


def test_uat_card_links_to_uat_page_and_marks_env() -> None:
    order = _order(1924, ENV_UAT, [_task()])
    client = _EnvClient({(ENV_UAT, 1924): order})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    body = card_text(sent[0][1])
    assert "UatOrderDetail?order_id=1924" in body
    assert "执行环境：** UAT" in body.replace("**执行环境：**", "执行环境：**")


def test_uat_transition_card_is_marked() -> None:
    """状态事件卡也要标 UAT：同一个「执行失败」在两边处置完全不同。"""
    order = _order(1924, ENV_UAT, [_task()])
    client = _EnvClient({(ENV_UAT, 1924): order})
    # refresh_sec=0：每轮都全量读（SQL 状态只有 action=data 里有，轻量接口只回 deploy）
    w, sent = _watcher(client, refresh_sec=0)

    async def run() -> None:
        await w.scan_once()                                   # 首见：新工单卡
        client.orders[(ENV_UAT, 1924)] = _order(1924, ENV_UAT, [_task(3, "执行成功")])
        await w.scan_once()                                   # 完成事件

    asyncio.run(run())
    titles = [c["header"]["title"]["content"] for _, c in sent]
    assert titles[-1] == "CMDB 任务已完成 · UAT"


def test_each_env_keeps_its_own_watermark() -> None:
    """共用一条水位线会让 UAT 的 1932 把生产的 5917 拉回去（或反之）。"""
    client = _EnvClient({
        (ENV_PROD, 5917): _order(5917, ENV_PROD, [_task()]),
        (ENV_UAT, 1932): _order(1932, ENV_UAT, [_task()]),
    })
    w, _ = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert w.store.watermark(ENV_PROD) == 5917
    assert w.store.watermark(ENV_UAT) == 1932


def test_uat_only_subscriber_order_gets_notified() -> None:
    """UAT 单同样走「按姓名」链路：订阅者名下的任务照样提醒。"""
    client = _EnvClient({(ENV_UAT, 1924): _order(1924, ENV_UAT, [_task()])})
    w, sent = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert len(sent) == 1
    assert sent[0][0] == "ou_devuser"


def test_store_roundtrip_keeps_env() -> None:
    _cleanup()
    store = WatchStore(_TMP)
    store.upsert_order(1924, "UAT 单", {"sql:1": _state()}, env=ENV_UAT)
    store.add_order_watch(1924, "ou_a", env=ENV_UAT, order_name="UAT 单")
    store.scanned_max[ENV_UAT] = 1932
    store.save()

    reloaded = WatchStore(_TMP)
    reloaded.load()
    assert reloaded.orders["uat:1924"].env == ENV_UAT
    assert reloaded.order_watches["uat:1924:ou_a"].env == ENV_UAT
    assert reloaded.watermark(ENV_UAT) == 1932
    # 同名同号的生产单不该被这条记录影响
    assert "prod:1924" not in reloaded.orders


def test_legacy_state_without_env_reads_as_prod() -> None:
    """老档案（键里没有环境前缀）必须能继续读，否则升级即丢全部跟踪状态。"""
    _cleanup()
    pathlib.Path(_TMP).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(_TMP).write_text(
        '{"scanned_max_id": 5784, "orders": {"5784": {"order_name": "老单", "tasks": {}}},'
        ' "order_watches": {"5784:ou_a": {"order_id": 5784, "open_id": "ou_a"}}}',
        encoding="utf-8")
    store = WatchStore(_TMP)
    store.load()
    assert "prod:5784" in store.orders
    assert store.watermark(ENV_PROD) == 5784
    assert "prod:5784:ou_a" in store.order_watches


def _state() -> Any:
    from app.domains.cmdb_watch.store import TaskState

    return TaskState(state=1, label="未执行", creator="devuser",
                     name="任务", kind="sql")


# ── handler：链接自动识别环境 ───────────────────────────────

class _RecordingWatcher:
    """只记录参数、不碰网络的 watcher 替身（handler 单测用）。"""

    def __init__(self) -> None:
        self.store = WatchStore(_TMP)
        self.fetched: list[tuple[int, str]] = []
        self.watched: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []

    async def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        self.fetched.append((order_id, env))
        return _order(order_id, env, [_task()])

    async def add_order_watch(self, order_id: int, open_id: str, *, env: str = ENV_PROD,
                              chat_id: str = "", display_name: str = "",
                              order: Order | None = None) -> None:
        self.watched.append((order_id, env))

    async def remove_order_watch(self, order_id: int, open_id: str,
                                 env: str = ENV_PROD) -> bool:
        self.removed.append((order_id, env))
        return env == ENV_UAT


def _handler(watcher: _RecordingWatcher) -> Any:
    from app.agents.handlers import cmdb_watch_handler as H
    from app.config import RouterConfig

    _cleanup()
    handler = H.CmdbWatchAgentHandler(
        RouterConfig(base_url="http://llm.test", api_key="test"),
        cmdb_config=CmdbWatchConfig(enabled=True, base_url="http://cmdb.test",
                                    state_path=_TMP))
    handler.bind_watcher(watcher)
    return handler


def test_watch_link_derives_uat_env(monkeypatch) -> None:
    """贴 UatOrderDetail 链接 = 明确指定 UAT，不需要用户再说一次。"""
    monkeypatch.setattr("app.agents.handlers.cmdb_watch_handler.get_caller_context",
                        lambda: {"sender_id": "ou_a", "chat_id": "", "sender_name": "我"})
    watcher = _RecordingWatcher()
    handler = _handler(watcher)
    result = asyncio.run(handler.execute_tool("watch_cmdb_order", {
        "order_id": "http://cmdb.x.local/order/UatOrderDetail?order_id=1924"}))
    assert watcher.fetched == [(1924, ENV_UAT)]
    assert watcher.watched == [(1924, ENV_UAT)]
    assert "UAT" in result.data["reply"]


def test_watch_explicit_env_argument(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.handlers.cmdb_watch_handler.get_caller_context",
                        lambda: {"sender_id": "ou_a", "chat_id": "", "sender_name": "我"})
    watcher = _RecordingWatcher()
    handler = _handler(watcher)
    asyncio.run(handler.execute_tool("watch_cmdb_order",
                                     {"order_id": 1924, "env": "uat"}))
    assert watcher.fetched == [(1924, ENV_UAT)]


def test_watch_defaults_to_prod(monkeypatch) -> None:
    monkeypatch.setattr("app.agents.handlers.cmdb_watch_handler.get_caller_context",
                        lambda: {"sender_id": "ou_a", "chat_id": "", "sender_name": "我"})
    watcher = _RecordingWatcher()
    handler = _handler(watcher)
    asyncio.run(handler.execute_tool("watch_cmdb_order", {"order_id": 5910}))
    assert watcher.fetched == [(5910, ENV_PROD)]


def test_unwatch_falls_back_to_other_env(monkeypatch) -> None:
    """用户记错环境时不要只说「你没关注过」——另一环境里可能真有。"""
    monkeypatch.setattr("app.agents.handlers.cmdb_watch_handler.get_caller_context",
                        lambda: {"sender_id": "ou_a", "chat_id": "", "sender_name": "我"})
    watcher = _RecordingWatcher()
    handler = _handler(watcher)
    result = asyncio.run(handler.execute_tool("unwatch_cmdb_order",
                                              {"order_id": 1924, "env": "prod"}))
    # 桩里只有 UAT 那条能删掉 → 回执必须说明真实命中的是 UAT
    assert watcher.removed == [(1924, ENV_PROD), (1924, ENV_UAT)]
    assert "UAT" in result.data["reply"]
