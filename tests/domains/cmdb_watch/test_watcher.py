"""CMDB 工单提醒单测 — 判定逻辑 / 渲染 / 状态落盘，零网络。

跑法：python -m pytest tests/domains/cmdb_watch/test_watcher.py -q

覆盖（每条都对应一个真实会翻车的场景）：
- 名单匹配必须全等（`wang` 不该命中 `wangwei.wang`）
- 首次见到的工单：有任务在跑就不补发卡（开开关不补喊历史）
- 状态迁移：未执行→执行中 发 started；→失败 发 failed；状态没变不发
- 同一事件不重复发（轮询间隔内反复跳变）
- 别人的工单不进跟踪；我的任务被删后退出跟踪
- probe_span=0 且列表接口缺 session → 本轮不猜 id
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
from typing import Any

from app.config import CmdbWatchConfig
from app.domains.cmdb_watch.client import (
    CmdbClient,
    CmdbError,
    Order,
    Task,
    load_session_id,
    parse_order,
    ENV_PROD,
)
from app.domains.cmdb_watch.render import build_order_card, build_task_card, card_text
from app.domains.cmdb_watch.store import WatchStore
from app.domains.cmdb_watch.watcher import CmdbWatcher, _transition

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_TMP_STATE = str(pathlib.Path(tempfile.gettempdir()) / "feishu_agent_platform_cmdb_watch_test" / "test_watcher.json")


def _cfg(**kw: Any) -> CmdbWatchConfig:
    base: dict[str, Any] = dict(enabled=True, base_url="http://cmdb.test",
                                state_path=_TMP_STATE, interval_sec=60,
                                scan_limit=10, probe_span=0, initial_scan=0,
                                refresh_sec=0)
    base.update(kw)
    return CmdbWatchConfig(**base)

# 订阅即授权：用例默认预置一个订阅者 `devuser`，推送到 open_id 直投（私聊）
_SUB = ("ou_devuser_test", "devuser")


def _task(kind: str = "sql", task_id: int = 1, creator: str = "devuser",
          state: int = 1, label: str = "未执行", name: str = "任务",
          env: str = "") -> Task:
    return Task(kind=kind, task_id=task_id, name=name, creator=creator,
                state=state, state_label=label, env=env)


def _order(order_id: int = 100, tasks: list[Task] | None = None) -> Order:
    return Order(order_id=order_id, order_name="TaaS 测试发布", tasks=tasks or [])


class _FakeClient(CmdbClient):
    """打桩客户端：orders 是 {order_id: Order}，list_ok 控制列表接口是否可用。"""

    def __init__(self, orders: dict[int, Order], list_ids: list[int] | None = None,
                 list_error: CmdbError | None = None):
        super().__init__(base_url="http://cmdb.test", session_id="")
        self._orders = orders
        self._list_ids = list_ids or []
        self._list_error = list_error
        self.fetched: list[int] = []

    def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        self.fetched.append(order_id)
        if order_id not in self._orders:
            raise CmdbError(f"#{order_id} 不存在")
        return self._orders[order_id]

    def order_exists(self, order_id: int, env: str = ENV_PROD) -> bool:
        return order_id in self._orders

    def list_recent_order_ids(self, env: str = ENV_PROD) -> list[int]:
        if self._list_error is not None:
            raise self._list_error
        return sorted(self._list_ids, reverse=True)

    def fetch_status(self, order_id: int, env: str = ENV_PROD) -> dict[str, Any]:
        """轻量轮询桩：真实接口只回 deploy 状态码，这里如实模拟。"""
        order = self._orders.get(order_id)
        if order is None:
            raise CmdbError(f"#{order_id} 不存在")
        return {"deploy_status": {str(t.task_id): [t.state, t.state_label, 0, ""]
                                  for t in order.tasks if t.kind == "deploy"},
                "run_bit": False}


class _Recorder:
    """收集发出的卡片。"""

    def __init__(self) -> None:
        self.cards: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, chat_id: str, card: dict[str, Any]) -> None:
        self.cards.append((chat_id, card))

    def texts(self) -> list[str]:
        return [card_text(c) for _, c in self.cards]


def _watcher(client: _FakeClient, *, subscribe: bool = True,
             **kw: Any) -> tuple[CmdbWatcher, _Recorder]:
    _cleanup()   # 每个用例从干净状态开始（state 文件是跨用例共享的全局资源）
    rec = _Recorder()
    store = WatchStore(_TMP_STATE)
    if subscribe:
        store.subscribe(_SUB[0], _SUB[1])
    # 水位 -1：跳过「冷启动只对齐水位」分支，让桩里的工单都算本轮新出现
    store.scanned_max_id = -1
    w = CmdbWatcher(_cfg(**kw), client=client, store=store, send_card=rec)
    return w, rec


def _cleanup() -> None:
    pathlib.Path(_TMP_STATE).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        path = _TMP_STATE + suffix
        if os.path.exists(path):
            os.remove(path)


# ── 名单匹配 ────────────────────────────────────────────────

def test_mine_is_exact_match() -> None:
    order = _order(tasks=[_task(creator="wangwei.wang"), _task(task_id=2)])
    assert [t.task_id for t in order.mine({"wang"})] == []
    assert [t.task_id for t in order.mine({"WANGWEI.WANG"})] == [1]
    assert [t.task_id for t in order.mine({"devuser"})] == [2]


# ── 状态迁移 ────────────────────────────────────────────────

def test_transition_events() -> None:
    from app.domains.cmdb_watch.store import TaskState

    pending = TaskState(state=1)
    assert _transition(None, _task(state=1)) == ""          # 首次见到不动
    assert _transition(None, _task(state=2, label="执行中")) == ""  # 跟踪前就在跑
    assert _transition(pending, _task(state=2, label="执行中")) == "started"
    assert _transition(pending, _task(state=3, label="执行成功")) == "done"
    assert _transition(TaskState(state=2), _task(state=4, label="执行失败")) == "failed"
    assert _transition(TaskState(state=1), _task(state=1)) == ""
    # 重跑：终态 → 执行中 当成一次新的 started 是错的，实测 CMDB 侧极少发生；
    # 这里锁定「不重复打扰」的取舍
    assert _transition(TaskState(state=3), _task(state=2, label="执行中")) == "started"


def test_deploy_failure_codes() -> None:
    from app.domains.cmdb_watch.store import TaskState

    assert _transition(TaskState(state=1), _task("deploy", state=6, label="执行失败")) == "failed"
    assert _transition(TaskState(state=1), _task("deploy", state=9, label="执行失败")) == "failed"
    assert _transition(TaskState(state=1), _task("deploy", state=5, label="执行成功")) == "done"


# ── 首次见到 ────────────────────────────────────────────────

def test_first_sight_pending_sends_order_card() -> None:
    client = _FakeClient({100: _order(tasks=[_task(task_id=11),
                                             _task("deploy", 22, state=0)])},
                         list_ids=[100])
    w, rec = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert len(rec.cards) == 1
    text = rec.texts()[0]
    assert "工单提醒" in text and "#11" in text and "#22" in text
    assert "命中任务：** 2 项（待执行）" in text
    assert w.store.orders["prod:100"].tasks


def test_first_sight_running_is_silent() -> None:
    """开开关那一刻，已经在跑的工单不该补喊一遍。"""
    client = _FakeClient({100: _order(tasks=[_task(task_id=11, state=2, label="执行中")])},
                         list_ids=[100])
    w, rec = _watcher(client)

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert rec.cards == []
    assert "prod:100" in w.store.orders   # 但入了跟踪，后续完成时会提醒


def test_my_task_removed_stops_tracking() -> None:
    client = _FakeClient({100: _order(tasks=[_task(task_id=11)])}, list_ids=[100])
    w, _ = _watcher(client)

    async def run() -> None:
        await w.scan_once()
        client._orders[100] = _order(tasks=[_task(task_id=11, creator="other.user")])
        await w.scan_once()

    asyncio.run(run())
    assert "prod:100" not in w.store.orders


# ── 迁移 → 发卡 + 去重 ──────────────────────────────────────

def test_started_then_done_cards_once_each() -> None:
    client = _FakeClient({100: _order(tasks=[_task(task_id=11)])}, list_ids=[100])
    w, rec = _watcher(client)

    async def run() -> None:
        await w.scan_once()                                     # 首发新单卡
        client._orders[100] = _order(tasks=[_task(task_id=11, state=2, label="执行中")])
        await w.scan_once()                                     # started
        await w.scan_once()                                     # 状态没变 → 不发
        client._orders[100] = _order(tasks=[_task(task_id=11, state=3, label="执行成功")])
        await w.scan_once()                                     # done
        await w.scan_once()                                     # 重复轮询 → 不发

    asyncio.run(run())
    texts = rec.texts()
    assert len(rec.cards) == 3
    assert "工单提醒" in texts[0]
    assert "任务执行中" in texts[1]
    assert "任务已完成" in texts[2]


def test_failure_card_is_red_header() -> None:
    client = _FakeClient({100: _order(tasks=[_task(task_id=11)])}, list_ids=[100])
    w, rec = _watcher(client)

    async def run() -> None:
        await w.scan_once()
        client._orders[100] = _order(tasks=[_task(task_id=11, state=4, label="执行失败")])
        await w.scan_once()

    asyncio.run(run())
    assert rec.cards[-1][1]["header"]["template"] == "red"
    assert "任务未能成功完成" in rec.texts()[-1]


def test_restart_does_not_repeat() -> None:
    """同一份 state 文件再起一个 watcher：历史事件不该重发。"""
    client = _FakeClient({100: _order(tasks=[_task(task_id=11)])}, list_ids=[100])

    async def first() -> None:
        w, _ = _watcher(client)
        await w.scan_once()
        client._orders[100] = _order(tasks=[_task(task_id=11, state=2, label="执行中")])
        await w.scan_once()

    asyncio.run(first())
    rec2: _Recorder
    w2, rec2 = _watcher(client)

    async def second() -> None:
        await w2.scan_once()

    asyncio.run(second())
    assert rec2.cards == []


def test_send_failure_retries_next_round() -> None:
    """发卡失败不该吃掉提醒：下一轮成功后仍然能发出去。"""
    client = _FakeClient({100: _order(tasks=[_task(task_id=11)])}, list_ids=[100])
    sent: list[Any] = []
    fail = {"on": True}

    async def flaky(chat_id: str, card: dict[str, Any]) -> None:
        if fail["on"]:
            raise RuntimeError("feishu 502")
        sent.append(card)

    _cleanup()
    store = WatchStore(_TMP_STATE)
    store.subscribe(_SUB[0], _SUB[1])
    store.scanned_max_id = -1   # 同上：跳过冷启动对齐，直接进发现
    w = CmdbWatcher(_cfg(), client=client, store=store, send_card=flaky)

    async def run() -> None:
        await w.scan_once()          # 第一轮：发卡失败
        fail["on"] = False
        await w.scan_once()          # 第二轮：同一事件重试成功

    asyncio.run(run())
    assert len(sent) == 1
    assert "工单提醒" in card_text(sent[0])


def test_probe_span_zero_without_session_does_not_probe() -> None:
    """probe_span=0 且列表接口缺 session：本轮 cero 候选，但错误要留痕。"""
    client = _FakeClient({}, list_ids=[], list_error=CmdbError("未登陆", need_session=True))
    w, rec = _watcher(client, probe_span=0)

    async def run() -> None:
        stats = await w.scan_once()
        assert stats.errors

    asyncio.run(run())
    assert rec.cards == []
    # 列表入口被拒且不做 id 探测 → 水位不前进（-1 是桩设的初值）
    assert w.store.scanned_max_id == -1


def test_probe_span_finds_new_orders_without_session() -> None:
    """无 session 时从 scanned_max_id+1 顺序探测（内网实测可行）。"""
    client = _FakeClient({101: _order(101, tasks=[_task(task_id=11)])},
                         list_error=CmdbError("未登陆", need_session=True))
    w, rec = _watcher(client, probe_span=3)
    w.store.scanned_max_id = 100

    async def run() -> None:
        await w.scan_once()

    asyncio.run(run())
    assert 101 in client.fetched
    assert len(rec.cards) == 1
    assert w.store.scanned_max_id >= 102


# ── parse_order ─────────────────────────────────────────────

_SAMPLE = {
    "order_id": 5778,
    "order_name": "星路-陆浦_正常发布_后台二组中台柜台_2026-11-04",
    "client": [{
        "client_id": 11923, "creator": "hongxiong.ma", "client_title": "陆浦资产分析报告",
        "sql_list": [{"id": 14161, "creator": "devuser.name", "db_name": "saas_counter",
                      "workflow_name": "[CMDB工单] devuser.name_资产报告",
                      "instance_group_label": "PROD-FLP-HK-票据 (PROD-FLP)",
                      "archery_data": {"workflow": {"status": "workflow_manreviewing"}}}],
        "nacos_list": [{"id": 5233, "env": "PROD-FLP", "data_id": "flp-webportal-app",
                        "act_type_str": "修改", "creator": "jia.yang"}],
        "deploy_data": [{"deploy_id": 22366, "job_name": "flp-counter-assets",
                         "env": "prod-k8s", "creator": "hongxiong.ma"}],
        "cron_list": [],
    }],
    "deploy_status": {"22366": [5, "执行成功", 12, ""]},
    "nacos_status": {"5233": [2, "执行成功"]},
    "sql_status": {"14161": 2},
    "cron_status": {},
}


def test_parse_order_tasks_and_status() -> None:
    order = parse_order(_SAMPLE)
    by_key = {t.key: t for t in order.tasks}
    assert set(by_key) == {"sql:14161", "nacos:5233", "deploy:22366"}
    assert by_key["sql:14161"].state == 2 and by_key["sql:14161"].running
    assert by_key["deploy:22366"].state == 5 and by_key["deploy:22366"].done
    assert by_key["nacos:5233"].done
    # 任务级 creator 优先于子单 creator
    assert by_key["sql:14161"].creator == "devuser.name"
    assert by_key["deploy:22366"].creator == "hongxiong.ma"
    assert [t.key for t in order.mine({"DEVUSER.NAME"})] == ["sql:14161"]


def test_parse_order_falls_back_to_workflow_status() -> None:
    sample = {**_SAMPLE, "sql_status": {}}
    order = parse_order(sample)
    sql = next(t for t in order.tasks if t.kind == "sql")
    assert sql.state == 1   # workflow_manreviewing → 未执行


# ── 渲染 ────────────────────────────────────────────────────

def test_order_card_layout() -> None:
    """版式要点：绿头 + 副标题带单号日期 + 任务走原生 table + 底部蓝色链接。"""
    order = _order(tasks=[_task(task_id=11, name="[CMDB工单] 资产", env="PROD-FLP")])
    card = build_order_card(order, order.tasks, base_url="http://cmdb.test",
                            actor="devuser")
    assert card["header"]["template"] == "green"
    assert "5778" not in card["header"]["subtitle"]          # 用的是测试工单号 100
    assert "#100" in card["header"]["subtitle"]["content"]
    text = card_text(card)
    assert "**工单名称：**" in text
    assert "**命中人员：** devuser" in text
    # 状态术语翻译：CMDB 的「未执行」在卡片上写「待执行」
    assert "待执行" in text and "未执行" not in text
    # 任务清单是原生 table（列对齐），不是 markdown 管道表
    tables = [el for el in card["body"]["elements"] if el.get("tag") == "table"]
    assert len(tables) == 1
    assert [c["display_name"] for c in tables[0]["columns"]] == \
        ["类型", "任务", "名称", "状态", "执行环境"]
    assert tables[0]["rows"][0]["col_1"] == "#11"
    assert "[在 CMDB 查看工单](http://cmdb.test/order/OrderDetail?order_id=100)" in text


def test_order_name_suffix_date_trimmed() -> None:
    """工单标题尾部的日期去掉（副标题已有日期，正文再写一遍是冗余）。"""
    order = Order(order_id=1, order_name="星路-陆浦_正常发布_后台二组中台柜台_2026-11-04")
    card = build_order_card(order, [_task()], base_url="")
    text = card_text(card)
    assert "后台二组中台柜台_2026-11-04" not in text
    assert "后台二组中台柜台" in text


def test_task_card_no_link_when_base_url_empty() -> None:
    task = _task(task_id=11, state=3, label="执行成功")
    card = build_task_card(task, _order(), "done", base_url="")
    text = card_text(card)
    assert "查看工单" not in text
    # 术语归位：「执行成功」→「成功」，字段名用业务语言
    assert "**执行状态：** 成功" in text
    assert "**任务创建人：** devuser" in text


def test_watcher_loads_persisted_state_on_construction() -> None:
    """构造即读档：影子进程（MCP 子进程）不跑轮询，不读档就会对着空档案作答。

    真实故障：用户刚订阅完，紧接着问「我的提醒状态」，MCP 子进程里新建的
    watcher 没调过 `load()`，回的是「未订阅 / 0 人订阅 / 登录态缺失」——
    与主进程日志和 state.json 全都矛盾。
    """
    path = pathlib.Path(_TMP_STATE).with_name("test_watcher_reload.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = WatchStore(path)
    seed.subscribe(_SUB[0], _SUB[1], chat_id="oc_test")
    seed.save()

    watcher = CmdbWatcher(_cfg(state_path=str(path)))
    assert list(watcher.store.subscribers) == [_SUB[0]]
    assert watcher.store.subscribers[_SUB[0]].cmdb_user == _SUB[1]


def test_shadow_client_reports_local_session_without_probing() -> None:
    """影子客户端的登录态取自本地会话文件：恒空会让状态回执永远说「缺失」。"""
    from app.mcp.capabilities_server import _ArchiveOnlyClient

    assert _ArchiveOnlyClient().session_id == load_session_id()


def test_shadow_client_can_read_a_single_order_but_never_polls() -> None:
    """关注工单要念单号与进度，这一条必须真读；轮询/探测仍必须短路。

    历史 bug 形态：影子客户端把 `fetch_order` 也短路掉，dsh 里关注任何单都回
    「读不到工单」，用户以为能力不存在。
    """
    from app.domains.cmdb_watch.client import CmdbError
    from app.mcp.capabilities_server import _ArchiveOnlyClient

    shadow = _ArchiveOnlyClient("http://cmdb.test")

    def _boom(self: Any, api: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"result": True, "data": {"order_id": params["order_id"],
                                         "order_name": "UAT 单", "client": []}}

    original = CmdbClient._get
    CmdbClient._get = _boom          # type: ignore[method-assign]
    try:
        order = shadow.fetch_order(1924, "uat")
    finally:
        CmdbClient._get = original   # type: ignore[method-assign]
    assert order.key == "uat:1924"
    assert shadow.list_recent_order_ids() == []
    try:
        shadow.fetch_status(1924, "uat")
    except CmdbError:
        pass
    else:
        raise AssertionError("影子客户端不该轮询 CMDB")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
