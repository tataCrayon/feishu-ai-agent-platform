"""LangGraph 编排层 — 意图识别 + 路由 + 复杂任务编排。

使用 LangGraph StateGraph 构建消息处理管线：
1. Router Node: LLM 意图识别 + 路由决策（纯 AI，无正则匹配）
2. Agent Nodes: 按 agent.yaml 的 transport 声明分派执行通道
3. Decompose Node: 复杂任务分解为多步
4. Gather Node: 结果聚合 + 格式化

Agent 执行通道（transport，声明于 bot/agents/*/agent.yaml）：
- handler:      进程内 Python Handler（app/agents/handlers/，自动发现注册）
- cc_connect:   Claude Code 执行通道（cc-connect 桥接，与 handler 平级的普通通道）
"""

from __future__ import annotations

import asyncio
import logging
import operator
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time as _now
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from app.agents.handlers import AgentHandlerRegistry
from app.agents.registry import AgentRegistry
from app.config import RouterConfig
from app.observability.task_record import (
    TaskRecord,
    decision_recorder,
    reply_store,
    task_recorder,
)
from app.orchestrator.direct import DirectAnswerLLM
from app.orchestrator.history import DEFAULT_PERSIST_DIR, SessionHistory
from app.orchestrator.reroute_rules import exclusive_domain_agents
from app.orchestrator.router import (
    AGENT_NAME_MAP,
    ROUTER_TOOLS,
    RouterLLM,
    RouterResult,
    build_router_tools,
)
from app.protocols.cc_connect.client import LATE_DELIVERY, CCConnectClient

logger = logging.getLogger("orchestrator.crew")


def _opening_thought(message: str) -> str:
    """进度卡第一句：从用户原话抽出在做什么，而不是固定套话。

    路由模型还没返回时只能用原文。去掉 @、引用包装和客套，留下关键词。
    """
    text = message or ""
    text = re.sub(r"@\S+", "", text)
    text = text.replace("（引用的上一条：", " ")
    text = text.replace("）", " ")
    for filler in ("你确认一下", "确认一下", "处理一下", "帮我", "一下", "这个", "那个"):
        text = text.replace(filler, " ")
    text = re.sub(r"\s+", " ", text).strip(" ，。,.、")
    if not text:
        return "看你这条消息"
    if len(text) > 28:
        text = text[:28].rstrip("，。,.、 ") + "…"
    return f"看「{text}」"


# ── 执行方自评标记（shared/self-check.md 约定）────────────────────────────
# 只认回复末尾的最后一行；没有标记就原样返回，绝不改写用户可见内容。
_QC_RE = re.compile(
    r"(?:\r?\n){1,2}<<<QC\s+quality=[\"']?(\w+)[\"']?"
    r"(?:\s+retry_with=[\"']([^\"']*)[\"']?)?\s*>>>\s*$",
    re.IGNORECASE,
)


def _extract_self_check(response: str) -> tuple[str, str, str | None]:
    """剥离回复末尾的自评标记。

    Returns:
        (干净回复, quality, retry_with) — 无标记时 (原回复, "ok", None)。
        剥离后为空（整条回复只有标记）时保留原回复，绝不产出空响应。
    """
    if not response:
        return "", "ok", None
    m = _QC_RE.search(response)
    if not m:
        return response, "ok", None
    clean = response[: m.start()].rstrip()
    quality = (m.group(1) or "ok").lower()
    retry = (m.group(2) or "").strip() or None
    if quality not in ("ok", "unclear", "failed"):
        quality = "ok"
    return (clean or response), quality, retry


# ── 附件下载回调（生产由 main.py 注入，测试可替换）────────────────────────
_attachment_downloader = None
_recent_file_channel = None


def set_attachment_downloader(fn):
    """注入附件下载回调 fn(resources) -> list[dict]，返回 attachment dict 列表。"""
    global _attachment_downloader
    _attachment_downloader = fn


def set_recent_file_channel(channel):
    """注入 channel，供聚合链路回看同一发送者最近的群文件。"""
    global _recent_file_channel
    _recent_file_channel = channel


# ── 迟到结果投递回调（cc-connect 超时转后台后，结果经此直发原会话）────────
_late_delivery_sender = None


def set_late_delivery_sender(fn):
    """注入 async fn(chat_id: str, text: str) —— main.py 启动时接线 channel.send。"""
    global _late_delivery_sender
    _late_delivery_sender = fn


# 进程内通道超时转后台后，仍在跑的任务引用（防 GC）。
# 与 cc-connect 的后台守望同一语义：不砍掉已在做的事，先让出并发额度 + 给用户
# 一句诚实回执，真结果落好后直发原会话。
_detached_tasks: set[Any] = set()

# 进程内通道超过这个墙钟就转后台（与 cc-connect 的 90s 同步窗口同口径）。
# agent.yaml 的 timeout_sec 仍作为「彻底放弃」的硬上限（见 _await_deadline）。
_INPROC_SYNC_WAIT_SEC = 90.0

# 分派超时的可区分错误标记（TaskRecord 按它统计「等太久转后台」而非当失败）
DISPATCH_TIMEOUT = "dispatch_timeout"


# ── LangGraph 状态定义 ────────────────────────────────────────────────────


class AgentState(TypedDict):
    """StateGraph 的完整状态，所有节点共享。"""

    # 输入字段
    input_message: str
    chat_id: str
    sender_id: str
    sender_name: str
    session_key: str
    trace_id: str
    span_id: str

    # 多轮语义：该会话最近 N 轮历史（最旧在前）
    history: list[dict]

    # 用户长期记忆上下文（MemoryCore L1-L3 召回，路由前注入；空 = 无记忆）
    memory_context: str

    # 路由决策回调：router 出结果后调用（direct/help 不调），签名
    # async fn(agent_display: str) -> None，供上层发上下文化回执
    notify_cb: Any

    # 处理进度回调（豆包式进度卡）：签名 async fn(event: dict) -> None。
    # event 形如 {"stage": str, "detail": str, "tool": str|None, "seq": int}，
    # 由 handler 循环逐工具调用；上层据此发卡+原地更新（update_card）。
    # 无回调（cc-connect 通道等）时 handler 静默跳过，不阻断主链。
    progress_cb: Any

    # 消息对象（含 resources 附件描述），由 on_message 透传
    message_obj: Any

    # 多模态媒体包（图片/视频 base64、文档解析文本、音频降级说明），
    # 由 main 层下载解析后传入；None = 纯文本消息（原链路零改动）
    media_bundle: Any

    # 路由字段
    route_to: str
    route_args: dict[str, Any]
    confidence: float

    # 复杂任务字段
    is_complex: bool
    sub_tasks: list[dict[str, Any]]
    sub_results: list[str]
    current_step: int

    # 执行字段
    response: str
    error: str | None
    duration_ms: float

    # 路由轨迹：驱动 TaskRecord.route_chain 落档 + 通道故障时的显示名回溯。
    route_trace: list[str]

    # 模型自评（判定内联在执行方，编排层只观测不裁决）：
    # - results_quality: ok | unclear | failed，执行方自报，缺省 ok
    # - retry_with: 执行方建议的换道目标，**只记录不执行**（避免重新引入
    #   第二次路由的割裂判定）
    results_quality: str
    retry_with: str | None

    # 结构化门禁拦截标记（error / ⚠️ 前缀 / 空响应，全程不调 LLM）
    gate_blocked: bool

    # 执行通道故障标记：仅 transport=cc_connect 的节点失败置位。
    # 置位后 gather 直接给「暂不可用」诚实文案——cc-connect 通道整体不可用
    # 时再换一个同通道 agent 只会空转一轮（最坏多等 90 秒），无意义。
    no_recover: bool

    # 意图路由决策（SPEC-20260912）：模型自报意图复述、是否被低置信度降级、
    # 澄清反问文本（route_to_clarify 时非空）
    intent_summary: str
    demoted: bool
    clarify_question: str

    # ReAct 多意图：router 一次响应带多个专业域 tool_calls 时，与主路由并行分派
    parallel_tasks: list[dict[str, Any]]

    # 任务档案 span 收集：各节点 append 执行记录，execute() 结束组装 TaskRecord
    spans: Annotated[list[dict[str, Any]], operator.add]


# ── CrewOrchestrator ──────────────────────────────────────────────────────


class CrewOrchestrator:
    """LangGraph 编排器 — 纯 LLM 意图识别 + 路由 + 复杂任务编排。

    Args:
        cc_client: cc-connect 客户端（可选执行通道之一，可为 None —
                   None 时仅 transport=cc_connect 的 agent 不可用，其余照常）。
        config: 路由配置。
        registry: AgentRegistry，transport/guard/help 清单的数据源。
        handler_registry: AgentHandlerRegistry，进程内 Python Handler 注册表。
    """

    def __init__(
        self,
        cc_client: CCConnectClient | None,
        config: RouterConfig,
        registry: AgentRegistry | None = None,
        handler_registry: AgentHandlerRegistry | None = None,
        memory_client: Any | None = None,
    ):
        self._cc_client = cc_client
        self._config = config
        self._registry = registry
        self._handler_registry = handler_registry or AgentHandlerRegistry()
        # MemoryCore 客户端（可选）：路由前召回用户长期记忆注入路由上下文
        self._memory_client = memory_client
        # transport → 执行协程的分派表：agent.yaml 声明执行通道，
        # 运行时查表分派（handler 运行时注册仍优先于 yaml 声明）
        self._dispatchers: dict[str, Callable[..., Awaitable[tuple[str, str | None]]]] = {
            "handler": self._run_handler_agent,
            "cc_connect": self._run_cc_connect_agent,
        }
        # 专属领域集合：从 agent.yaml 的 exclusive_domain 声明派生（数据驱动）
        self._exclusive_agents = exclusive_domain_agents(registry)
        # 从完整 registry 派生路由工具表（含 Skill 合成 Agent）— 单源注入各 LLM。
        # 模块级 ROUTER_TOOLS/AGENT_NAME_MAP 是 import 时的静态派生（不含 Skill）
        if registry is not None:
            derived_tools, self._agent_name_map = build_router_tools(registry)
        else:
            derived_tools, self._agent_name_map = ROUTER_TOOLS, AGENT_NAME_MAP
        self._router_llm = RouterLLM(config, tools=derived_tools,
                                     agent_name_map=self._agent_name_map,
                                     registry=registry)
        # direct_answer 自答引擎（专门生成环节，替代路由模型顺手 reply）
        self._direct_llm = DirectAnswerLLM(config)
        self._graph = self._build_graph()
        # 多轮语义历史缓冲（persist_dir 非空时重启不丢：logs/history/ 按 key 分文件）
        history_persist = getattr(config, "history_persist", True)
        persist_dir = DEFAULT_PERSIST_DIR if history_persist else None
        self._history = SessionHistory(
            config.history_max_turns, persist_dir=persist_dir
        ) if config.history_enabled else None
        # 每会话 tool-call 限流（窗口内滑动计数）
        self._tool_calls: dict[str, list[float]] = {}
        self._tool_rate_limit = getattr(config, "tool_rate_limit", 50)
        self._tool_rate_window_sec = getattr(config, "tool_rate_window_sec", 600)

    # ── 公开接口 ──

    async def execute(
        self,
        message: str,
        chat_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        session_key: str = "",
        trace_id: str = "",
        span_id: str = "",
        message_obj: Any = None,
        media_bundle: Any | None = None,
        on_routed: Any | None = None,
        on_progress: Any | None = None,
    ) -> str:
        """执行编排管线，返回回复文本。

        Args:
            message: 用户消息。
            chat_id: 飞书会话 ID。
            sender_id: 用户 open_id。
            sender_name: 用户显示名称。
            session_key: session key，不传则自动构建。
            trace_id: TraceContext.trace_id。
            span_id: TraceContext.span_id。
            message_obj: InboundMessage（含 resources 附件），用于附件下载与透传。
            media_bundle: MediaBundle 多模态媒体包（图片/视频 base64、文档文本），
                供路由/自答/判定 LLM 直接"看"媒体；None = 纯文本消息。
            on_routed: 路由决策回调 async fn(agent_display)，router 出结果后调用；
                direct_answer/help 不回调（秒回场景，无需"处理中"回执）。
            on_progress: 处理进度回调 async fn(event: dict)，handler 工具循环
                逐事件调用（豆包式进度卡数据源）；None = 不上报。

        Returns:
            回复文本。
        """
        session_key = session_key or f"feishu:{chat_id}:{sender_id}"

        # 拉取该会话最近多轮历史（最旧在前），注入状态供路由与子 Agent 使用
        history: list[dict] = []
        if self._history:
            history = await self._history.snapshot(chat_id, sender_id)

        # 澄清闭环（SPEC-20261002）：上一轮若有待确认反问，取出状态注入路由
        # 上下文——用户回「对，就是那个」「查库那个」时路由 LLM 能结合候选定向。
        # 取即删：只对下一轮消息有效。
        pending_clarify: dict[str, Any] | None = None
        if self._history:
            try:
                pending_clarify = await self._history.pop_pending(chat_id, sender_id)
            except Exception as e:
                logger.debug("[pending] pop failed: %s", e)

        # 召回用户长期记忆（L1-L3：画像/偏好/事实），注入路由上下文。
        # 失败/超时静默降级——记忆是增强项，绝不能挡主链。
        memory_context = ""
        if self._memory_client is not None:
            try:
                memory_context = await asyncio.wait_for(
                    self._memory_client.recall(
                        query=message,
                        session_key=session_key,
                        user_id=sender_id,
                    ),
                    timeout=3.0,
                )
                if memory_context:
                    logger.info(
                        "Memory context injected into routing (%d chars)",
                        len(memory_context),
                    )
            except Exception as e:
                logger.debug("Memory recall for routing skipped: %s", e)

        # pending 拼进 memory_context 尾部（同一注入通道，保持 user 帧结构不变；
        # router 侧已声明「与本次请求冲突时以消息原文为准」，确认语境同理）
        if pending_clarify:
            cands = "、".join(pending_clarify.get("candidates") or []) or "（无候选）"
            block = (
                f"[上一轮待确认意图：{pending_clarify.get('intent_summary', '')}；"
                f"候选：{cands}。用户本条消息若是对该确认的回答，请结合候选定向，"
                f"并给足置信度（不再反问）；若是新话题则忽略本块]"
            )
            memory_context = f"{memory_context}\n\n{block}" if memory_context else block
            logger.info(
                "[pending] clarify context injected: intent=%r candidates=%s",
                pending_clarify.get("intent_summary", "")[:40],
                pending_clarify.get("candidates"),
            )

        initial_state: AgentState = {
            "input_message": message,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "session_key": session_key,
            "trace_id": trace_id,
            "span_id": span_id,
            "history": history,
            "memory_context": memory_context,
            "notify_cb": on_routed,
            "progress_cb": on_progress,
            "message_obj": message_obj,
            "media_bundle": media_bundle,
            "route_to": "",
            "route_args": {},
            "confidence": 0.0,
            "intent_summary": "",
            "demoted": False,
            "clarify_question": "",
            "is_complex": False,
            "sub_tasks": [],
            "parallel_tasks": [],
            "sub_results": [],
            "current_step": 0,
            "response": "",
            "error": None,
            "duration_ms": 0.0,
            # 观测初始字段（判定内联在执行方，编排层只落档）
            "route_trace": [],
            "results_quality": "ok",
            "retry_with": None,
            "gate_blocked": False,
            "no_recover": False,
            "spans": [],
        }

        started = time.time()
        try:
            result = await self._graph.ainvoke(initial_state)
            self._write_task_record(
                result, message, chat_id, sender_id, sender_name,
                duration_ms=(time.time() - started) * 1000,
            )
            return result.get("response", "") or "⚠️ 处理出错，请稍后再试"
        except Exception as e:
            logger.exception("CrewOrchestrator execution failed: %s", e)
            return f"⚠️ 处理出错: {str(e)[:100]}"

    def _write_task_record(
        self,
        result: dict[str, Any],
        message: str,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        duration_ms: float,
    ) -> None:
        """把一次编排的完整 span 链组装为 TaskRecord 落盘。

        失败不影响主流程（档案是旁路产物）。
        """
        try:
            spans = result.get("spans", []) or []
            route_to = result.get("route_to", "")

            # final_agent：最后一个 agent 执行 span；无则按路由目标语义归类
            final_agent = ""
            for sp in reversed(spans):
                if sp.get("span") == "agent":
                    final_agent = sp.get("agent", "")
                    break
            if not final_agent:
                if route_to == "direct_answer":
                    final_agent = "direct-answer"
                elif route_to == "route_to_help":
                    final_agent = "help"
                elif route_to == "route_to_clarify":
                    final_agent = "clarify"
                elif result.get("is_complex"):
                    final_agent = "multi-step"

            # 首个 router span 携带意图决策（R4）
            router_span: dict[str, Any] = next(
                (sp for sp in spans if sp.get("span") == "router"), {}
            )

            error = result.get("error") or ""
            response = result.get("response", "") or ""
            # status 由结构化信号 + 模型自评判定，不依赖第二次 LLM 裁决
            gate_hit = bool(result.get("gate_blocked")) or response.startswith("⚠️")
            self_failed = str(result.get("results_quality", "ok")) == "failed"
            status = "success"
            if error:
                status = "error"
            elif gate_hit or self_failed:
                status = "failed"

            # token/cost 汇总（仅 LLM 调用 span 携带）；llm_calls = 带 model 的 span 数
            tokens_total = sum(int(sp.get("tokens", 0) or 0) for sp in spans)
            cost_total = sum(float(sp.get("cost_usd", 0) or 0) for sp in spans)
            model = next(
                (sp.get("model", "") for sp in spans if sp.get("model")), ""
            )
            llm_calls = sum(1 for sp in spans if sp.get("model"))

            record = TaskRecord(
                task_id=result.get("trace_id", "") or uuid.uuid4().hex[:16],
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                query=message[:300],
                final_agent=final_agent,
                route_chain=list(result.get("route_trace", []) or []),
                results_quality=str(result.get("results_quality", "ok") or "ok"),
                retry_with=result.get("retry_with"),
                gate_blocked=bool(result.get("gate_blocked", False)),
                status=status,
                duration_ms=round(duration_ms, 1),
                response_preview=response[:200],
                error=error[:200],
                tokens_total=tokens_total,
                cost_usd=round(cost_total, 6),
                model=model,
                llm_calls=llm_calls,
                intent_summary=str(router_span.get("intent_summary", "") or ""),
                confidence=float(router_span.get("confidence", 0.0) or 0.0),
                demoted=bool(router_span.get("demoted", False)),
                clarify_question=str(result.get("clarify_question", "") or "")[:200],
            )
            task_recorder.write(record)

            # 回复全文落盘（控制台「报告」tab 消费；主档案只留 200 字 preview）
            reply_store.write(
                record.task_id,
                response,
                meta={
                    "task_id": record.task_id,
                    "ts": record.ts,
                    "agent": final_agent,
                    "status": status,
                    "query": message[:200],
                },
            )

            # R4：路由决策日志（logs/decisions/）—— query → tool → confidence → demoted
            decision_recorder.write({
                "task_id": record.task_id,
                "ts": record.ts,
                "query": message[:200],
                "tool": router_span.get("tool", route_to),
                "args": {
                    "query": message[:100],
                    "intent_summary": record.intent_summary,
                    "confidence": record.confidence,
                },
                "demoted": record.demoted,
                "original_target": str(router_span.get("original_target", "") or ""),
                "is_fallback": bool(router_span.get("is_fallback", False)),
                "model": router_span.get("model", ""),
                "prompt_version": router_span.get("prompt_version", ""),
                "latency_ms": router_span.get("duration_ms", 0),
                "tokens": router_span.get("tokens", 0),
            })
        except Exception as e:
            logger.debug("Task record write skipped: %s", e)

    def get_graph_info(self) -> dict[str, Any]:
        """获取图结构信息（节点列表）。"""
        nodes = list(self._graph.get_graph().nodes.keys())
        return {"nodes": nodes, "node_count": len(nodes)}

    def rebuild(self) -> tuple[bool, str]:
        """热重建：registry → 路由工具表 → graph 编译 → 原子替换。

        能力热插拔（app/ops/capability/reloader.py）在审批通过后调用。
        编译成功才替换 self._graph 引用——在途请求继续跑旧图，
        编译失败自动保留旧图继续服务（零中断回滚语义）。
        """
        try:
            if self._registry is None:
                return False, "registry 未注入，无法重建"
            derived_tools, agent_name_map = build_router_tools(self._registry)
            self._agent_name_map = agent_name_map
            self._exclusive_agents = exclusive_domain_agents(self._registry)
            # 重派工具表注入 RouterLLM（持有 _tools 引用）
            self._router_llm.update_tools(derived_tools, agent_name_map)
            new_graph = self._build_graph()
            self._graph = new_graph  # 原子替换：编译通过才换引用
            nodes = len(new_graph.get_graph().nodes)
            logger.info(
                "[capability] graph rebuilt: %d agents, %d nodes",
                len(agent_name_map), nodes,
            )
            return True, f"{len(agent_name_map)} agents, {nodes} nodes"
        except Exception as e:
            logger.exception("[capability] graph rebuild failed: %s", e)
            return False, str(e)[:300]

    async def record_history(
        self,
        chat_id: str,
        sender_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """把一轮对话（用户消息 + 助手回复）写入多轮历史缓冲。

        由 main.py 在回复后调用，供后续消息承接上下文。
        """
        if self._history:
            await self._history.record(chat_id, sender_id, user_content, assistant_content)

    # ── 多轮对话聚合 ──────────────────────────────────────────────
    #
    # 说明：「跨轮文本+附件累积」内聚到提供 run_with_message 的 Handler
    # （见下），主链路只管把历史递下去；
    # SessionHistory（_history）仍是全局语义历史的唯一事实源，供路由/子 Agent 承接上下文。

    def _allow_tool_call(self, session_key: str) -> bool:
        """每会话 tool-call 滑动窗口限流。

        同一 session_key 在 tool_rate_window_sec 秒内累计 tool-call 数
        ≥ tool_rate_limit 时拒绝（返回 False）。超时后自动重置。
        """
        if not session_key:
            return True
        now = _now()
        window = self._tool_calls.get(session_key, [])
        # 移除窗口外的时间戳
        cutoff = now - self._tool_rate_window_sec
        active = [t for t in window if t > cutoff]
        # 会话结束（窗口内无调用）→ 删除键，防长期运行 dict 无限增长（P2-6）
        if not active:
            self._tool_calls.pop(session_key, None)
        elif len(self._tool_calls) > 1024:
            # 机会式全量清扫：只保留窗口内仍有调用的会话
            stale = [k for k, v in self._tool_calls.items() if not v or max(v) <= cutoff]
            for k in stale:
                self._tool_calls.pop(k, None)
        if len(active) >= self._tool_rate_limit:
            self._tool_calls[session_key] = active
            logger.warning(
                "Tool-call rate limited: %s (%d in %ds window)",
                session_key, len(active), self._tool_rate_window_sec,
            )
            return False
        active.append(now)
        self._tool_calls[session_key] = active
        return True

    # ── 图构建 ──

    def _build_graph(self) -> Any:
        """构建 LangGraph StateGraph。

        编排闭环：router → 执行节点 → gather → END。
        结果质量判定内联在执行方（模型自评 results_quality），编排层只做
        结构化门禁与观测，不再设独立的判定/纠错节点。
        """
        builder = StateGraph(AgentState)

        # 添加节点
        builder.add_node("router", self._router_node)
        builder.add_node("decompose", self._decompose_node)
        builder.add_node("gather", self._gather_node)

        for tool_name in self._agent_name_map:
            builder.add_node(tool_name, self._make_agent_node(tool_name))

        builder.add_node("help_node", self._help_node)
        builder.add_node("direct_answer_node", self._direct_answer_node)
        # 意图澄清节点（SPEC-20260912 R2）：低置信度/模型主动反问
        builder.add_node("clarify_node", self._clarify_node)

        # 入口
        builder.set_entry_point("router")

        # 条件边：Router → 按路由目标分派
        builder.add_conditional_edges(
            "router",
            self._route_condition,
            {
                **{t: t for t in self._agent_name_map},
                "route_to_help": "help_node",
                "direct_answer": "direct_answer_node",
                "route_to_clarify": "clarify_node",
                "__decompose__": "decompose",
                "__end__": END,
            },
        )

        # 复杂任务：分解 → 逐步执行 → Gather
        builder.add_edge("decompose", "gather")

        # 执行节点直通 gather：结构化门禁（error/⚠️/空响应）在 gather 内判定，
        # 不再经第二次 LLM 裁决。
        for tool_name in self._agent_name_map:
            builder.add_edge(tool_name, "gather")
        builder.add_edge("help_node", "gather")
        builder.add_edge("direct_answer_node", "gather")
        builder.add_edge("clarify_node", "gather")

        # Gather → END
        builder.add_edge("gather", END)

        return builder.compile()

    # ── 路由条件 ──

    def _route_condition(self, state: AgentState) -> str:
        """根据路由结果决定下一步走向。

        is_complex（router 已分解出 sub_tasks）必须先于 agent 表判断：
        复杂任务的目标也落在 agent 表内（route_to_dev_bot），若先查表
        会直接进 agent 节点，decompose 永远不可达（P1-1 死代码根因）。
        """
        route_to = state.get("route_to", "")

        if state.get("is_complex") and route_to in self._agent_name_map:
            return "__decompose__"
        if route_to in self._agent_name_map:
            # ReAct 多意图：主路由之外还有并行专业域任务 → 也走 decompose
            # 分组执行（复用其依赖分组 + 结果整合），decompose 节点会先跑主
            # 路由 agent 再并行跑 parallel_tasks（见 _decompose_node）。
            if state.get("parallel_tasks"):
                return "__decompose__"
            return route_to
        if route_to == "route_to_help":
            # help + 专业域混合意图：help 本地生成 + 并行 agent 在 decompose 整合
            if state.get("parallel_tasks"):
                return "__decompose__"
            return "route_to_help"
        if route_to == "direct_answer":
            return "direct_answer"
        if route_to == "route_to_clarify":
            return "route_to_clarify"
        return "__end__"

    # ── 节点实现 ──

    async def _router_node(self, state: AgentState) -> dict[str, Any]:
        """LLM 意图识别与路由决策（纯 AI，无正则）。

        R3 低置信度降级：模型自报 confidence < clarify_threshold 时，
        路由目标改写为 route_to_clarify（原目标保留在 spans 供审计）。
        """
        start = time.time()
        message = state["input_message"]
        history = state.get("history") or []
        on_routed = state.get("notify_cb")
        progress_cb = state.get("progress_cb")

        # 进度卡第一句跟这条消息走，不写死「在想派谁」。
        # 路由还没返回时，用用户原话的关键词；返回后换成模型自己的一句话复述。
        if progress_cb:
            try:
                await progress_cb({
                    "stage": "route_start",
                    "detail": _opening_thought(message),
                    "tool": "router",
                    "seq": 0,
                })
            except Exception:
                pass

        # 调用 LLM 路由（注入多轮历史 + 用户长期记忆实现语义承接与个性化；
        # 带媒体时 user 帧升级图文混合，路由模型直接看图判意图）
        result: RouterResult = await self._router_llm.route(
            message, history, memory_context=state.get("memory_context", ""),
            media_bundle=state.get("media_bundle"),
        )

        duration_ms = (time.time() - start) * 1000

        # R3：低置信度降级 → clarify（fallback/confidence=0 视为无把握）
        demoted = False
        tool_name = result.tool_name
        threshold = getattr(self._config, "clarify_threshold", 0.0)
        if (
            threshold > 0
            and tool_name in self._agent_name_map
            and result.confidence < threshold
        ):
            demoted = True
            tool_name = "route_to_clarify"
            candidates = [self._display_name(self._agent_name_map.get(result.tool_name, ""))]
            if not result.args.get("question"):
                result.args["question"] = (
                    f"你想做的是「{result.intent_summary or message[:40]}」吗？"
                    "可以补充一句具体需求，我好交给对的人处理。"
                )
            result.args.setdefault("candidates", [c for c in candidates if c])

        # 判断是否为复杂任务（需要多步编排）
        need_decompose = False
        decompose = None
        if result.tool_name == "route_to_dev_bot" and not demoted:
            decompose = await self._router_llm.decompose(message, history)
            if decompose.sub_tasks:
                need_decompose = True
                logger.info(
                    "Complex task detected: %d sub-tasks for '%s'",
                    len(decompose.sub_tasks), message[:50],
                )

        # ReAct 多意图：同一次响应里的其余专业域 tool_calls → 并行分派。
        # 主路由是专业 Agent 或 route_to_help 均可（「你可以做什么？日报还能用么？」
        # 这类 help+专业域混合意图：help 主路由 + 专业域 extra_calls）；
        # clarify/direct 语义下不并行。
        parallel_tasks: list[dict[str, Any]] = []
        if (
            not demoted
            and not need_decompose
            and (tool_name in self._agent_name_map or tool_name == "route_to_help")
        ):
            main_agent = self._agent_name_map.get(tool_name, "")
            # 专属领域主路由独占消息：其 extra_calls 是同一意图的冗余分派
            # （如「技术日报」同时派 skill-aihot），且 cc-connect 通道会话
            # 繁忙时会拖满 90s 超时导致整条消息空返回
            if main_agent and main_agent in self._exclusive_agents:
                if result.extra_calls:
                    logger.info(
                        "Parallel dispatch suppressed: main route %s is exclusive_domain",
                        main_agent,
                    )
            else:
                for extra in result.extra_calls:
                    e_agent = self._agent_name_map.get(extra["tool_name"], "")
                    if not e_agent:
                        continue
                    if e_agent == main_agent:
                        continue
                    parallel_tasks.append({
                        "agent": e_agent,
                        "query": str(extra["args"].get("query", "") or message),
                    })
            if parallel_tasks:
                logger.info(
                    "Parallel dispatch: %d extra agents %s",
                    len(parallel_tasks), [t["agent"] for t in parallel_tasks],
                )

        # 路由后通知（供上层发上下文化回执）；direct/help/clarify 秒回场景不通知
        if on_routed and tool_name not in ("direct_answer", "route_to_help", "route_to_clarify"):
            agent_name = self._agent_name_map.get(tool_name, "")
            display = self._display_name(agent_name) or agent_name
            try:
                await on_routed(display)
            except Exception as e:
                logger.debug("on_routed callback failed: %s", e)

        # 进度卡：意图判定完成 + 分发去向（叙事化时间线，主流思考过程形态）。
        # simple=True 标记秒回链路（direct/help/clarify/降级），main 侧若卡片
        # 尚未真正发出则直接取消——闲聊不该有郑重其事的处理过程卡。
        if progress_cb:
            try:
                if demoted:
                    await progress_cb({
                        "stage": "route_done", "simple": True,
                        "detail": "想再和你确认一下具体需求",
                        "tool": "router", "seq": 0,
                    })
                elif tool_name == "direct_answer":
                    await progress_cb({
                        "stage": "route_done", "simple": True,
                        "detail": "这个问题我直接回答",
                        "tool": "router", "seq": 0,
                    })
                elif tool_name == "route_to_help":
                    await progress_cb({
                        "stage": "route_done", "simple": True,
                        "detail": "整理一下我能帮你做什么",
                        "tool": "router", "seq": 0,
                    })
                else:
                    agent_name = self._agent_name_map.get(tool_name, "")
                    display = self._display_name(agent_name) or agent_name
                    thought = (result.intent_summary or "").strip()
                    if thought:
                        await progress_cb({
                            "stage": "route_done",
                            "replace_opening": True,
                            "detail": thought,
                            "tool": "router",
                            "seq": 0,
                        })
                    await progress_cb({
                        "stage": "dispatch", "simple": False,
                        "detail": f"交给 **{display}**",
                        "tool": agent_name, "seq": 0,
                    })
                    for pt in parallel_tasks:
                        p_display = self._display_name(pt["agent"]) or pt["agent"]
                        await progress_cb({
                            "stage": "dispatch", "simple": False,
                            "detail": f"同时交给 **{p_display}**",
                            "tool": pt["agent"], "seq": 0,
                        })
            except Exception:
                logger.debug("progress_cb (router) failed", exc_info=True)

        return {
            "route_to": tool_name,
            "route_args": result.args,
            "confidence": result.confidence,
            "intent_summary": result.intent_summary,
            "demoted": demoted,
            "clarify_question": str(result.args.get("question", "")) if tool_name == "route_to_clarify" else "",
            "is_complex": need_decompose,
            "sub_tasks": decompose.sub_tasks if (decompose and need_decompose) else [],
            # ReAct 多意图并行分派（router 一次响应里的其余专业域 calls）
            "parallel_tasks": parallel_tasks,
            # 记录本轮路由轨迹（agent 名），供 TaskRecord.route_chain 落档
            # 与通道故障时的显示名回溯
            "route_trace": [
                self._agent_name_map.get(result.tool_name, result.tool_name)
            ] + [t["agent"] for t in parallel_tasks],
            "duration_ms": duration_ms,
            # 任务档案 span
            "spans": [{
                "span": "router",
                "route_to": self._agent_name_map.get(result.tool_name, result.tool_name),
                "tool": tool_name,
                "is_fallback": result.is_fallback,
                "demoted": demoted,
                "confidence": result.confidence,
                "intent_summary": result.intent_summary,
                "original_target": (
                    self._agent_name_map.get(result.tool_name, "") if demoted else ""
                ),
                "duration_ms": round(duration_ms, 1),
                "tokens": result.tokens,
                "cost_usd": round(result.cost_usd, 6),
                "model": self._config.model,
                "prompt_version": result.prompt_version,
            }],
        }

    async def _clarify_node(self, state: AgentState) -> dict[str, Any]:
        """意图澄清节点 — 反问用户确认（反问本身即是合法终态）。

        优先用路由模型生成的 question；缺省时拼装兜底文案。
        反问内容写入会话 pending 状态（5 分钟 TTL）：下一轮消息路由前取出
        注入上下文，用户回「对，就是那个」时路由 LLM 能结合候选定向（澄清闭环）。
        """
        question = str(state.get("clarify_question", "") or "")
        summary = state.get("intent_summary", "") or state["input_message"][:40]
        if not question:
            question = (
                f"你想做的是「{summary}」吗？可以补充一句具体需求，"
                "我好交给对的人处理。"
            )
        candidates = list(state.get("route_args", {}).get("candidates") or [])
        chat_id = state.get("chat_id", "")
        sender_id = state.get("sender_id", "")
        if self._history and chat_id:
            try:
                await self._history.record_pending(
                    chat_id, sender_id,
                    intent_summary=summary,
                    candidates=candidates,
                    question=question,
                )
            except Exception as e:
                logger.debug("[pending] record failed: %s", e)
        logger.info(
            "[clarify] demoted=%s question=%r",
            state.get("demoted", False), question[:60],
        )
        return {
            "response": f"🤔 想跟你确认一下：{question}",
            "error": None,
            "spans": [{
                "span": "clarify",
                "demoted": bool(state.get("demoted", False)),
                "confidence": state.get("confidence", 0.0),
                "pending_recorded": bool(chat_id),
            }],
        }

    async def _decompose_node(self, state: AgentState) -> dict[str, Any]:
        """复杂任务分解执行节点（含 ReAct 多意图并行分派）。

        两种进入方式：
        - is_complex（router 分解出 sub_tasks）：按依赖分组逐步执行
        - parallel_tasks 非空（router 一次响应多个专业域 tool_calls）：
          主路由 agent 与并行任务同批并发执行，结果按执行者整合
        """
        sub_tasks = state.get("sub_tasks", [])
        parallel = state.get("parallel_tasks") or []
        sub_results: list[str] = []
        original_message = state["input_message"]
        history = state.get("history") or []

        # ReAct 多意图：主路由 + 并行任务统一为执行步，同批并发。
        # 主路由是 route_to_help 时，能力清单由 help 节点本地生成（help 不是
        # Agent，不走 _dispatch_step），并行 agent 回复拼在其后。
        if parallel and not sub_tasks:
            main_agent = state.get("route_to", "")
            parts: list[str] = []

            async def _run_one(agent: str, query: str) -> tuple[str, str]:
                resp, _err = await self._dispatch_step(agent, query, history, state)
                return agent, resp or ""

            if main_agent == "route_to_help":
                help_state = await self._help_node(state)
                help_resp = help_state.get("response", "")
                if help_resp:
                    parts.append(help_resp)
                    sub_results.append(help_resp)
                par_results = await asyncio.gather(
                    *[_run_one(t["agent"], t["query"]) for t in parallel]
                )
                parts.extend(
                    f"**{self._display_name(a) or a}**：\n{r}" for a, r in par_results if r
                )
                sub_results.extend(r for _a, r in par_results)
            else:
                main_query = str(
                    (state.get("route_args") or {}).get("query", "") or original_message
                )
                steps: list[dict[str, Any]] = []
                if main_agent:
                    # route_to 存的是工具名（route_to_daily_agent）——必须先映射回
                    # agent 名再分派，否则 _dispatch_step 查不到 handler/transport，
                    # 进程内 Handler 会被误派 cc-connect 通道空转超时
                    steps.append({
                        "agent": self._agent_name_map.get(main_agent, main_agent),
                        "query": main_query,
                    })
                steps.extend(parallel)

                async def _run_par(i: int) -> tuple[int, str, str]:
                    st = steps[i]
                    resp, _err = await self._dispatch_step(
                        st["agent"], st["query"], history, state
                    )
                    return i, st["agent"], resp or ""

                results = await asyncio.gather(*[_run_par(i) for i in range(len(steps))])
                parts = [
                    f"**{self._display_name(agent) or agent}**：\n{resp}"
                    for _i, agent, resp in results if resp
                ]
                sub_results.extend(r for _i, _a, r in results)

            response = "\n\n".join(parts) if parts else "⚠️ 各分派任务均未返回结果"
            return {
                "response": response,
                "sub_results": sub_results,
                "route_to": state.get("route_to", ""),
            }

        # 无有效子任务 → 整条消息走分派表（dev-bot 通道），不绕过分派逻辑
        if not sub_tasks:
            response, err = await self._run_cc_connect_agent(
                "dev-bot", original_message, state
            )
            if err:
                return {"response": "", "sub_results": [], "error": err,
                        "no_recover": True}
            if response == LATE_DELIVERY:
                return {"response": (
                    "⏳ **开发助手** 任务较重，已转后台继续处理，"
                    "完成后结果会自动发到这里（无需重复发送）。"
                ), "sub_results": []}
            return {"response": response, "sub_results": [response]}

        # 按依赖关系分组执行
        executed: dict[int, str] = {}
        remaining = list(range(len(sub_tasks)))

        while remaining:
            ready = []
            for i in remaining:
                dep = sub_tasks[i].get("depends_on", -1)
                if dep == -1 or dep in executed:
                    ready.append(i)

            if not ready:
                logger.error("Task decomposition deadlock: remaining=%s", remaining)
                break

            async def _run_step(idx: int) -> tuple[int, str]:
                task = sub_tasks[idx]
                agent = task.get("agent", "dev-bot")
                query = task.get("query", original_message)

                dep = task.get("depends_on", -1)
                if dep != -1 and dep in executed:
                    query = f"{query}\n\n上下文（上一步结果）:\n{executed[dep]}"

                # 与主路由共用分派逻辑：handler 运行时注册优先，否则按 transport 查表
                resp, _err = await self._dispatch_step(agent, query, history, state)
                return idx, resp or ""

            step_results = await asyncio.gather(*[_run_step(i) for i in ready])

            for idx, resp in step_results:
                executed[idx] = resp
                remaining.remove(idx)

        sub_results = [executed.get(i, "") for i in range(len(sub_tasks))]

        response_parts = []
        for i, task in enumerate(sub_tasks):
            agent = task.get("agent", "?")
            result_text = sub_results[i] if i < len(sub_results) else ""
            if result_text:
                response_parts.append(f"**{agent}** 回复：\n{result_text[:500]}")

        if not response_parts:
            response_parts.append("⚠️ 子任务均未返回结果")

        return {
            "response": "\n\n".join(response_parts),
            "sub_results": sub_results,
        }

    async def _dispatch_step(
        self, agent_name: str, query: str, history: list[dict], state: AgentState
    ) -> tuple[str, str | None]:
        """decompose 子步的统一分派入口 — 与 agent 节点共用 transport 查表。"""
        if self._handler_registry.has_handler(agent_name):
            handler = self._handler_registry.get(agent_name)
            # 调用方上下文透传：并行分派的 handler 同样跑在消息链路里
            # （发图、建任务这类后续动作都依赖 chat_id）
            from app.orchestrator.context import set_caller_context

            set_caller_context({
                "sender_id": state.get("sender_id", ""),
                "sender_name": state.get("sender_name", ""),
                "chat_id": state.get("chat_id", ""),
                "message_obj": state.get("message_obj"),
            })
            async def _call() -> tuple[str, str | None]:
                try:
                    resp = await handler.run(query, history)  # type: ignore[union-attr]
                    return resp or "", None
                except Exception as e:
                    logger.warning("Decompose handler '%s' failed: %s", agent_name, e)
                    return "", str(e)[:100]

            # 子步同样受两级时限约束：一个挂住的 handler 会拖死整条分解链，
            # 并占满并发闸额度（同 _execute_agent 的中断根因）
            resp, err = await self._inproc_with_deadline(agent_name, _call(), state)
            if resp == LATE_DELIVERY:
                disp = self._display_name(agent_name) or agent_name
                resp = f"⏳ **{disp}** 任务较重，已转后台继续处理，完成后结果会自动发到这里。"
            return resp, err
        agent = self._registry.get(agent_name) if self._registry else None
        key = agent.transport if agent else "cc_connect"
        dispatcher = self._dispatchers.get(key, self._run_cc_connect_agent)
        if key == "handler":
            # decompose 子步同样受两级时限约束，否则一个挂住的子步会拖死整条分解链
            resp, err = await self._inproc_with_deadline(
                agent_name, dispatcher(agent_name, query, state), state)
        else:
            resp, err = await dispatcher(agent_name, query, state)
        # decompose/并行路径的 LATE_DELIVERY 哨兵就地转回执文案
        # （后台守望与 on_reply 直投不受影响）
        if resp == LATE_DELIVERY:
            disp = self._display_name(agent_name) or agent_name
            resp = f"⏳ **{disp}** 任务较重，已转后台继续处理，完成后结果会自动发到这里。"
        return resp, err

    # ── 执行通道实现（transport dispatchers）────────────────────────────────

    async def _run_handler_agent(
        self, role_name: str, query: str, state: AgentState
    ) -> tuple[str, str | None]:
        """transport=handler：进程内 Python Handler 执行。"""
        handler = self._handler_registry.get(role_name)
        # 调用方上下文透传（contextvars）：Handler 经 get_caller_context()
        # 拿 sender/chat/message_obj（schedule-agent 创建入口依赖 message_obj）
        from app.orchestrator.context import set_caller_context

        set_caller_context({
            "sender_id": state.get("sender_id", ""),
            "sender_name": state.get("sender_name", ""),
            "chat_id": state.get("chat_id", ""),
            "message_obj": state.get("message_obj"),
        })
        try:
            response = await handler.run(query, state.get("history") or [])  # type: ignore[union-attr]
            if response:
                return response, None
            return "", f"{role_name} 未返回结果"
        except Exception as e:
            logger.warning("Handler '%s' failed: %s", role_name, e)
            return "", str(e)[:100]

    async def _run_aggregated_handler(
        self, role_name: str, query: str, state: AgentState
    ) -> tuple[str, str | None]:
        """需要多轮聚合 + 附件透传的 Handler 走本分派（提供 run_with_message 的）。

        聚合逻辑内聚在 Handler 的 run_with_message 里，这里只负责把本轮消息
        原文、附件与历史递进去，并把进度回调原样透传/还原。

        输入取用户本轮消息**原文**而非路由改写 query：路由 LLM 会把同会话
        历史与长期记忆（跨会话旧任务）织进 query 参数，把上一件事的描述混进
        这一件（实测：旧任务混入后执行面被带偏）。Handler 自带会话聚合缓冲
        （TTL 10 分钟），多轮上下文不依赖路由改写。
        """
        handler = self._handler_registry.get(role_name)
        run_with = getattr(handler, "run_with_message", None)

        # 进度回调透传：handler 的工具循环逐事件上报（豆包式进度卡数据源）
        progress_cb = state.get("progress_cb")
        set_progress = getattr(handler, "set_progress_callback", None)
        prev_progress_cb = None
        if callable(set_progress):
            prev_progress_cb = getattr(handler, "_progress_cb", None)
            set_progress(progress_cb)

        # 引擎壳 Handler 提供聚合入口 → 走聚合路径
        if callable(run_with):
            raw_message = state.get("input_message") or query
            attachments = []
            try:
                if _attachment_downloader:
                    message_obj = state.get("message_obj") or None
                    if message_obj is not None:
                        attachments = await _attachment_downloader(message_obj)
                        # 当前消息没有文件时，回看同一发送者最近 2 分钟的群消息。
                        # 飞书回复不携带更早几条上的附件。
                        if not attachments:
                            ch = _recent_file_channel
                            if ch is not None:
                                attachments = await ch.download_recent_sender_files(
                                    message_obj, "logs/attachments", within_sec=1800,
                                )
            except Exception as e:
                logger.warning("Attachment download failed: %s", e)
            try:
                response = await run_with(
                    raw_message, state["session_key"],
                    attachments=attachments,
                    history=state.get("history") or [],
                )
                if response:
                    return response, None
                return "", f"{role_name} 未返回结果"
            except Exception as e:
                logger.warning("Handler '%s' failed: %s", role_name, e)
                return "", str(e)[:100]
            finally:
                if callable(set_progress):
                    set_progress(prev_progress_cb)  # 还原，防跨消息串回调

        # 没有聚合入口的 Handler 走标准路径
        try:
            response = await handler.run(query, state.get("history") or [])  # type: ignore[union-attr]
            if response:
                return response, None
            return "", f"{role_name} 未返回结果"
        except Exception as e:
            logger.warning("Handler '%s' failed: %s", role_name, e)
            return "", str(e)[:100]
        finally:
            if callable(set_progress):
                set_progress(prev_progress_cb)

    async def _run_cc_connect_agent(
        self, role_name: str, query: str, state: AgentState
    ) -> tuple[str, str | None]:
        """transport=cc_connect：Claude Code 执行通道（webhook + session 轮询）。

        cc-connect 是与 handler 平级的普通执行通道：客户端缺席或调用失败时
        置 no_recover 标记，gather 直接给「暂不可用」诚实文案——通道整体不可
        用时再换一个同通道 agent 只会空转一轮。
        """
        if not self._cc_client:
            return "", "Claude Code 执行通道未启用"

        agent = self._registry.get(role_name) if self._registry else None

        # 前缀语义：skill 合成 Agent 触发 Claude Code 原生技能加载；
        # 普通 Agent 用角色前缀切换行为；dev-bot 本体无需前缀。
        project = (agent.cc_project if agent else "") or "dev-bot"
        if agent and agent.skill_name:
            prefix = f"[use skill: {agent.skill_name}] "
        elif role_name != project:
            prefix = f"[role={role_name}] "
        else:
            prefix = ""

        # 迟到投递回调：cc-connect 同步轮询超时后，后台守望按 [task:tag] 精准
        # 取回本任务结果并经此直发原会话（新闻SQL 事故修复：17 分钟产出不再
        # 被 90s 超时静默吞掉）。chat_id 闭包捕获，结果不会错配到别的会话。
        chat_id = state.get("chat_id", "")

        async def _deliver_late(text: str) -> None:
            if _late_delivery_sender is None or not chat_id:
                logger.warning("[cc-connect] late result dropped (no sender/chat)")
                return
            try:
                await _late_delivery_sender(
                    chat_id, f"✅ **{self._display_name(role_name) or role_name} 后台任务完成**\n\n{text}"
                )
            except Exception as e:
                logger.warning("[cc-connect] late delivery failed: %s", e)

        try:
            response = await self._cc_client.send_and_wait(
                prompt=prefix + query,
                project=project,
                session_key=state["session_key"],
                on_reply=_deliver_late,
            )
            if response == LATE_DELIVERY:
                # 同步窗口内没等到，但后台守望已按 tag 锁定本任务结果，
                # 完成后直发原会话 → 回执即终态，不重复分派
                logger.info("[cc-connect] %s 转后台守望（结果完成后自动送达）", role_name)
                return LATE_DELIVERY, None
            if response:
                return response, None
            logger.warning("[cc-connect] %s no response (timeout/unreachable)", role_name)
            return "", f"{role_name} 未响应（服务超时或不可达）"
        except Exception as e:
            logger.warning("Agent node '%s' failed: %s", role_name, e)
            return "", str(e)[:100]

    async def _inproc_with_deadline(
        self, role_name: str, awaitable: Awaitable[tuple[str, str | None]],
        state: AgentState,
    ) -> tuple[str, str | None]:
        """进程内通道两级时限：>90s 转后台，>agent.yaml 的 timeout_sec 才真放弃。

        为什么要两级：实测最长任务可到 1100s，而分派处此前
        **没有任何超时**（agent.yaml 的 timeout_sec 只解析不消费），长任务一路占住
        并发闸额度 → 后面的消息排队到 queue_timeout 回「系统繁忙」，用户体感就是
        「任务经常中断」。这里对齐 cc-connect 已有的后台守望语义：到点先让出额度
        并给诚实回执，任务本体继续跑（shield 不被 cancel），真结果落好直发原会话。
        """
        task = asyncio.ensure_future(awaitable)
        display = self._display_name(role_name) or role_name
        hard = self._agent_hard_timeout(role_name)
        try:
            return await asyncio.wait_for(asyncio.shield(task),
                                          timeout=_INPROC_SYNC_WAIT_SEC)
        except TimeoutError:  # asyncio.TimeoutError 自 3.11 就是它的别名
            pass
        except Exception as e:
            logger.warning("Inproc dispatch '%s' failed: %s", role_name, e)
            return "", str(e)[:100]

        chat_id = state.get("chat_id", "")

        async def _notify(text: str) -> None:
            if _late_delivery_sender is None or not chat_id:
                logger.warning("[timeout] %s 后台结果无处投递（chat=%s, sender=%s）",
                               role_name, chat_id, _late_delivery_sender is not None)
                return
            try:
                await _late_delivery_sender(chat_id, text)
            except Exception:
                logger.warning("[timeout] 后台结果投递失败", exc_info=True)

        async def _finish() -> None:
            remaining = max(1.0, hard - _INPROC_SYNC_WAIT_SEC)
            try:
                resp, err = await asyncio.wait_for(task, timeout=remaining)
            except TimeoutError:
                task.cancel()
                logger.warning("[timeout] %s 超过硬上限 %ds，已放弃", role_name, hard)
                await _notify(f"⚠️ **{display}** 超过 {hard}s 仍未完成，已停止等待。"
                              "可以把需求拆小一点再发，或稍后重试。")
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[timeout] %s 后台执行失败: %s", role_name, e)
                await _notify(f"⚠️ **{display}** 后台处理失败：{str(e)[:100]}")
                return
            if err:
                await _notify(f"⚠️ **{display}** 后台结果：{err}")
            elif resp:
                await _notify(f"✅ **{display} 后台任务完成**\n\n{resp}")

        logger.info("[timeout] %s 超过 %.0fs 未完成 → 转后台（硬上限 %ds）",
                    role_name, _INPROC_SYNC_WAIT_SEC, hard)
        bg = asyncio.ensure_future(_finish())
        _detached_tasks.add(bg)
        bg.add_done_callback(_detached_tasks.discard)
        return LATE_DELIVERY, None

    def _make_agent_node(self, tool_name: str):
        """工厂方法：创建 Agent Node。

        执行通道由 agent.yaml 的 transport 声明决定（查表分派）；
        运行时注册的 Handler 优先于 yaml 声明。
        """
        role_name = self._agent_name_map.get(tool_name, "dev-bot")

        async def _execute_agent(state: AgentState) -> dict[str, Any]:
            query = state["route_args"].get("query", state["input_message"])

            # ── 前置：每会话 tool-call 限流（防滥用） ──
            if not self._allow_tool_call(state.get("session_key", "")):
                return {"response": "⚠️ 触发频率过高，请稍后再试", "error": "rate_limited"}

            # 进度卡：agent 开始执行（与 router 节点的 dispatch 事件呼应）
            progress_cb = state.get("progress_cb")
            if progress_cb:
                try:
                    display = self._display_name(role_name) or role_name
                    await progress_cb({"stage": "agent_start",
                                       "detail": f"**{display}** 正在处理", "tool": role_name, "seq": 0})
                except Exception:
                    pass

            # handler 运行时注册优先于 transport 声明
            used_cc_connect = False
            if self._handler_registry.has_handler(role_name):
                # 聚合/附件语义挂在 Handler 的 run_with_message：
                # 提供该入口的 Handler 走聚合分派（多轮连发 + 附件转文本），
                # 其余 Handler 仍走标准 run()
                if callable(getattr(self._handler_registry.get(role_name), "run_with_message", None)):
                    resp, err = await self._inproc_with_deadline(
                        role_name, self._run_aggregated_handler(role_name, query, state), state)
                else:
                    resp, err = await self._inproc_with_deadline(
                        role_name, self._run_handler_agent(role_name, query, state), state)
            else:
                agent = self._registry.get(role_name) if self._registry else None
                transport = agent.transport if agent else "cc_connect"
                dispatcher = self._dispatchers.get(transport, self._run_cc_connect_agent)
                used_cc_connect = dispatcher is self._run_cc_connect_agent
                resp, err = await dispatcher(role_name, query, state)

            result: dict[str, Any] = {"response": resp, "error": err}
            # cc-connect 超时转后台守望 → 回执文案即终态：结果稍后经
            # _deliver_late 直投，回执已发就不该再分派一次（双跑烧会话额度）
            if resp == LATE_DELIVERY:
                result["response"] = (
                    f"⏳ **{self._display_name(role_name) or role_name}** 任务较重，"
                    "已转后台继续处理，完成后结果会自动发到这里，不用重发。\n"
                    "期间可以继续问我别的。"
                )
            # cc-connect 通道故障 → 置 no_recover：通道整体不可用时再换一个同
            # 通道 agent 只会空转一轮，gather 直接给「暂不可用」诚实文案
            if err and used_cc_connect:
                result["no_recover"] = True
            return result

        async def agent_node(state: AgentState) -> dict[str, Any]:
            """执行包装：计时 + 任务档案 span（Observability 支柱）。

            R5：span 记录分派给 agent 的最终 query（含改写），支撑
            「用户问了什么 → 我们怎么做的」链路回放。
            """
            start = time.time()
            dispatched_query = state["route_args"].get("query", state["input_message"])
            result = await _execute_agent(state)
            error = result.get("error") or ""

            # 进度卡：agent 执行完成/失败——链路「处理结果」一跳
            progress_cb = state.get("progress_cb")
            if progress_cb:
                try:
                    display = self._display_name(role_name) or role_name
                    if error:
                        await progress_cb({"stage": "agent_done",
                                           "detail": f"**{display}** 遇到问题：{error[:60]}",
                                           "tool": role_name, "seq": 0})
                    else:
                        ms = time.time() - start
                        await progress_cb({"stage": "agent_done",
                                           "detail": f"**{display}** 处理完成（用时 {ms:.0f} 秒）",
                                           "tool": role_name, "seq": 0})
                except Exception:
                    pass

            result["spans"] = [{
                "span": "agent",
                "agent": role_name,
                "status": "error" if error else "success",
                "error": error[:100],
                "query_preview": str(dispatched_query)[:120],
                "duration_ms": round((time.time() - start) * 1000, 1),
            }]
            return result

        return agent_node

    @staticmethod
    def _short_name(display_name: str) -> str:
        """展示名 → 清单短名：去「Agent/技能」后缀（如「数据库查询 Agent」→「数据库查询」）。"""
        for suffix in (" Agent", " 技能"):
            if display_name.endswith(suffix):
                return display_name[: -len(suffix)]
        return display_name

    @staticmethod
    def _version_label(version: str) -> str:
        """版本号 → 清单后缀（' 1.2.0'→' v1.2.0'；空串退空）。"""
        v = (version or "").strip().lower().lstrip("v")
        return f" v{v}" if v else ""

    @classmethod
    def _mentioned_agents(cls, query: str, agents) -> list:
        """query 点名检测：消息文本包含能力名/技能名/展示短名即视为提到。

        大小写不敏感；「news-sql-generator 的版本是？」能命中
        skill-news-sql-generator。供 help 详情块与 direct_answer
        运行时上下文共用。
        """
        q = (query or "").lower()
        if not q:
            return []
        hits = []
        for a in agents:
            needles = {a.name.lower(), (a.skill_name or "").lower(),
                       cls._short_name(a.display_name).lower()}
            needles.discard("")
            if any(n in q for n in needles):
                hits.append(a)
        return hits

    @staticmethod
    def _short_capability(display_name: str, description: str) -> str:
        """agent 描述 → 用户向一句话能力（≤24 字）。

        description 是路由级单源（RouterLLM 也读它），直接展示会把
        「用户想…时使用」「触发词：…」这类给 LLM 的路由信息暴露给用户。
        清洗顺序：截路由尾缀 → 去与 display_name 重复的领域前缀 →
        截通道定位语（如 dev-bot 的「Claude Code 通用业务 Agent：」）→
        取顿号/逗号前首段 → 硬截 24 字。
        """
        for sep in ("。用户想", "。用户", "。当", "。触发词", "。综合", "。", "——"):
            idx = description.find(sep)
            if idx > 0:
                description = description[:idx]
        head, sep, rest = description.partition("：")
        if sep and len(head) <= 14 and (
            head in display_name or head[:2] in display_name
        ):
            description = rest
        if len(description) > 24:
            # 通道定位语/领域标签（冒号前 ≤24 字，如「Claude Code 通用业务
            # Agent」「定时任务」）不是用户句式，截掉取冒号后
            head2, sep2, rest2 = description.partition("：")
            if sep2 and rest2 and len(head2) <= 24:
                description = rest2
        if len(description) > 24:
            # 按 24 字预算累积顿号列举段（信息量优于硬截）；有括号补充说明
            # 时（如「创建定时/周期任务（评估通过即生效…）」）先整体剔除括号段
            if "（" in description and description.index("（") <= 24:
                description = description[: description.index("（")]
            if len(description) > 24:
                acc = ""
                for part in description.replace("，", "、").split("、"):
                    if acc and len(acc) + 1 + len(part) > 24:
                        break
                    if len(part) > 24:
                        break
                    acc = f"{acc}、{part}" if acc else part
                description = acc or description
        if len(description) > 24:
            return description[:24].rstrip("、，, (") + "…"
        return description

    async def _help_node(self, state: AgentState) -> dict[str, Any]:
        """帮助节点 — 动态生成自我介绍 + 能力清单。

        分组展示：智能助手（L0 开放）→ 专业工具（L1 授权）→ 扩展技能（skill 合成）
        → 热插拔技能（capability 链路安装，📦 标识），全部从 registry 单源派生
        （agent.yaml / SKILL.md 的 description + version），新增 Agent/Skill 重启后
        自动出现在对应分组。routable=False 的 Agent（如 dev-bot 远程开发通道）
        不对团队用户开放，不进清单；运行时开关（agent_switches）关闭的能力
        同样不可见。

        复合问题感知：query 里点名了某个能力（如「…news-sql-generator 的版本
        是？」）时，清单后附加该能力的详情（版本/描述/最近安装时间）——
        否则多意图消息只答能力清单、版本问题被静默吞掉。

        问候语按北京时间（UTC+8）计算，不依赖宿主机时区。
        """
        query = state.get("input_message") or ""
        h = (time.gmtime().tm_hour + 8) % 24  # 北京时间
        if h < 12:
            greeting = "早上好"
        elif h < 18:
            greeting = "下午好"
        else:
            greeting = "晚上好"

        agents = [a for a in (self._registry.routable() if self._registry else [])]

        # 热插拔名单（bot/inbox 档案，type=skill 的注册名为 skill-{name}）；
        # 读取失败退空集——分组是展示增强，绝不阻断 /help
        hotplug_names: set[str] = set()
        hotplug_installed: dict[str, str] = {}
        try:
            from app.ops.capability.store import CapabilityStore

            for rec in CapabilityStore().active_capabilities():
                key = f"skill-{rec.name}" if rec.type == "skill" else rec.name
                hotplug_names.add(key)
                if rec.created_at:
                    hotplug_installed[key] = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(rec.created_at)
                    )
        except Exception as e:
            logger.debug("[help] hotplug listing skipped: %s", e)

        core, pro, skills, hotplug = [], [], [], []
        for agent in agents:
            if agent.name in hotplug_names:
                hotplug.append(agent)
            elif agent.skill_name:
                skills.append(agent)
            elif agent.permission_level >= 1:
                pro.append(agent)
            else:
                core.append(agent)

        lines = [
            f"{greeting}！我是**团队数字助手**。",
            "",
            "**我能帮你**",
        ]
        for agent in core:
            lines.append(
                f"• {self._short_name(agent.display_name)}"
                f"{self._version_label(agent.version)}"
                f"：{self._short_capability(agent.display_name, agent.description)}"
            )
        for agent in pro:
            lines.append(
                f"• {self._short_name(agent.display_name)}"
                f"{self._version_label(agent.version)}"
                f"：{self._short_capability(agent.display_name, agent.description)}"
            )
        if skills or hotplug:
            lines.append("")
            lines.append("**扩展技能和🧩热插拔技能：**")
            for agent in skills:
                lines.append(
                    f"• {self._short_name(agent.display_name)}"
                    f"{self._version_label(agent.version)}"
                    f"：{self._short_capability(agent.display_name, agent.description)}"
                )
            for agent in hotplug:
                lines.append(
                    f"• 📦 {self._short_name(agent.display_name)}"
                    f"{self._version_label(agent.version)}"
                    f"：{self._short_capability(agent.display_name, agent.description)}"
                )

        # 复合问题：query 点名的能力附详情块（版本/描述/最近安装时间）
        mentioned = self._mentioned_agents(query, agents)
        if mentioned:
            lines.append("")
            lines.append("**🔍 你问到的能力**")
            for agent in mentioned:
                ver = (agent.version or "").strip().lower().lstrip("v")
                ver_text = f"v{ver}" if ver else "未声明（包内未写 version 字段）"
                lines.append(f"• {self._short_name(agent.display_name)}：{ver_text}")
                lines.append(f"  {agent.description}")
                installed = hotplug_installed.get(agent.name)
                if installed:
                    lines.append(f"  最近安装：{installed}")

        lines.append("")
        lines.append("直接说需求就行，不用记命令～")

        # 能力盘点出口收敛：清单尾追加团队技能库引导，给
        # 「我们团队有哪些AI能力」这类问题一个明确的二跳入口——此前该类
        # 问题在 help/direct/skill-catalog 三出口漂移（skill-catalog 全量
        # 扫描慢查询 96s）。数量来自本地快照，读取失败静默省略该行。
        skill_total = self._skill_catalog_count()
        if skill_total > 0:
            lines.append(
                f"📦 团队技能库另有 {skill_total} 项 Skill 资产，"
                "回复「团队技能库」查看全景～"
            )

        return {"response": "\n".join(lines), "error": None}

    @staticmethod
    def _skill_catalog_count() -> int:
        """团队技能库快照的条目数（失败返回 0 — 引导行不出现）。"""
        try:
            from app.domains.skill_catalog.store import CatalogStore

            snap_path = (
                Path(__file__).resolve().parent.parent.parent
                / "logs" / "skill_catalog" / "snapshot.json"
            )
            snap = CatalogStore(snapshot_path=str(snap_path), scanner=None).load()
            if snap is None:
                return 0
            return len(snap.entries)
        except Exception as e:
            logger.debug("[help] skill catalog count skipped: %s", e)
            return 0

    def _capability_context(self, query: str) -> str:
        """能力清单运行时上下文（含版本）— direct_answer 的事实来源。

        此前「xx 的版本是？」路由到 direct_answer 后，LLM 手里只有
        history + query，没有任何运行时能力数据，只能答「没有版本信息」。
        这里只在 query 提到能力关键词或显式点名能力时构建（问候/闲聊
        零开销），供 DirectAnswerLLM 拼到 user 帧尾部。
        """
        agents = [a for a in (self._registry.routable() if self._registry else [])]
        if not agents:
            return ""
        q = (query or "").lower()
        keywords = ("版本", "version", "能力", "技能", "能做什么", "会什么", "清单")
        if any(k in q for k in keywords):
            selected = agents
        else:
            selected = self._mentioned_agents(q, agents)
        if not selected:
            return ""
        rows = []
        for a in selected:
            ver = self._version_label(a.version).strip()
            rows.append(
                f"- {self._short_name(a.display_name)}"
                + (f"（{ver}）" if ver else "（版本未声明）")
            )
        return (
            "[运行时能力清单 — 回答能力/版本类问题的事实来源，"
            "版本号来自包内元数据，不要编造]\n" + "\n".join(rows)
        )

    async def _direct_answer_node(self, state: AgentState) -> dict[str, Any]:
        """直接回答节点 — 编排层自答（无合适专业 Agent 时的通用问答兜底）。

        两档策略：
        - 短问候/闲聊（≤12字）且有路由模型 reply → 直接用 reply（秒回，零额外调用）
        - 一般问题 → DirectAnswerLLM 专门生成（注入多轮历史 + 自答 prompt）
          失败时回退到路由模型 reply
        """
        query = state["input_message"]
        router_reply = state["route_args"].get("reply", "")
        history = state.get("history") or []

        if self._direct_llm.should_fast_reply(query, router_reply):
            return {
                "response": router_reply,
                "error": None,
                "spans": [{"span": "direct", "mode": "fast_reply"}],
            }

        # 降级消息（路由 LLM 403/超时等）直接静态回复，不再调自答 LLM：
        # 同一故障源二次调用只会再等一个完整超时（00:41 案例 15s+8s）
        router_span = next(
            (sp for sp in (state.get("spans") or []) if sp.get("span") == "router"),
            {},
        )
        if router_span.get("is_fallback"):
            return {
                "response": (
                    "我在的～可以直接说你的需求；说「你能做什么」查看能力清单"
                ),
                "error": None,
                "spans": [{"span": "direct", "mode": "router_degraded"}],
            }

        runtime_ctx = self._capability_context(query)

        result = await self._direct_llm.generate(
            query, history, runtime_context=runtime_ctx,
            media_bundle=state.get("media_bundle"),
        )
        reply = result["reply"] or router_reply or (
            "我在的～可以直接说你的需求；输入 /help 查看我能做什么"
        )
        return {
            "response": reply,
            "error": None,
            "spans": [{
                "span": "direct",
                "mode": "llm" if result["reply"] else "fallback",
                "duration_ms": round(result.get("latency_ms", 0), 1),
                "tokens": result.get("tokens", 0),
                "cost_usd": round(result.get("cost_usd", 0), 6),
                "model": self._config.model,
                "prompt_version": result.get("prompt_version", ""),
            }],
        }

    async def _gather_node(self, state: AgentState) -> dict[str, Any]:
        """结果聚合节点 — 统一格式化 + 结构化门禁（不调 LLM）。

        门禁只看硬信号：通道故障 / 执行错误 / ⚠️ 前缀 / 空响应。
        语义级「答没答对」由执行方自评 results_quality 落档，此处只剥离标记、
        只记录，不裁决也不重试。
        """
        # 自评标记在所有路径统一剥离（direct/help 也可能带），避免漏给用户看
        response, quality, retry = _extract_self_check(state.get("response", "") or "")
        error = state.get("error")
        self_check = {"results_quality": quality, "retry_with": retry}

        # 通道故障：cc-connect 节点没写 response 时用显示名给暂不可用提示；
        # 已写 response（多为诚实的错误说明）则透传
        if state.get("no_recover"):
            if response:
                return {"response": response, "gate_blocked": True, **self_check}
            agent_label = self._last_tried_label(state)
            return {
                "response": f"⚠️ {agent_label} 暂不可用（服务未响应），请稍后再试",
                "gate_blocked": True,
                **self_check,
            }

        if error:
            return {"response": f"⚠️ {error}", "gate_blocked": True, **self_check}

        if not response:
            return {"response": "⚠️ 未获取到回复，请稍后再试",
                    "gate_blocked": True, **self_check}

        # 常规路由：去向以尾注形式并入最终回复（替代 routed_receipt 中间消息）。
        # 防重：handler LLM 会模仿历史里的尾注样式自己写一个（曾出现一条回复挂
        # 两条「由 XX 处理」），已含尾注则不再追加。
        label = self._initial_route_label(state)
        if label and "_🤖 由" not in response:
            response = f"{response}\n\n_🤖 由 **{label}** 处理_"

        return {"response": response, **self_check}

    def _initial_route_label(self, state: AgentState) -> str:
        """本轮初始路由目标的显示名；help/direct/clarify 不标注（语义自明）。"""
        route_to = state.get("route_to", "")
        if route_to in ("direct_answer", "route_to_help", "route_to_clarify"):
            return ""
        agent_name = ""
        for sp in state.get("spans", []) or []:
            if sp.get("span") == "router":
                agent_name = sp.get("route_to", "") or ""
                break
        if not agent_name:
            return ""
        return self._display_name(agent_name)

    def _last_tried_label(self, state: AgentState) -> str:
        """route_trace 中最后一个 agent 的显示名（通道故障提示用）。"""
        trace = state.get("route_trace") or []
        for name in reversed(trace):
            if name:
                return self._display_name(name)
        return "服务"

    def _agent_hard_timeout(self, agent_name: str) -> int:
        """agent.yaml 的 timeout_sec → 进程内通道彻底放弃的硬上限。

        该字段此前只被 registry 解析、无人消费（等于没配）；现在作为后台守望的
        兜底期限。不给无限等待——一次挂死的 A2A 调用会永久占着一个后台槽。

        下限取同步窗口 +30s：声明值低于同步窗口会让「转后台」与「已放弃」同时
        发生在同一时刻，用户只看到失败回执。真实耗时（logs/tasks 观测 p90）
        该写在 agent.yaml 里，不在这再抄一份。
        """
        agent = self._registry.get(agent_name) if self._registry else None
        try:
            declared = int(getattr(agent, "timeout_sec", 0) or 0)
        except (TypeError, ValueError):
            declared = 0
        return max(declared, int(_INPROC_SYNC_WAIT_SEC) + 30)

    def _display_name(self, agent_name: str) -> str:
        """agent 名 → 显示名（读 registry，无则原样）。"""
        if not self._registry:
            return agent_name
        agent = self._registry.get(agent_name)
        return agent.display_name if agent else agent_name


# ── 便捷函数 ──────────────────────────────────────────────────────────────


def create_orchestrator(
    cc_client: CCConnectClient,
    config: RouterConfig,
    registry: AgentRegistry | None = None,
    handler_registry: AgentHandlerRegistry | None = None,
) -> CrewOrchestrator:
    """创建编排器实例的便捷函数。"""
    return CrewOrchestrator(
        cc_client=cc_client,
        config=config,
        registry=registry,
        handler_registry=handler_registry,
    )