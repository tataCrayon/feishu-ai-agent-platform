"""MCP：把进程内能力的工具面投影出来 — dsh 会话的执行入口。

## 为什么需要这个 server

dsh 迁移后，能力清单（技能目录）与执行入口（工具）是**两条独立的线**：
技能目录只放「名字 + 描述」进路由候选集，真正动手要靠 `session/new` 里挂上的
MCP 工具。迁移前每个 Handler 自带 Function Calling 循环，工具只在旧编排链路里
可达；dsh 链路不挂 MCP 就等于把能力做成「只会说不会做」的空壳
（实测：数字先知回答「本会话没有这个执行入口」，定时任务回答「创建未开放」）。

本 server 在**子进程里**现场装配 Handler，把它们的 `tools` 原样导出、调用转发
回去。不复制工具 schema、不在本文件里写业务判断——`handler.tools` 是唯一事实源，
少一份就少一份漂移。

## 为什么用低层 Server 而不是 FastMCP

工具 schema 是现成的 OpenAI Function Calling 格式，FastMCP 的装饰器会按 Python
签名**重新推导** schema：参数名、枚举、必填项都会跟原 schema 对不上（实测
`additionalProperties` 这类自由字典直接丢）。低层 `Server` 允许直接返回
`mcp.types.Tool`，schema 原样透传，模型看到的参数与 Handler 自己期望的一致。

## 身份从哪来（安全边界）

Handler 需要 `sender_id / chat_id` 才能把订阅绑到正确的人。dsh 的 MCP 子进程
按**会话**拉起（实测：同一 spec 两个 session 得到两个 pid），所以身份可以在
进程启动时从 env 固定，不会串台：

    PROMATE_CALLER_SENDER_ID / PROMATE_CALLER_SENDER_NAME / PROMATE_CALLER_CHAT_ID

`main.py` 给某个 chat 建会话时按该 chat 生成 spec。env 里没有身份时
（如 `verify_routing.py` 这类离线回归）工具仍可枚举与只读查询，凡是**写操作
或绑定身份**的调用一律拒绝并说明原因——宁可不做，也不替某个猜出来的人做。

## 哪些工具不投影

- 绑死卡片按钮回调的：PRD 整理（`flush_prd_watch`）要等用户点「采纳/修正」，
  脱离飞书链路无法完成确认。
- 定时任务的**写操作照常投影**：创建走 `ScheduledTaskService.submit()`（不依赖
  `InboundMessage`，回执卡片照样发到本会话），暂停/恢复/取消按发送者身份鉴权
  （本人或 L2）。没有审批门要绕过——创建即生效，只是不再要求「调用方手里有
  消息对象」这种实现细节。

不投影 ≠ 能力缺席：路由层照常把这些请求交给飞书主链路，执行走正规入口。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from app.agents.handlers import AgentHandlerRegistry, auto_discover_handlers
from app.config import load_config

logger = logging.getLogger("mcp.capabilities")

SERVER_NAME = "capabilities"

# 需要活会话/审批卡回调的工具：不投影（见模块 docstring）
_NEEDS_LIVE_CHANNEL: dict[str, str] = {
    "flush_prd_watch": "整理当前群讨论要等卡片按钮回调，脱离飞书链路无法完成确认",
}

# 依赖启动期单例（ctx 注入），子进程里拿不到的不投影。
# 目前为空：ctx 已按 main 的同一份构造补齐（见 registry()），skill_catalog 也就通了。
_NEEDS_BOOT_SINGLETON: set[str] = set()

# 绑定调用者身份的工具：没有注入身份时必须拒绝（只读查询不受限）
_NEEDS_CALLER = {
    "subscribe_cmdb_watch", "unsubscribe_cmdb_watch", "watch_cmdb_order",
    "unwatch_cmdb_order",
}

# 查询类工具：不「绑定」身份，但答案因人而异（「我的订阅」）。没有身份时不拒绝，
# 只是如实说「不知道你是谁」——用户问状态时宁可拿到半份答案，也不要一句看不出来
# 原因的「请在飞书会话里发起」。
_PERSONAL_READS = {
    "cmdb_watch_status", "list_cmdb_watches",
}

_CALLER_ENV = (
    ("sender_id", "PROMATE_CALLER_SENDER_ID"),
    ("sender_name", "PROMATE_CALLER_SENDER_NAME"),
    ("chat_id", "PROMATE_CALLER_CHAT_ID"),
)

_registry: AgentHandlerRegistry | None = None
_load_error = ""
_config: Any = None
# 启动期预热标记：重依赖（数字先知的 numpy/yfinance）的导入放在 server 起来时做，
# 不能懒到第一次 tool call 里——见 `_preload_heavy_engines`。
_preloaded = False

# 进程内已装配的旁路服务（cmdb / prd 的 watcher 归属方）。子进程恒为空。
_live_watchers_registry: dict[str, Any] = {}


def register_live_watchers(**services: Any) -> None:
    """由启动方注入旁路服务实例（`cmdb=` / `prd=`）。

    只做登记，不做装配：服务实例的构造顺序（先建 registry、再建 watcher）
    属于启动流程的知识，塞进本模块会让它反向依赖主入口。
    """
    _live_watchers_registry.clear()
    _live_watchers_registry.update({k: v for k, v in services.items() if v is not None})


def _live_watchers() -> dict[str, Any]:
    """本进程内已装配的服务实例（dsh MCP 子进程里恒为空）。

    为什么还要读：这些工具在 `main.py` 进程内（旧编排链路 / 单测）也可能被
    调用，那里 watcher 已由启动流程注入。子进程读不到时返回空字典，
    由 `_ensure_watcher` 就地建一个只读档案版。
    """
    # 注册表由启动方填：本模块不能 import `app.main`——那是分层表里不存在的一条
    # 反向边（mcp → 主入口），也把「装工具面」绑死在「起整个 bot」上（单测与
    # 回归脚本因此要先付一遍主入口的 import 副作用）。
    return dict(_live_watchers_registry)


def app_config() -> Any:
    """进程内配置单例（MCP 子进程只读一次）。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _skill_catalog_store(cfg: Any) -> Any:
    """团队 Skill 目录 store（未启用返回 None → handler 拒绝上线，这是有意的）。

    与 `main._build_skill_catalog_store` 同一份构造：这里是**读同一份快照**给
    MCP 子进程用，别让它自己扫 git（那是启动期一次性动作，子进程不该重复拉仓）。
    """
    sc = cfg.skill_catalog
    if not sc.enabled:
        return None
    try:
        from app.domains.skill_catalog.scan import CatalogScanner
        from app.domains.skill_catalog.store import CatalogStore

        root = Path(__file__).resolve().parents[2]
        bare = sc.bare_dir if Path(sc.bare_dir).is_absolute() else str(root / sc.bare_dir)
        snap = (sc.snapshot_path if Path(sc.snapshot_path).is_absolute()
                else str(root / sc.snapshot_path))
        return CatalogStore(
            CatalogScanner(repo_url=sc.repo_url, bare_dir=bare,
                           skip_branches=sc.skip_branches,
                           fetch_timeout_sec=sc.fetch_timeout_sec),
            snapshot_path=snap,
            refresh_interval_hours=sc.refresh_interval_hours,
        )
    except Exception as e:
        logger.warning("[mcp.capabilities] skill catalog store 装配失败：%s", e)
        return None


def _reply(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        isError=not payload.get("ok", False),
    )


class _ArchiveOnlyClient:
    """子进程影子 watcher 的取数客户端：默认只认本地档案，但**用户主动指定工单时可真读一次**。

    为什么默认不留真客户端：影子进程会跑订阅回执的统计逻辑（比如「当前跟踪中的单里
    有几张含你的任务」），留着真客户端就会在 50 人订阅时多出一份内网请求。

    但「关注/取消关注某张工单」是用户当场下的指令，回执要念工单名与当前进度——
    这一条不读就等于把 `watch_cmdb_order` 整个打死（实测：dsh 里关注任何单都回
    「读不到工单」，用户以为能力不存在）。所以 `fetch_order` 走真客户端，
    频率天然由「用户手动关注的次数」决定，与轮询无关。
    """

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url
        self._reader: Any = None

    def _real(self) -> Any:
        if self._reader is None:
            from app.domains.cmdb_watch.client import CmdbClient, load_session_id

            self._reader = CmdbClient(base_url=self.base_url, session_id=load_session_id())
        return self._reader

    @property
    def session_id(self) -> str:
        """读本地会话文件，不登录、不探活。

        状态工具把它念给用户（「列表登录态：有效/缺失」）：这个进程不发请求，
        真去探活等于为一句状态回执多打两次内网。会话有没有过期由主进程在下一轮
        发现时才知道——这里只如实反映「手上有没有一把会话」，不冒称已验证。
        恒返回空串会让状态回执永远说「缺失」，与主进程日志里的「登录态有效」自相矛盾。
        """
        from app.domains.cmdb_watch.client import load_session_id

        return load_session_id()

    def fetch_order(self, order_id: int, env: str = "prod") -> Any:
        return self._real().fetch_order(order_id, env)

    def fetch_status(self, order_id: int, env: str = "prod") -> Any:
        from app.domains.cmdb_watch.client import CmdbError

        raise CmdbError("MCP 子进程不轮询 CMDB（提醒由主进程轮询）")

    order_exists = fetch_status

    def list_recent_order_ids(self, env: str = "prod") -> list[int]:
        return []


def _ensure_watcher(handler: Any, kind: str) -> None:
    """子进程里没有 main 的 watcher：按同一份配置就地建一个**只读档案版**。

    为什么不能照搬 main 的「未接线」：dsh 的 MCP 子进程天生没有 main 的运行时
    对象，那句提示会把所有 CMDB/PRD 工具永久打死——配置明明开着，用户看到的是
    「功能关了」。

    这个 watcher 只用来看档案（订阅表、关注表、状态计数），**不启动轮询**：
    轮询属于常驻的 main 进程，子进程只是长命会话的一个影子。写操作（订阅/关注）
    落的是同一份 state.json，所以照样有效；副作用是提醒由 main 进程按时发出。
    """
    if getattr(handler, "_watcher", None) is not None:
        return
    try:
        cfg = app_config()
        if kind == "cmdb" and cfg.cmdb_watch.enabled:
            from pathlib import Path

            from app.domains.cmdb_watch.watcher import CmdbWatcher

            cw = cfg.cmdb_watch
            state = Path(cw.state_path)
            # 相对路径的基准是仓库根（与 main 的解析同口径），不是进程 cwd——
            # MCP 子进程的 cwd 取决于谁拉起它，落错地方的后果是「订阅了但收不到」。
            if not state.is_absolute():
                state = Path(__file__).resolve().parents[2] / state
                cw.state_path = str(state)
            watcher = CmdbWatcher(cw)
            # 影子 watcher 只登记订阅：订阅回执会为「订阅时已跟踪的单」算待提醒数量，
            # 那一步不该在子进程里再打一次内网。换成一个空实现的客户端让任何
            # 意外的全量读变成明确的错误，而不是默默多花一份网络开销。
            watcher.bind_client(_ArchiveOnlyClient(cw.base_url))
            handler.bind_watcher(watcher)
        elif kind == "prd" and cfg.prd_watch.enabled:
            from app.domains.prd_watch.service import PrdWatchService
            from app.domains.prd_watch.watcher import PrdWatcher

            # 子进程也要有完整 service：analyze_story / ingest_meeting / review_prd_ui
            # 全挂在它上面，只回 watcher 时这些工具会永久回「服务未接线」。
            # 这里**不 start()** —— 轮询与攒窗属于常驻 main 进程，子进程只是长命会话
            # 里的影子；卡片出口走纯 HTTP（与定时任务审批卡同一条出口）。
            handler.bind_watcher(PrdWatcher(cfg.prd_watch, cfg.router))
            service_binder = getattr(handler, "bind_service", None)
            if callable(service_binder):
                service_binder(PrdWatchService(cfg, send_card=_send_card_http))
    except Exception as e:
        logger.warning("[mcp.capabilities] %s watcher 就地装配失败：%s", kind, e)


async def _send_card_http(chat_id: str, card: dict[str, Any]) -> str:
    """发卡片（纯 HTTP，不依赖 bot 的 channel 对象）。

    返回 message_id：审批 handler 用它原地更新卡片（「已通过」），拿不到就返回空串，
    卡片状态由审批回执文本兜底。

    **失败必须抛，不能返回空串吞掉**：调用方（评审卡片、分析卡片）用「发得出去」
    来区分「已发卡片」与「没发出去」，吞掉异常就会让工具回执写「已发」，而群里
    什么都没有——用户看到的是一句假话，比报错难查得多。请示卡片这类「发不出去也
    不影响主流程」的调用方自己 catch 即可。
    """
    from app.protocols.feishu_send import send_card

    return send_card(chat_id, card)


def registry() -> AgentHandlerRegistry | None:
    """现场装配 Handler（子进程只做一次）。

    失败不抛：MCP server 起不来会让整个 `session/new` 失败（dsh 对 MCP 启动失败
    的处置是拒绝会话），用户看到的是「bot 完全不回话」——比少几个工具糟得多。
    """
    global _registry, _load_error
    if _registry is not None or _load_error:
        return _registry
    try:
        cfg = app_config()
        # ctx 必须补齐各 Handler 工厂读的键，否则它们会拿到 None 并在 Guard 里
        # 报「未启用」——配置明明是 true，用户看到的是「功能关了」，比报错更难查。
        _registry = auto_discover_handlers(cfg.router, {
            "case_mirror": cfg.case_mirror,
            "cmdb_watch": cfg.cmdb_watch,
            "prd_watch": cfg.prd_watch,
            # 团队 Skill 目录：没有 store 时 handler 会拒绝上线（空目录上线等于
            # 让模型查一份假清单），所以这里按 main 的同一份构造补齐。
            "skill_catalog": _skill_catalog_store(cfg),
        })
        # watcher 只在 main 进程里存在；子进程按需就地建只读档案版
        watchers = _live_watchers()
        for name, watcher in (
            ("cmdb-watch-agent", watchers.get("cmdb")),
            ("prd-watch-agent", watchers.get("prd")),
        ):
            handler = _registry.get(name)
            if handler is None:
                continue
            if watcher is not None:
                # watcher 只在具体 Handler（prd/cmdb）上声明，AgentHandler 基类没有；
                # getattr 兜底比把这条加到基类更诚实——基类不该为两个子类长接口。
                binder = getattr(handler, "bind_watcher", None)
                if callable(binder):
                    binder(getattr(watcher, "watcher", watcher))
            else:
                _ensure_watcher(handler, "cmdb" if name.startswith("cmdb") else "prd")

        # 定时任务的审批卡也走 HTTP：子进程没有 channel 对象，但卡片必须发得出去，
        # 否则「创建成功」只是建了个没人审批的档案。
        sched = _registry.get("schedule-agent")
        service = getattr(sched, "_service", None)
        if service is not None and getattr(service, "_send_card", None) is None:
            service.wire(
                send_card_callback=_send_card_http,
                reply_callback=None,
                update_card_callback=None,
            )
        logger.info("[mcp.capabilities] handlers: %s", _registry.list())
    except Exception as e:
        _load_error = f"{type(e).__name__}: {e}"
        logger.warning("[mcp.capabilities] handler 装配失败：%s", _load_error)
    return _registry


def caller() -> dict[str, str]:
    """本会话的调用方身份（main.py 按 chat 注入 env）。"""
    return {key: os.environ.get(env, "") for key, env in _CALLER_ENV}


def _preload_heavy_engines() -> None:
    """启动期把重依赖导入（数字先知的 numpy/yfinance），别等第一次 tool call。

    Windows 上实测：同一个 `import numpy`，在 argparse 期（server 启动）0.7s，
    在 server 跑起来之后的 tool call 里 25s+ 才返回——调用方早超时了。
    数字先知的引擎是延迟加载的（`DigitalOracleHandler._get_engine`），真跑起来
    就是「取数永远超时」；这里提前到启动期付这笔钱，代价是一次性的 1.2s。
    """
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    try:
        from app.agents.handlers.digital_oracle_handler import _load_engine

        _load_engine()
        logger.info("[mcp.capabilities] 数字先知引擎已预热")
    except Exception as e:  # 引擎装不上不阻断：工具本身会如实报错
        logger.warning("[mcp.capabilities] 数字先知引擎预热失败：%s", e)


def tool_specs() -> list[tuple[str, dict[str, Any]]]:
    """(handler_name, tool_schema)，已滤掉不投影的工具。"""
    reg = registry()
    if reg is None:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for name in reg.list():
        handler = reg.get(name)
        if handler is None:
            continue
        for tool in handler.tools:
            fn = tool.get("function") or {}
            tool_name = str(fn.get("name") or "")
            if not tool_name:
                continue
            if tool_name in _NEEDS_LIVE_CHANNEL or tool_name in _NEEDS_BOOT_SINGLETON:
                continue
            out.append((name, tool))
    return out


async def dispatch(tool_name: str, args: dict[str, Any]) -> types.CallToolResult:
    """把调用转给对应 Handler，回执统一成 JSON。"""
    if tool_name in _NEEDS_LIVE_CHANNEL:
        return _reply({"ok": False, "error": (
            f"{tool_name} 不在本入口开放：{_NEEDS_LIVE_CHANNEL[tool_name]}"
            "（请在飞书会话里直接发起，审批卡会在会话内流转）")})
    if tool_name in _NEEDS_BOOT_SINGLETON:
        return _reply({"ok": False, "error": (
            f"{tool_name} 需要的启动期数据源未装载，本入口不提供；"
            "请在飞书会话里发起，或由管理员确认该能力已启用")})
    reg = registry()
    if reg is None:
        return _reply({"ok": False, "error": (
            f"能力装载失败：{_load_error or 'handler 未装配'}"
            "（检查本机 config.toml 与依赖，见 bot 日志）")})

    for name in reg.list():
        handler = reg.get(name)
        if handler is None:
            continue
        names = {str((t.get("function") or {}).get("name")) for t in handler.tools}
        if tool_name not in names:
            continue
        who = caller()
        if tool_name in _NEEDS_CALLER and not who["sender_id"]:
            return _reply({"ok": False, "error": (
                f"{tool_name} 要绑到你本人的飞书身份（订阅/关注只认调用者），"
                "本入口没拿到身份——请在飞书会话里发起这条请求")})
        if tool_name in _PERSONAL_READS and not who["sender_id"]:
            # 不拒绝：全局那部分（订阅人数、轮询轮次、登录态）照样有用，
            # 「我的订阅」那行由 handler 说明拿不到身份，比整条不回话好。
            logger.info("[mcp.capabilities] %s 无调用方身份，仅返回全局状态", tool_name)
        try:
            from app.orchestrator.context import set_caller_context

            # Handler 走 contextvars 读调用方（进程内原样沿用，不改成参数传递——
            # 那会让旧编排链路与这里出现两套身份来源）。env 是本会话已固定的身份。
            set_caller_context({
                "sender_id": who["sender_id"],
                "sender_name": who["sender_name"],
                "chat_id": who["chat_id"],
            })
            result = await handler.execute_tool(tool_name, args)
        except Exception as e:
            logger.warning("[mcp.capabilities] %s failed: %s", tool_name, e)
            return _reply({"ok": False,
                           "error": f"{tool_name} 执行失败：{type(e).__name__}: {str(e)[:200]}"})
        if not result.success:
            return _reply({"ok": False, "error": result.error or "执行失败（无错误信息）"})
        data = result.data
        if isinstance(data, dict) and "reply" in data:
            # Handler 已经把用户可见文案写好了：原样给，别让模型再改写一遍
            return _reply({"ok": True, "reply": str(data["reply"]),
                           **{k: v for k, v in data.items() if k != "reply"}})
        return _reply({"ok": True, "result": data})
    return _reply({"ok": False, "error": f"没有能力承载工具 {tool_name}（能力可能已下线）"})


app: Server[Any, Any] = Server(SERVER_NAME)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    """导出 Handler 的工具定义（schema 原样透传，见模块 docstring）。"""
    tools: list[types.Tool] = []
    for _handler, spec in tool_specs():
        fn = spec["function"]
        tools.append(types.Tool(
            name=str(fn["name"]),
            description=str(fn.get("description") or ""),
            inputSchema=dict(fn.get("parameters") or {"type": "object", "properties": {}}),
        ))
    logger.info("[mcp.capabilities] 导出 %d 个工具", len(tools))
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    return await dispatch(name, arguments)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream, write_stream,
            app.create_initialization_options(),
        )


def main() -> None:
    import anyio

    # 必须在 anyio.run 之前：重依赖在事件循环里懒导入会卡到调用方超时（见函数注释）
    _preload_heavy_engines()
    anyio.run(_run)


__all__: list[str] = ["SERVER_NAME", "caller", "dispatch", "main", "registry", "tool_specs"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
