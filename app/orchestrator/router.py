"""轻量 LLM 路由 — OpenAI Function Calling 实现意图识别。

封装 OpenAI 兼容 API 的 Function Calling 调用，用于：
- 意图识别：判断用户消息应该路由到哪个子 Agent
- 复杂任务分解：将复杂任务拆解为多步子任务
- 直接回答：简单问候/闲聊直接返回回复

提示词从 bot/prompts/ 加载，由 PromptLoader 统一管理。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai

from app.agents.registry import AgentDef, AgentRegistry
from app.config import RouterConfig
from app.llm import (
    CacheMetrics,
    build_cacheable_messages,
    estimate_cost,
    extract_cache_metrics,
)
from app.multimodal import build_user_content
from app.prompts.loader import PromptLoader

logger = logging.getLogger("orchestrator.router")

# ── 确定性路由兜底（工单域）────────────────────────────────────────

# lark_channel 的纯文件消息占位符（如 <file key="..." name="xx.msg"/>）
_FILE_TAG_RE = re.compile(r'<file\s+key="[^"]*"(?:\s+name="([^"]*)")?\s*/>')

# 工单强特征：流程编号 / CMDB DATA 单号 / 明确的工单-SQL 意图 / 审批短链
# （go.feishu.cn/approval 与「处理这个审批」裸链也须确定性兜住；案例链
#   沉淀案例/案例入库/同步案例话术同理）
_TICKET_HINT_RE = re.compile(
    r"流程编号|DATA\d{6,}|处理.{0,6}(?:工单|审批)|工单.{0,6}(?:转|生成|处理|执行|方案)|"
    r"(?:生成|出|给我?)\s*(?:完整|全部)?\s*SQL|SQL\s*(?:生成|变更|方案)|"
    r"go\.feishu\.cn/approval|审批.{0,6}(?:链接|短链|单据)|"
    r"沉淀.{0,6}案例|案例.{0,4}(?:入库|沉淀|同步)|同步.{0,4}案例|录入.{0,4}案例",
    re.IGNORECASE,
)

# 工单常见附件类型（与 channel 下载白名单求交，图片不属于——图片更多是
# 数字先知/截图问询场景，继续交给 LLM 路由判断）
_TICKET_FILE_EXTS = {".msg", ".eml", ".txt", ".md", ".csv", ".xlsx", ".xls",
                     ".doc", ".docx", ".pdf", ".sql", ".log"}

# ── 路由工具定义（OpenAI Function Calling 格式，registry 单源派生）──────────

# 编排层内置目标（非 registry Agent）：帮助清单 / 自答引擎
_BUILTIN_TOOL_SPECS: tuple[tuple[str, str, dict | None, list[str]], ...] = (
    (
        "route_to_help",
        "帮助/自我介绍/能力查询：用户问机器人能做什么、有什么功能、你是谁、帮助信息，"
        "或询问某个能力/技能的版本号、详情（如「xx 的版本是？」）。"
        "任何时候用户询问能力列表、帮助、自我介绍时使用此工具",
        None,
        ["query"],
    ),
    (
        "direct_answer",
        "直接回答（编排层自答）：问候、感谢、告别、闲聊，以及不需要专业 Agent 的通用问题"
        "——常识问答、概念解释、一般性讨论、对上一轮回复的简单追问。"
        "专业领域（数据库/发布/配置/工单/日报/文档/代码分析）不要用此工具",
        {
            "reply": {
                "type": "string",
                "description": "简短问候/闲聊时的直接回复；较长问题可留空，由自答引擎生成",
            },
        },
        [],
    ),
    (
        "route_to_clarify",
        "意图澄清（拿不准时反问）：消息含糊、多个候选 Agent 难以抉择、缺少关键信息时使用。"
        "宁可反问确认，不要猜错路由。清晰的专业请求不要用此工具",
        {
            "question": {
                "type": "string",
                "description": "向用户提出的澄清问题（一句话，具体、可直接回答）",
            },
            "candidates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 个候选方向的语义描述，如 ['数据库查询','工单创建']",
            },
        },
        ["question"],
    ),
)


def tool_name_for(agent_name: str) -> str:
    """agent 名 → 路由工具名：db-agent → route_to_db_agent。"""
    return "route_to_" + agent_name.replace("-", "_")


def _tool_schema(
    name: str,
    description: str,
    extra_params: dict | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """生成单个 OpenAI function-calling 工具 schema（模板函数）。

    默认仅 query 参数；额外参数由 agent.yaml 的
    route_params 声明，且视为必填。
    所有工具统一追加 confidence / intent_summary 自报字段（R1，SPEC-20260912）。
    """
    props: dict[str, Any] = {"query": {"type": "string", "description": "用户完整查询"}}
    req = list(required) if required is not None else ["query"]
    if extra_params:
        props.update(extra_params)
        req += [k for k in extra_params if k not in req]
    props["confidence"] = {
        "type": "number",
        "description": "本次路由决策的置信度 0~1：意图清晰且唯一匹配时接近 1，"
        "含糊或多个候选难以取舍时降低",
    }
    props["intent_summary"] = {
        "type": "string",
        "description": "一句话复述识别到的用户意图（40 字内），供回执与决策日志",
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": req},
        },
    }


def build_router_tools(registry) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """从 registry 派生路由工具表与命名映射（enabled 且 routable 的 Agent 纳入）。

    routable=False 的 Agent（如 dev-bot — 维护者远程开发通道）不生成路由工具，
    路由 LLM 看不到也到不了；新增 Agent/Skill 只需进 registry（目录发现或
    SKILL.md 注入）即可获得路由工具，无需修改本文件 —— agent.yaml 的
    description 直接用作工具描述，因此该字段应写成「能力关键词 + 触发场景」
    的路由级描述。

    Returns:
        (ROUTER_TOOLS, AGENT_NAME_MAP) 二元组。
    """
    tools: list[dict[str, Any]] = []
    name_map: dict[str, str] = {}
    for a in registry.routable():
        tname = tool_name_for(a.name)
        tools.append(
            _tool_schema(tname, a.description or a.display_name,
                         extra_params=a.route_params or None)
        )
        name_map[tname] = a.name
    for name, desc, params, req in _BUILTIN_TOOL_SPECS:
        tools.append(_tool_schema(name, desc, extra_params=params, required=req))
    return tools, name_map


# 模块级默认表：import 时从 bot/agents/*/agent.yaml 派生一次（不含运行时注入的
# Skill 合成 Agent）。CrewOrchestrator 启动时会用完整 registry 重新派生并注入
# 各 LLM；此默认值供独立脚本（prompt_eval 等）与未初始化场景兜底。
_ROUTER_REGISTRY = AgentRegistry()
ROUTER_TOOLS, AGENT_NAME_MAP = build_router_tools(_ROUTER_REGISTRY)

# ── System Prompt 从 bot/prompts/ 加载，由 PromptLoader 统一管理 ──

ROUTER_SYSTEM_PROMPT = PromptLoader.load_router_system_prompt()
DECOMPOSE_SYSTEM_PROMPT = PromptLoader.load_router_decompose_prompt()


@dataclass
class RouterResult:
    """路由结果。"""

    tool_name: str  # 路由工具名，如 "route_to_db_agent"
    args: dict[str, Any]  # 工具参数，如 {"query": "..."}
    latency_ms: float  # LLM 调用耗时
    tokens: int = 0  # Token 用量
    cache_metrics: CacheMetrics = field(default_factory=CacheMetrics)  # Prompt Caching 指标
    cost_usd: float = 0.0  # 估算成本
    prompt_version: str = ""  # 路由提示词版本号
    is_fallback: bool = False  # 是否降级（LLM 失败时）
    confidence: float = 0.5  # 模型自报置信度（fallback 记 0.0）
    intent_summary: str = ""  # 模型复述的意图（决策日志/回执用）
    extra_calls: list[dict[str, Any]] = field(default_factory=list)
    # extra_calls：同一次响应里除首个之外的其他 tool_calls（ReAct 多意图并行分派）。
    # 每项 {"tool_name", "args", "confidence", "intent_summary"}，由 crew 的
    # router 节点展开为并行 agent 节点；空 = 单意图（绝大多数请求）


@dataclass
class DecomposeResult:
    """任务分解结果。"""

    sub_tasks: list[dict[str, Any]]  # 子任务列表
    latency_ms: float = 0
    tokens: int = 0


class RouterLLM:
    """LLM 路由 — 通过 OpenAI Function Calling 实现意图识别和任务分解。

    Args:
        config: RouterConfig 配置实例。
        tools: 路由工具表（可选）。缺省用模块级默认表；CrewOrchestrator 启动时
            会传入由完整 registry（含 Skill 合成 Agent）派生的最新表。
        agent_name_map: 工具名 → agent 名映射（可选，与 tools 配套）。
    """

    def __init__(
        self,
        config: RouterConfig,
        tools: list[dict[str, Any]] | None = None,
        agent_name_map: dict[str, str] | None = None,
        registry: AgentRegistry | None = None,
    ):
        self._config = config
        # connect 单独给足余量：内网网关抖动窗口（TCP/TLS 建连可达 10s+），
        # 标量 timeout 会把 connect 压到 5s 以下导致整轮 APIConnectionError
        llm_timeout = httpx.Timeout(
            connect=max(10.0, config.timeout_sec),
            read=float(config.timeout_sec),
            write=float(config.timeout_sec),
            pool=float(config.timeout_sec),
        )
        self._client = openai.AsyncClient(
            api_key=config.api_key,
            base_url=config.base_url,
            max_retries=config.max_retries,
            timeout=llm_timeout,
        )
        self._tools = tools if tools is not None else ROUTER_TOOLS
        self._agent_name_map = agent_name_map if agent_name_map is not None else AGENT_NAME_MAP
        self._registry = registry
        # 预加载提示词版本号（用于打点）
        self._router_prompt_version = PromptLoader.get_prompt_version("router/system_prompt.md")
        self._decompose_prompt_version = PromptLoader.get_prompt_version("router/decompose_system_prompt.md")

    def update_tools(
        self,
        tools: list[dict[str, Any]],
        agent_name_map: dict[str, str] | None = None,
    ) -> None:
        """热更新路由工具表（能力热插拔 rebuild 调用）。

        下一次 route() 即用新表；prompt 占位符按新表实时渲染，无需额外处理。
        """
        self._tools = tools
        if agent_name_map is not None:
            self._agent_name_map = agent_name_map

    # ── prompt 占位符渲染（Registry 单源注入）──────────────────────────────

    def _render_router_prompt(self) -> str:
        """渲染路由 system prompt 的 {{AGENT_LIST}} — 工具清单来自派生的 tools。"""
        prompt = ROUTER_SYSTEM_PROMPT
        if "{{AGENT_LIST}}" not in prompt:
            return prompt
        lines = [
            f"- {t['function']['name']} — {t['function']['description']}"
            for t in self._tools
        ]
        return prompt.replace("{{AGENT_LIST}}", "\n".join(lines))

    def _render_decompose_prompt(self) -> str:
        """渲染分解 system prompt 的 {{AGENT_LIST_DECOMPOSE}} — agent 名单。

        registry 可用时含 Skill 合成 Agent；否则从 name_map 反推兜底。
        """
        prompt = DECOMPOSE_SYSTEM_PROMPT
        if "{{AGENT_LIST_DECOMPOSE}}" not in prompt:
            return prompt
        entries: list[str] = []
        if self._registry is not None:
            enabled: list[AgentDef] = list(self._registry.routable())
            for a in enabled:
                entries.append(f"- {a.name}：{a.description or a.display_name}")
        else:
            for tool, agent in self._agent_name_map.items():
                entries.append(f"- {agent}（{tool}）")
        return prompt.replace("{{AGENT_LIST_DECOMPOSE}}", "\n".join(entries))

    # ── 确定性工单兜底 ────────────────────────────────────────────

    def _ticket_result(
        self, message: str, latency_ms: float, reason: str,
        *, require_hint: bool,
    ) -> RouterResult | None:
        """工单特征命中 → webportal-agent 路由结果；不命中返回 None。

        require_hint=False（纯附件场景）：仅凭「工单文档类附件」即可命中；
        require_hint=True（LLM 失败兜底）：须再命中工单关键词，避免把闲聊
        误送进引擎。confidence 取 0.85（> clarify_threshold 0.6，不降级）。
        """
        tool = "route_to_webportal_agent"
        if tool not in self._agent_name_map:
            return None

        names = _FILE_TAG_RE.findall(message)
        stripped = _FILE_TAG_RE.sub("", message).strip()

        if not stripped:
            # 纯附件消息：全部为工单文档类才确定性命中
            if not names:
                return None
            exts = {("." + n.rsplit(".", 1)[-1].lower()) if "." in n else ""
                    for n in names}
            if not exts or not exts <= _TICKET_FILE_EXTS:
                return None
        elif not require_hint or not _TICKET_HINT_RE.search(message):
            return None

        intent = "工单附件/工单描述（确定性路由）"
        logger.info("Deterministic ticket route: %s — reason=%s", tool, reason)
        return RouterResult(
            tool_name=tool,
            args={"query": message, "confidence": 0.85, "intent_summary": intent},
            latency_ms=latency_ms,
            is_fallback=True,
            confidence=0.85,
            intent_summary=intent,
        )

    async def route(
        self,
        message: str,
        history: list[dict] | None = None,
        memory_context: str = "",
        media_bundle: Any | None = None,
    ) -> RouterResult:
        """判断用户消息意图，返回路由结果。

        Args:
            message: 用户消息文本。
            history: 该会话最近 N 轮对话历史（最旧在前，role=user/assistant）。
                注入后让路由 LLM 能理解指代/承接，实现多轮语义。可为 None。
            memory_context: 长期记忆上下文（用户画像/偏好/事实，来自 MemoryCore
                L1-L3）。拼接到 user 帧尾部——不放进 system（破坏前缀缓存）。
            media_bundle: MediaBundle 多模态媒体包（图片/视频/文档），非空时
                user 帧升级为图文混合 content 数组，路由 LLM 直接看媒体判意图。

        Returns:
            RouterResult 包含路由工具名和参数。
        """
        start = time.time()

        # 纯工单附件消息（LLM 前的确定性规则）：附件必须进 webportal handler
        # 的聚合缓冲——走 clarify 则附件永不落缓冲，后续文字消息出单时丢附件
        # （事故形态：Setup Mobile Number.msg 被判「解析失败」转 clarify，
        # 工单正文到达时引擎已无附件上下文）
        pre = self._ticket_result(message, 0.0, "file-only pre-route",
                                  require_hint=False)
        if pre:
            return pre

        try:
            # 使用 build_cacheable_messages 构建前缀可缓存的 messages，
            # history 置于 system 之后、最新 user 之前（system 仍为稳定前缀 → 命中缓存）。
            # system prompt 经占位符渲染注入 registry 派生的工具清单（同一进程内稳定）
            user_content: Any = message
            if memory_context:
                user_content = (
                    f"{message}\n\n"
                    f"[长期记忆参考 — 该用户的历史画像/偏好/事实，供理解指代与个性化，"
                    f"与本次请求冲突时以消息原文为准]\n{memory_context}"
                )
            # 多模态：带媒体时 user 帧升级为图文混合数组（无媒体保持字符串，
            # 前缀缓存行为不变）
            user_content = build_user_content(user_content, media_bundle)
            msgs = build_cacheable_messages(
                self._render_router_prompt(),
                user_content,
                history=history,
            )

            resp = await self._client.chat.completions.create(
                model=self._config.model,
                **self._config.sampling_params("router"),
                messages=msgs,
                tools=self._tools,
                tool_choice="auto",
                timeout=self._config.timeout_sec,
            )

            latency_ms = (time.time() - start) * 1000
            choice = resp.choices[0]
            tokens = resp.usage.total_tokens if resp.usage else 0
            cache_metrics = extract_cache_metrics(resp.usage)
            cost = estimate_cost(self._config.model, {
                "prompt_tokens": cache_metrics.input_tokens,
                "completion_tokens": cache_metrics.output_tokens,
                "cache_read_input_tokens": cache_metrics.cache_read_input_tokens,
            })

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                # ReAct 多意图：一次响应可能带多个 tool_calls（如「查日报并建定时任务」）。
                # 首个作主路由（沿用原有 clarify/decompose 闭环），其余进
                # extra_calls 由 crew 展开为并行 agent 节点。
                # 幻觉工具名由 _parse_tool_args 校验丢弃（valid_targets 在那里维护）。
                extra_calls: list[dict[str, Any]] = []
                primary: dict[str, Any] | None = None
                for tc in choice.message.tool_calls:
                    parsed_args = self._parse_tool_args(tc)
                    if parsed_args is None:
                        continue  # 幻觉工具名：丢弃该 call（与单意图防御同口径）
                    tool = parsed_args
                    if primary is None:
                        primary = tool
                    elif tool["tool_name"] in self._agent_name_map:
                        extra_calls.append(tool)
                if primary is None:
                    logger.warning(
                        "Router LLM returned unknown tool(s) %r",
                        [tc.function.name for tc in choice.message.tool_calls],
                    )
                    fb = self._ticket_result(message, latency_ms,
                                             "unknown-tool", require_hint=True)
                    if fb is not None:
                        return fb
                    logger.warning(
                        "Router LLM returned unknown tool(s) — fallback to direct_answer")
                    return RouterResult(
                        tool_name="direct_answer",
                        args={"reply": ""},
                        latency_ms=latency_ms,
                        tokens=tokens,
                        cache_metrics=cache_metrics,
                        cost_usd=cost,
                        prompt_version=self._router_prompt_version,
                        confidence=0.0,
                    )

                tool_name = primary["tool_name"]
                args = primary["args"]
                confidence = primary["confidence"]
                intent_summary = primary["intent_summary"]

                # 确定性工单改判（LLM 合法自选
                # direct_answer conf=0.9「需拒答」，被会话历史里的旧失败带偏）：
                # 消息命中工单强特征且多意图里也没有 webportal 调用时，改判送
                # webportal-agent——催单/重复请求不因历史失败改判为拒答。
                # 只换主调用，extra_calls 保留（第二意图照常并行分派）。
                if (
                    tool_name == "direct_answer"
                    and not any(
                        t["tool_name"] == "route_to_webportal_agent"
                        for t in extra_calls
                    )
                ):
                    fb = self._ticket_result(
                        message, latency_ms, "hint-override", require_hint=True)
                    if fb is not None:
                        if extra_calls:
                            fb.extra_calls = extra_calls
                        return fb

                if extra_calls:
                    logger.info(
                        "Router LLM multi-intent: %s + %d extra calls for '%s'",
                        tool_name, len(extra_calls), message[:50],
                    )

                logger.info(
                    "Router LLM: %s → %s (conf=%.2f, %.0fms, %d tokens, cache hit rate=%.1f%%)",
                    message[:50], tool_name, confidence, latency_ms, tokens,
                    cache_metrics.hit_rate * 100,
                )
                return RouterResult(
                    tool_name=tool_name,
                    args=args,
                    latency_ms=latency_ms,
                    tokens=tokens,
                    cache_metrics=cache_metrics,
                    cost_usd=cost,
                    prompt_version=self._router_prompt_version,
                    confidence=confidence,
                    intent_summary=intent_summary,
                    extra_calls=extra_calls,
                )

            # LLM 未调用工具 → 先确定性识别工单/审批消息（与 LLM 失败兜底同
            # 语义；审批单曾在这里被误送 direct_answer），兜不住才
            # direct_answer 自答（诚实自答优于乱兜底）
            logger.warning("Router LLM returned no tool call")
            fb = self._ticket_result(message, latency_ms,
                                     "no-tool-call", require_hint=True)
            if fb is not None:
                return fb
            logger.warning("Router LLM returned no tool call — fallback to direct_answer")
            return RouterResult(
                tool_name="direct_answer",
                args={"reply": ""},
                latency_ms=latency_ms,
                tokens=tokens,
                cache_metrics=cache_metrics,
                cost_usd=cost,
                prompt_version=self._router_prompt_version,
                confidence=0.0,
            )

        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            # attempt 观测：SDK timeout 是单次请求语义，max_retries=N 时最坏
            # N+1 次×timeout_sec——日志里的 latency 超过 timeout_sec 即发生了
            # SDK 静默重试，attempt 数只能从错误信息反推（APITimeoutError 带尝试次数）
            err_name = type(e).__name__
            # 网关抖动是常态（Router LLM 连接失败 103s 后回
            # direct_answer 废话，用户的工单没人接）。先确定性识别工单消息
            # 直送 webportal-agent，兜不住才回 direct_answer。
            fb = self._ticket_result(message, latency_ms,
                                     f"llm-failed({err_name})", require_hint=True)
            if fb is not None:
                logger.warning(
                    "Router LLM failed (%.0fms, err=%s): %s — deterministic "
                    "fallback → webportal-agent",
                    latency_ms, err_name, str(e)[:200],
                )
                return fb
            logger.warning(
                "Router LLM failed (%.0fms, err=%s): %s — fallback to direct_answer",
                latency_ms, err_name, str(e)[:200],
            )
            return RouterResult(
                tool_name="direct_answer",
                args={"reply": ""},
                latency_ms=latency_ms,
                is_fallback=True,
                confidence=0.0,
            )

    def _parse_tool_args(self, tool_call: Any) -> dict[str, Any] | None:
        """解析单个 tool_call：工具名 + 参数 + 自报置信度。

        幻觉工具名（不在派生工具表）返回 None 丢弃；
        confidence/intent_summary 缺省兜底，不阻断。
        """
        tool_name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        valid_targets = set(self._agent_name_map) | {
            "route_to_help", "direct_answer", "route_to_clarify",
        }
        if tool_name not in valid_targets:
            return None
        try:
            confidence = float(args.pop("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        intent_summary = str(args.pop("intent_summary", "") or "")[:80]
        return {
            "tool_name": tool_name,
            "args": args,
            "confidence": min(1.0, max(0.0, confidence)),
            "intent_summary": intent_summary,
        }

    async def decompose(
        self,
        message: str,
        history: list[dict] | None = None,
    ) -> DecomposeResult:
        """将复杂任务分解为多步子任务。

        Args:
            message: 用户消息文本。
            history: 该会话最近 N 轮对话历史（可选），用于承接上文。

        Returns:
            DecomposeResult 包含子任务列表。
        """
        start = time.time()

        try:
            msgs = build_cacheable_messages(
                self._render_decompose_prompt(),
                message,
                history=history,
            )

            resp = await self._client.chat.completions.create(
                model=self._config.model,
                **self._config.sampling_params("decompose"),
                messages=msgs,
                response_format={"type": "json_object"},
                timeout=self._config.timeout_sec,
            )

            latency_ms = (time.time() - start) * 1000
            tokens = resp.usage.total_tokens if resp.usage else 0
            cache_metrics = extract_cache_metrics(resp.usage)
            content = resp.choices[0].message.content or '{"tasks": []}'
            parsed = json.loads(content)
            sub_tasks = parsed.get("tasks", []) if isinstance(parsed, dict) else parsed

            if not isinstance(sub_tasks, list):
                sub_tasks = []

            logger.info(
                "Decompose: %d sub-tasks (%.0fms, %d tokens, cache hit rate=%.1f%%)",
                len(sub_tasks), latency_ms, tokens,
                cache_metrics.hit_rate * 100,
            )
            return DecomposeResult(
                sub_tasks=sub_tasks[:5],
                latency_ms=latency_ms,
                tokens=tokens,
            )

        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            logger.warning("Decompose LLM failed (%.0fms): %s", latency_ms, e)
            return DecomposeResult(sub_tasks=[], latency_ms=latency_ms)