"""cmdb-watch-agent Handler 单测 — 订阅入口的身份与范围，零网络零模型。

跑法：python -m pytest tests/agents/handlers/test_cmdb_watch_handler.py -q

为什么要单独测 Handler：订阅/关注是**写操作**，身份只能来自
`get_caller_context()`（MCP 子进程 env 注入）。这里钉死三件事：
- 身份从 contextvars 取，模型给的参数里没有命令身份的口子；
- `kinds` 参数的三种形态（不传/空/具体）语义不同，不能被归一成同一个；
- 没有任何订阅时，工具回执要引导到「自助订阅」，而不是让人找管理员。
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from app.agents.handlers import cmdb_watch_handler as H
from app.config import CmdbWatchConfig, RouterConfig
from app.domains.cmdb_watch.store import WatchStore
from app.domains.cmdb_watch.watcher import CmdbWatcher
from app.orchestrator.context import set_caller_context

_TMP_STATE = str(pathlib.Path(tempfile.gettempdir())
                 / "feishu_agent_platform_cmdb_watch_test" / "handler_state.json")


def _cfg() -> CmdbWatchConfig:
    return CmdbWatchConfig(enabled=True, base_url="http://cmdb.test",
                           state_path=_TMP_STATE, interval_sec=60, scan_limit=10,
                           probe_span=0, initial_scan=0, refresh_sec=900)


def _handler() -> H.CmdbWatchAgentHandler:
    path = pathlib.Path(_TMP_STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".tmp"):
        p = pathlib.Path(_TMP_STATE + suffix)
        if p.exists():
            p.unlink()
    handler = H.CmdbWatchAgentHandler(
        RouterConfig(base_url="http://llm.test", api_key="test"), cmdb_config=_cfg())
    handler.bind_watcher(CmdbWatcher(_cfg(), store=WatchStore(_TMP_STATE)))
    return handler


def _as(open_id: str = "ou_me", name: str = "张三") -> None:
    set_caller_context({"sender_id": open_id, "chat_id": open_id, "sender_name": name})


# ── kinds 参数归一 ──────────────────────────────────────────

def test_normalize_kinds_semantics() -> None:
    assert H._normalize_kinds(None) is None          # 不传 = 不改范围
    assert H._normalize_kinds([]) is None            # 「全部」= 清空范围
    assert H._normalize_kinds(["sql"]) == ["sql"]
    assert H._normalize_kinds("deploy") == ["deploy"]
    assert H._normalize_kinds(["SQL", "发布", "bogus"]) == ["sql", "deploy"]
    assert H._normalize_kinds("bogus") is None       # 全非法 → 不改范围（不误清空）


def test_kinds_text_readable() -> None:
    assert "全部四类" in H._kinds_text([])
    assert H._kinds_text(["sql", "deploy"]) == "SQL 变更、应用发布"


# ── 订阅入口 ────────────────────────────────────────────────

def test_subscribe_binds_caller_and_kinds() -> None:
    handler = _handler()
    _as("ou_alice", "Alice")

    result = asyncio.run(handler.execute_tool(
        "subscribe_cmdb_watch", {"cmdb_user": "alice.wang@example.com", "kinds": ["sql"]}))

    assert result.success
    sub = handler._watcher.store.subscribers["ou_alice"]
    assert sub.cmdb_user == "alice.wang"      # 邮箱前缀归一
    assert sub.kinds == ["sql"]
    assert "SQL 变更" in result.data["reply"]
    assert "隐私边界" in result.data["reply"]


def test_subscribe_target_follows_conversation() -> None:
    """在哪订阅就发哪：群里订阅发群，私聊订阅发私聊。

    用户直觉预期（也是原始诉求）：在群里说「订阅」就该在群里收到提醒。
    私聊的 chat_id 与 open_id 是两套 id，按 chat_id 发私聊不一定通，
    所以私聊统一表示成空 chat_id、由 target 回落 open_id。
    """
    handler = _handler()
    set_caller_context({"sender_id": "ou_alice", "chat_id": "oc_team",
                        "sender_name": "Alice"})
    asyncio.run(handler.execute_tool("subscribe_cmdb_watch", {"cmdb_user": "alice.wang"}))
    assert handler._watcher.store.subscribers["ou_alice"].target == "oc_team"

    # 换到私聊再订阅一次 = 改绑推送目标回到私聊
    set_caller_context({"sender_id": "ou_alice", "chat_id": "oc_p2p_9f2a",
                        "sender_name": "Alice"})
    asyncio.run(handler.execute_tool("subscribe_cmdb_watch", {"cmdb_user": "alice.wang"}))
    assert handler._watcher.store.subscribers["ou_alice"].target == "ou_alice"


def test_subscribe_receipt_states_the_real_target() -> None:
    """回执必须说真话：群里订阅时不能还写「推送目标：私聊」。"""
    handler = _handler()
    set_caller_context({"sender_id": "ou_bob", "chat_id": "oc_team",
                        "sender_name": "Bob"})
    result = asyncio.run(handler.execute_tool(
        "subscribe_cmdb_watch", {"cmdb_user": "bob.li"}))
    lines = result.data["reply"].splitlines()
    target_line = next(ln for ln in lines if ln.startswith("推送目标"))
    assert "当前群" in target_line and "私聊" not in target_line


def test_subscribe_without_identity_is_refused() -> None:
    """拿不到发送者身份：拒绝，绝不用模型口述的用户名替代归属。"""
    handler = _handler()
    set_caller_context({})

    result = asyncio.run(handler.execute_tool(
        "subscribe_cmdb_watch", {"cmdb_user": "alice.wang"}))

    assert not result.success
    assert handler._watcher.store.subscribers == {}


def test_subscribe_rejects_bad_username() -> None:
    handler = _handler()
    _as()
    result = asyncio.run(handler.execute_tool(
        "subscribe_cmdb_watch", {"cmdb_user": "张三"}))
    assert "不像 CMDB 用户名" in result.data["reply"]


def test_two_people_only_see_their_own_subscription() -> None:
    """A 订阅后再让 B 查状态：B 看到的是「未订阅」，A 的记录不泄漏给 B。"""
    handler = _handler()
    _as("ou_a", "A")
    asyncio.run(handler.execute_tool("subscribe_cmdb_watch", {"cmdb_user": "a.test"}))

    _as("ou_b", "B")
    result = asyncio.run(handler.execute_tool("cmdb_watch_status", {}))
    reply = result.data["reply"]
    assert "未订阅" in reply
    assert "a.test" not in reply


def test_status_shows_kind_scope() -> None:
    handler = _handler()
    _as()
    asyncio.run(handler.execute_tool(
        "subscribe_cmdb_watch", {"cmdb_user": "devuser", "kinds": ["deploy"]}))
    result = asyncio.run(handler.execute_tool("cmdb_watch_status", {}))
    assert "应用发布" in result.data["reply"]


def test_list_watches_when_empty_points_to_self_service() -> None:
    """没有任何关注时，回执要把人引到自助订阅（而不是「找管理员」）。"""
    handler = _handler()
    _as()
    result = asyncio.run(handler.execute_tool("list_cmdb_watches", {}))
    reply = result.data["reply"]
    assert "订阅 CMDB 工单提醒" in reply
    assert "不需要管理员" in reply          # 明确自助，不是「找管理员开权限」


def test_tools_are_read_only() -> None:
    """只读红线：工具描述里不能出现「执行/触发/修改」这类写动作承诺。"""
    names = {t["function"]["name"] for t in H.TOOLS}
    assert names == {
        "subscribe_cmdb_watch", "watch_cmdb_order", "list_cmdb_watches",
        "unwatch_cmdb_order", "cmdb_watch_status", "unsubscribe_cmdb_watch",
    }
    # 业务层只读由 client 保证（只有 GET 读接口）；这里锁住工具面没有写动作命名
    assert not any("trigger" in n or "execute" in n or "update" in n for n in names)


@pytest.mark.parametrize("raw,expected", [
    (5778, 5778),
    ("5778", 5778),
    ("https://cmdb/order/OrderDetail?order_id=5778", 5778),
    ("", 0),
    ("abc", 0),
])
def test_parse_order_id(raw: object, expected: int) -> None:
    assert H.CmdbWatchAgentHandler._parse_order_id(raw) == expected


# ── 按单号关注：通道错误按人隔离 ─────────────────────────────

def test_watch_error_is_scoped_per_person() -> None:
    """50 人订阅后一次内网抖动不能让所有人的关注一起被摘掉。"""
    handler = _handler()
    watcher = handler._watcher
    for open_id in ("ou_a", "ou_b"):
        watcher.store.add_order_watch(5778, open_id, order_name="测试单")
    stats = watcher.stats

    async def run() -> None:
        for _ in range(watcher._cfg.watch_error_limit):
            await asyncio.to_thread(watcher.store.load)
            # 关注键现在带环境前缀（UAT 与生产同号不同单）
            await watcher._on_watch_error("prod:5778", stats, open_id="ou_a")

    asyncio.run(run())
    assert "prod:5778:ou_a" not in watcher.store.order_watches
    assert "prod:5778:ou_b" in watcher.store.order_watches
