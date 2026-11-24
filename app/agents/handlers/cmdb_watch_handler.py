"""cmdb-watch-agent handler — CMDB 工单提醒的自助入口（按姓名订阅 + 按单号关注）。

两条链路都靠发送者身份落地：
- **按姓名**：「订阅 CMDB 工单提醒 devuser」→ 他创建的任务逐条提醒；
- **按单号**：「关注工单 5778 的执行情况」→ 整单开始/完成各提醒一次。
两者可并存，退订/取消只影响各自那一条。不需要管理员改配置，也不需要额外授权链接。

身份边界（硬约束）：
- 只认 `get_caller_context()` 里的 `sender_id`，从不接受用户口述别人的 open_id；
- 一个 open_id 只绑一个 CMDB 用户名，重复订阅 = 改绑（幂等，不会出现
  一个人同时收两个用户名任务的情况）；
- 不提供「订阅/关注别人的单」——CMDB 的 `creator` 与工单号是唯一归属依据，
  替人决定推送内容只会制造噪音。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.handlers import AgentHandler, ToolResult
from app.config import CmdbWatchConfig, RouterConfig
from app.domains.cmdb_watch.client import (
    ENV_PROD,
    ENV_UAT,
    TASK_KINDS,
    CmdbError,
    normalize_env,
)
from app.domains.cmdb_watch.progress import (
    order_phase,
    progress_text,
    task_failure_names,
)
from app.domains.cmdb_watch.render import KIND_LABEL, short_order_name
from app.domains.cmdb_watch.store import PHASE_DONE, PHASE_RUNNING
from app.domains.cmdb_watch.subscriber import SubscribeError, normalize_cmdb_user
from app.orchestrator.context import get_caller_context

logger = logging.getLogger("agents.handlers.cmdb_watch")

def _window_of(chat_id: str) -> str:
    """当前会话的推送目标：群（`oc_`）用群 id，私聊留空 = 直投本人 open_id。

    群聊里订阅就发那个群、私聊订阅就发私聊——这是用户的直觉预期。私聊的
    chat_id 与 open_id 是两套 id，按它发反而不一定通，所以「私聊」统一表示成
    空 chat_id，由 `Subscriber.target` 回落到 open_id。
    """
    # 群 id 以 `oc_` 开头；私聊的会话 id 也常是 `oc_` 形式，但那不是可直投的
    # 目标（按它发不一定通）。所以只把「群」当目标，其余（私聊）一律回落 open_id。
    return chat_id if chat_id.startswith("oc_") and not chat_id.startswith("oc_p2p") else ""


def _target_text(target: str) -> str:
    """推送目标的用户可见描述。"""
    if target.startswith("oc_"):
        return "**当前群**（在这里订阅就发这里；到别的会话再订阅一次即改绑）"
    return "我和你之间的**私聊**"


# 工单号：纯数字，或链接里 `order_id=5778` 那段
_ORDER_ID_RE = re.compile(r"order_id=(\d+)", re.IGNORECASE)

# 工单链接里带环境：`/order/UatOrderDetail?...` 是 UAT，`/order/OrderDetail?...` 是生产
_ENV_URL_RE = re.compile(r"/order/(uat)?orderdetail", re.IGNORECASE)


def _env_from_text(raw: Any) -> str:
    """从用户给的单号/链接里读出环境；读不出返回空串（调用方回落默认）。

    **先判 UAT**：`uatorderdetail` 里也含 `orderdetail`，顺序反了永远判成生产。
    """
    text = str(raw or "")
    match = _ENV_URL_RE.search(text)
    if match:
        return ENV_UAT if match.group(1) else ENV_PROD
    lowered = text.lower()
    if "uat" in lowered or "测试环境" in text:
        return ENV_UAT
    return ""

# 用户口中的类型说法 → 内部 kind。模型通常直接传英文，这里兜住中文误传。
_KIND_ALIASES = {
    "sql": "sql", "s": "sql",
    "deploy": "deploy", "release": "deploy", "发布": "deploy", "应用发布": "deploy",
    "nacos": "nacos", "配置": "nacos", "nacos配置": "nacos", "配置变更": "nacos",
    "cron": "cron", "定时任务": "cron", "job": "cron",
}


def _normalize_kinds(raw: Any) -> list[str] | None:
    """工具参数 `kinds` → 内部 kind 列表；None = 不改（保持原订阅范围）。

    空列表在语义上是「全收」，与「没传」在存储层同形（都是 []），所以这里
    把 `[]` 归一到 None：用户在对话里说「全部提醒」时，意图是清掉过滤，
    而工具调用传 `[]` 与不传都不该被理解成「什么都别推」。
    """
    if raw is None:
        return None
    items = [raw] if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for item in items:
        key = str(item).strip().lower()
        kind = _KIND_ALIASES.get(key)
        if kind and kind not in out:
            out.append(kind)
    return out or None


def _kinds_text(kinds: list[str]) -> str:
    if not kinds:
        return "全部四类（SQL 变更 / 应用发布 / Nacos 配置 / 定时任务）"
    return "、".join(KIND_LABEL.get(k, k) for k in kinds)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "subscribe_cmdb_watch",
            "description": "订阅 CMDB 工单提醒：用户提供自己的 CMDB 用户名（通常形如 "
                "devuser，或他的公司邮箱前缀）后，今后生产与 UAT 工单里只要有他创建的任务，"
                "新工单建立 / 开始执行 / 执行完成时都会推送（两个环境都覆盖，卡片标出 UAT）。"
                "推送目标跟随订阅所在的会话："
                "在群里订阅就发那个群，私聊订阅就发私聊。可用 kinds 只订某几类"
                "任务（SQL / 应用发布 / Nacos 配置 / 定时任务），不传=四类全收。用户说"
                "「订阅 CMDB 工单提醒 / 有我的工单就提醒我 / 只提醒我发的 SQL / 帮我盯着"
                "我的发布工单」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmdb_user": {
                        "type": "string",
                        "description": "用户自己的 CMDB 用户名，例如 devuser。"
                            "用户没说时先问他要，不要猜。",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(TASK_KINDS),
                        },
                        "description": "只对这几类任务提醒。不传 = 全收（默认）。"
                            "sql=SQL 变更、deploy=应用发布、nacos=Nacos 配置、"
                            "cron=定时任务。用户说「只提醒 SQL」时传 [\"sql\"]；"
                            "说「全部都要」时不要传这个字段。",
                    },
                },
                "required": ["cmdb_user"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "watch_cmdb_order",
            "description": "关注某一张指定的 CMDB 工单的执行进度：开始执行时提醒一次，"
                "全部任务执行完成时再提醒一次（有失败任务会标红）。用户说「关注工单 "
                "5778 的执行情况 / 盯一下这张单 / 这张单发完告诉我 / 工单开始执行告诉我」"
                "并给出工单号或工单链接时使用。用户没给单号时先问，不要猜。"
                "UAT 工单同样支持：链接是 UatOrderDetail 时自动识别，或显式传 env=uat。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "CMDB 工单号（OrderDetail / UatOrderDetail 链接里的 "
                            "order_id，如 5778）。用户给的是链接时从链接里取。",
                    },
                    "env": {
                        "type": "string",
                        "enum": ["prod", "uat"],
                        "description": "工单所属环境。生产与 UAT 的工单号是两套独立编号，"
                            "同号不同单：链接里是 UatOrderDetail 或用户说「UAT / 测试环境」"
                            "时传 uat，否则不传（默认生产）。不确定时问用户，不要猜。",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cmdb_watches",
            "description": "列出我正在关注的 CMDB 工单及其执行进度（开始执行/执行完成）。"
                "用户问「我关注了哪些工单 / 我盯的单现在什么进度」时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unwatch_cmdb_order",
            "description": "取消对某张 CMDB 工单的关注。用户说「别盯 5778 了 / 取消工单"
                "关注」时使用。只取消发送者本人对这张单的关注，不影响按姓名的订阅。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "description": "要取消关注的工单号"},
                    "env": {
                        "type": "string",
                        "enum": ["prod", "uat"],
                        "description": "该工单所属环境（与关注时一致）。不传 = 生产。",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cmdb_watch_status",
            "description": "查 CMDB 工单提醒状态：我自己订阅了哪个 CMDB 用户名、"
                "当前有多少人订阅、跟踪中多少张工单、上次轮询结果。用户问「CMDB 工单提醒"
                "开着吗 / 我订阅的是谁 / 为什么没收到提醒」时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unsubscribe_cmdb_watch",
            "description": "取消我自己的 CMDB 工单提醒订阅。用户说「别再提醒我工单了 / "
                "取消 CMDB 订阅」时使用。只取消发送者本人，不动别人的订阅。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


class CmdbWatchAgentHandler(AgentHandler):
    """cmdb-watch-agent 处理器。watcher 由 main 在 channel 就绪后注入。"""

    agent_name = "cmdb-watch-agent"
    tools = TOOLS

    def __init__(self, config: RouterConfig,
                 cmdb_config: CmdbWatchConfig | None = None):
        super().__init__(config)
        self._cw = cmdb_config or CmdbWatchConfig()
        self._watcher: Any = None

    def bind_watcher(self, watcher: Any) -> None:
        """main 启动时注入共享实例（未注入时工具给「未启用」回执，不自建一套）。"""
        self._watcher = watcher

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        if tool_name == "subscribe_cmdb_watch":
            return await self._subscribe(str(args.get("cmdb_user") or ""),
                                         kinds=args.get("kinds"))
        if tool_name == "watch_cmdb_order":
            return await self._watch_order(args.get("order_id"), env=args.get("env"))
        if tool_name == "list_cmdb_watches":
            return await self._list_watches()
        if tool_name == "unwatch_cmdb_order":
            return await self._unwatch_order(args.get("order_id"), env=args.get("env"))
        if tool_name == "cmdb_watch_status":
            return await self._status()
        if tool_name == "unsubscribe_cmdb_watch":
            return await self._unsubscribe()
        return ToolResult(error=f"未知工具: {tool_name}")

    # ── 前置检查 ────────────────────────────────────────────
    def _guard(self) -> str:
        if not self._cw.enabled:
            return "CMDB 工单提醒未启用（config.toml `[projects.cmdb_watch].enabled=false`）"
        if self._watcher is None:
            return "提醒服务未接线（bot 启动时未挂载），请重启后再试"
        return ""

    @staticmethod
    def _caller() -> tuple[str, str, str]:
        """→ (open_id, chat_id, display_name)。拿不到 open_id 说明不是消息链路。"""
        ctx = get_caller_context() or {}
        return (str(ctx.get("sender_id") or ""), str(ctx.get("chat_id") or ""),
                str(ctx.get("sender_name") or ""))

    # ── 工具实现 ────────────────────────────────────────────
    async def _subscribe(self, raw_user: str, *, kinds: Any = None) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, chat_id, name = self._caller()
        if not open_id:
            return ToolResult(error="拿不到你的飞书身份，请在飞书里 @ 我后再试")
        try:
            cmdb_user = normalize_cmdb_user(raw_user)
        except SubscribeError as e:
            return ToolResult(data={"reply": f"⚠️ {e}"})

        selected = _normalize_kinds(kinds)
        watcher = self._watcher
        sub = await watcher.add_subscriber(open_id, cmdb_user, display_name=name,
                                           kinds=selected, chat_id=_window_of(chat_id))
        # 改绑时 store 只在 chat_id 非空才覆盖（空 = 不改），所以从群换回私聊
        # 必须显式清空，否则推送目标永远停在那个群。
        if sub.chat_id != _window_of(chat_id):
            sub.chat_id = _window_of(chat_id)
        pending = sum(1 for o in watcher.store.tracked_orders()
                      if any(t.creator.strip().lower() == sub.cmdb_key
                             and sub.wants(t.kind)
                             for t in watcher.store_tasks_as_tasks(o)))
        lines = [
            f"✅ 已订阅 CMDB 工单提醒：**{sub.cmdb_user}**",
            f"推送目标：{_target_text(sub.target)}",
            "提醒时机：工单里出现我创建的任务（新工单）· 任务开始执行 · 任务执行完成/失败",
            "提醒范围：**" + _kinds_text(sub.kinds) + "**",
            "隐私边界：只推 `creator` 是我本人的任务，不收别人的工单。",
        ]
        if pending:
            lines.append(f"当前跟踪中的工单里已有 {pending} 张含你的任务，"
                         "未执行的会在下一轮提醒。")
        lines.append("换会话就换推送目标：在群里订阅就发到那个群，私聊订阅就发私聊；"
                     "再订阅一次即改绑。")
        lines.append("要改绑：再发一次「订阅 CMDB 工单提醒 <你的用户名>」；"
                     "要换提醒范围：带上类型说「只提醒 SQL / 只看发布 / 全部提醒」；"
                     "要退订：说「取消 CMDB 工单提醒」。")
        return ToolResult(data={"reply": "\n".join(lines)})

    # ── 指定工单关注 ────────────────────────────────────────
    @staticmethod
    def _parse_order_id(raw: Any) -> int:
        """工单号解析：接受纯数字、字符串、或整条 OrderDetail 链接。"""
        text = str(raw or "").strip()
        if not text:
            return 0
        match = _ORDER_ID_RE.search(text)
        if match:
            return int(match.group(1))
        return int(text) if text.isdigit() else 0

    async def _watch_order(self, raw_order_id: Any, *, env: Any = "") -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, chat_id, name = self._caller()
        if not open_id:
            return ToolResult(error="拿不到你的飞书身份，请在飞书里 @ 我后再试")
        order_id = self._parse_order_id(raw_order_id)
        if not order_id:
            return ToolResult(data={"reply": "⚠️ 请给我 CMDB 工单号（形如 5778，或直接贴 "
                                             "OrderDetail / UatOrderDetail 链接）。"})
        # 环境优先级：链接里带的 > 工具参数 > 生产。链接最可信——用户是直接复制的地址
        target_env = _env_from_text(raw_order_id) or normalize_env(str(env or ""))
        label = "UAT" if target_env == ENV_UAT else "生产"
        try:
            order = await self._watcher.fetch_order(order_id, target_env)
        except CmdbError as e:
            return ToolResult(data={"reply": f"⚠️ 读不到{label}工单 #{order_id}：{str(e)[:120]}\n"
                                             "若这条链接是 UatOrderDetail，请确认单号与该环境一致；"
                                             "否则请确认单号存在、且这个 CMDB 账号能看这张单。"})
        await self._watcher.add_order_watch(
            order_id, open_id, env=order.env, chat_id=chat_id,
            display_name=name, order=order)
        lines = [
            f"✅ 已关注{label}工单 #{order_id}：**{short_order_name(order)}**",
            f"当前进度：{progress_text(order)}",
            "提醒时机：开始执行时一次、全部任务结束再一次（有失败会标红）。",
        ]
        if order_phase(order) == PHASE_DONE:
            lines.append("这张单的任务已经全部结束，不会再推送后续提醒；"
                         "需要复盘请看工单页。")
        elif order_phase(order) == PHASE_RUNNING:
            lines.append("这张单已经在执行中，所以只会提醒「执行完成」。")
        failed = task_failure_names(order)
        if failed:
            lines.append("当前失败任务：" + "；".join(failed))
        lines.append(f"要取消：说「别盯 {order_id} 了」。")
        logger.info("[cmdb_watch] %s 关注工单 #%s（%s）", name or open_id[-6:],
                    order_id, progress_text(order))
        return ToolResult(data={"reply": "\n".join(lines)})

    async def _list_watches(self) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, _, _ = self._caller()
        watches = self._watcher.store.order_watches_for(open_id)
        if not watches:
            lines = [
                "你还没有关注任何 CMDB 工单。说「关注工单 5778 的执行情况」即可开始。",
                "",
                "按类型/按单号只覆盖了一部分场景；如果你想「凡是我提交的工单都提醒我」",
                "（SQL / 应用发布 / Nacos 配置 / 定时任务，可按类型筛），用按姓名订阅：",
                "说「订阅 CMDB 工单提醒 <你的 CMDB 用户名>」即可，不需要管理员。",
            ]
            return ToolResult(data={"reply": "\n".join(lines)})
        lines = [f"👀 我关注的 CMDB 工单（{len(watches)} 张）"]
        for watch in sorted(watches, key=lambda w: w.order_id, reverse=True):
            state = ("已结束" if watch.notified_done
                     else "执行中" if watch.notified_started else "待开始")
            lines.append(f"• #{watch.order_id} {watch.order_name or ''} — {state}")
        lines.append("说「关注工单 <单号>」增加，说「别盯 <单号> 了」取消。")
        return ToolResult(data={"reply": "\n".join(lines)})

    async def _unwatch_order(self, raw_order_id: Any, *, env: Any = "") -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, _, _ = self._caller()
        order_id = self._parse_order_id(raw_order_id)
        if not order_id:
            return ToolResult(data={"reply": "⚠️ 请告诉我要取消关注的工单号。"})
        target_env = _env_from_text(raw_order_id) or normalize_env(str(env or ""))
        label = "UAT" if target_env == ENV_UAT else "生产"
        if await self._watcher.remove_order_watch(order_id, open_id, target_env):
            return ToolResult(data={"reply": f"✅ 已取消对{label}工单 #{order_id} 的关注。"
                                             "按姓名的提醒订阅不受影响。"})
        # 环境记错时给出可执行的下一步，别让用户以为「明明关注过却说没有」
        other = ENV_PROD if target_env == ENV_UAT else ENV_UAT
        other_label = "UAT" if other == ENV_UAT else "生产"
        if await self._watcher.remove_order_watch(order_id, open_id, other):
            return ToolResult(data={"reply": f"✅ 已取消对{other_label}工单 #{order_id} 的关注"
                                             f"（你关注的是{other_label}那张，不是{label}）。"
                                             "按姓名的提醒订阅不受影响。"})
        return ToolResult(data={"reply": f"你本来就没有关注工单 #{order_id}，无需取消。"})

    async def _status(self) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, _, _ = self._caller()
        watcher = self._watcher
        snap = watcher.snapshot()
        store = snap.get("store") or {}
        mine = watcher.store.subscribers.get(open_id)
        lines = [
            "📋 CMDB 工单提醒状态",
            f"• 我的订阅：{mine.cmdb_user if mine else '未订阅（说「订阅 CMDB 工单提醒 <用户名>」）'}"
            + (f"　提醒范围：{_kinds_text(mine.kinds)}" if mine else ""),
            f"• 全部订阅者：{len(watcher.store.subscribers)} 人　"
            f"跟踪中工单：{store.get('tracked_orders', 0)} 张"
            f"（每轮实际轮询 {snap.get('poll_orders', 0)} 张）",
            f"• 轮询：每 {snap.get('interval_sec', 0)}s 一次，"
            f"已完成 {snap.get('rounds', 0)} 轮（上轮 {snap.get('last_round_ms', 0):.0f}ms）",
            f"• 累计发卡：{snap.get('cards_sent', 0)} 张"
            + (f"（发送失败 {snap['cards_failed']}）" if snap.get("cards_failed") else ""),
            f"• CMDB 列表登录态：{'有效' if snap.get('session') else '缺失（按 id 探测，发现可能晚一轮）'}",
        ]
        if snap.get("errors"):
            lines.append("• 最近错误：" + "；".join(str(e)[:80] for e in snap["errors"][:2]))
        return ToolResult(data={"reply": "\n".join(lines)})

    async def _unsubscribe(self) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        open_id, _, _ = self._caller()
        if not open_id:
            return ToolResult(error="拿不到你的飞书身份，请在飞书里 @ 我后再试")
        if await self._watcher.remove_subscriber(open_id):
            return ToolResult(data={"reply": "✅ 已取消你的 CMDB 工单提醒订阅；"
                                             "想恢复随时再说「订阅 CMDB 工单提醒 <用户名>」。"})
        return ToolResult(data={"reply": "你本来就没有订阅 CMDB 工单提醒，无需取消。"})


def build(config, ctx: dict) -> CmdbWatchAgentHandler:
    """自动发现约定工厂：watcher 要发消息能力，main 在 channel 就绪后 bind_watcher。"""
    return CmdbWatchAgentHandler(config, cmdb_config=ctx.get("cmdb_watch"))
