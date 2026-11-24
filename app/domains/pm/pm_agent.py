"""PM Agent - 带记忆的 tool-calling 循环

它解决什么问题
--------------
改造前：`process_user_query` 是一条单向流水线

    text ──► 意图分类(LLM 一次) ──► 查表选 handler ──► 拼一段文字 ──► 结束

这条流水线有三个死穴，用户的评价是「像个查询终端，不像助手」：

    1. 不会多轮   上一句「催一下 10.29」，下一句「第3条改成…」，它不知道第3条是什么
    2. 不会组合   问「哪些需求今天必须提测」要分三次问才能拼出答案
    3. 不会兜底   分类错了就答错，没有「先查一下再决定」的余地

改造后：一个循环

    text ──► ┌────────────────────────────────┐
             │  messages = 系统 + 历史 + 上下文  │
             │                                  │
             │  ┌─► LLM 决定：调工具 or 直接回答 │
             │  │        │                      │
             │  │   调工具 → 执行 → 结果回灌 ──┐ │
             │  │        │                    │ │
             │  └────────┴────────────────────┘ │
             │            │                     │
             │       直接回答 → 返回            │
             └────────────────────────────────┘

为什么保留旧路由
--------------
LLM 网关是外部依赖。挂了不能整个机器人哑掉。
所以 run() 在拿不到任何结果时返回 None，由调用方回落到旧的关键词路由。
两条路径共享同一批工具，能力不会分叉。

关于「短路」
----------
工具返回 render 时（催办草稿、日计划、写表预览），循环立刻把这段成品返回给用户，
不再让模型复述。模型复述只会把已经排好的版式和语气弄坏。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from app.domains.pm import pm_awareness, pm_session
from app.domains.pm.pm_intent_router import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from app.domains.pm.pm_tools import ToolContext, execute, spec

logger = logging.getLogger("pm_hub.agent")

# 一次提问最多几轮工具调用。
# 3 轮只够「查一次→再查一次→回答」。真实问法经常是
# 「查本周计划 → 查其中一个版本 → 再查这个版本的卡点 → 汇总」，
# 3 步会卡在最后一步，用户看到的是「工具调了一半就断」。
MAX_STEPS = 6

# 单步超时。整体最坏耗时 = MAX_STEPS * 该值，所以别调大。
STEP_TIMEOUT_SEC = 35

# 整轮对话的总时长上限。6 × 35 = 210 秒，群里没人等这么久。
# 超预算就用已有的工具结果收口 —— 宁可答一半，也别让人盯着「正在输入」。
TOTAL_BUDGET_SEC = 90

_WEEKDAYS = "一二三四五六日"


SYSTEM_PROMPT = """你是 AgentMate，示例科技 PMO 的项目经理助手，只服务李四一个人。

【怎么说话】
像同事聊天，不像系统通知。
- 不要「您好」「根据查询结果」「建议您」「综上所述」
- 他要什么给什么，不要罗列用不上的信息
- 不确定就说不确定，不要编。查不到就说查不到

【怎么干活】
- 你能查排期、起草催办、录日计划、改事项状态、查人物、查知识库、估工时
- 还能记东西和回忆：他说「记一下」就调 pm_capture_note 存原话；
  他说「上次说的」「之前记的」「我们以前怎么定的」先调 pm_recall 查，
  查不到就说查不到，**绝对不要凭印象编一段以前的结论**
- 该调工具就调，不要凭印象回答数据类问题
- 改状态、录计划这类写操作，工具自己会返回预览让用户确认，你不用再追问一遍
- 工具返回的成品（日计划、催办草稿、写表预览）会直接展示给用户，你不要复述它

【三种「查」不要混】
- pm_recall      他以前记过的东西、组织知识库、SOP        ←「上次说的」「有没有文档」
- pm_whats_new   机器人自己盯到的群聊私聊消息              ←「今天群里有什么」「谁找我了」
- pm_my_todos    过审时确认要跟进、还没做完的              ←「我还欠什么」
问的是哪类就用哪个。**不要用 pm_recall 回答「今天群里有什么」**，那是两回事。
如果我上面的【你掌握到的现场】里已经写明了，就直接答，不用再查一遍。

【编号 + 状态 = 改进度】
用户说「5 7 已完成」「第 3 条阻塞了」时，这是在对「每周计划跟踪」改进度，
直接调 pm_update_weekly，编号原样传（「5 7」拼成一个字符串传，不要拆成两次调用）。
不要先反问「你指的是哪份清单」—— 编号对不上时工具自己会列出本周全部事项让他挑。

【能力边界 - 这条最重要】
你没有权限代替用户给别人发消息。催办草稿只是草稿，需要他自己复制发出去。
**绝对不要说「已发出」「已通知」「已催」「已同步给对方」这类话。**
你只能描述你实际做了什么：拟定、暂存、写入表格。
拿不准某个动作是否真的执行了，就照实说「已经拟好了」而不是「已经发了」。

【不要过度行动】
用户问「谁卡着」「有哪些风险」是在要情况说明，不是在要催办。
只有他说「催一下」「帮我问问」「写个话术」才用起草类工具。
问情况就用排期/卡点类工具，把事实列出来。

【他表态之后必须落盘】
他说「这件办完了」「这条别管了」「下周再说」「重新捡回来」时，
调 pm_thread 的 action=settle 把处置写进去。
不要只回一句「好的记下了」—— 不写进去，明天它还会原样冒出来，
这正是他最烦的「说了跟没说一样」。
指代不清（「那个」「第二条」）先看【当前上下文】，对不上就问一句再动。

【理解指代】
用户说「第3条」「那个」「改成灿哥那个」时，看【当前上下文】里的编号清单。
上下文里没有对应清单，就老实问是哪一条，不要猜。

【不是 PM 的事怎么说】
域外的问题（工单、发布、CMDB、Nacos、数据库、PRD 文档、资讯日报、定时任务…）
由平台主链的其它 agent 负责，你答不了也不该猜。
遇到这类问题**不要自己编答案**，按格式回一行：

    RELAY: <把用户原话原样放在这里>

系统会把这条转给主链，主链处理完把结果交回来给你。判据是「这件事和交付排期
有没有关系」：有关系的（某需求卡在谁手上、这版什么时候上）留下来自己答；
没关系的（帮我建个工单、查下 SIT 日志）才转。
拿不准就别转，先问一句「这是要我查排期，还是要工单/发布那边处理？」

【格式】
飞书文本消息不渲染 markdown。不要用 **加粗**、# 标题、表格、代码块。
用短行、序号、· 分隔。一次回答控制在 10 行以内。
"""


# `pm_agent.run` 判「这问题不归我」时返回的前缀。daemon 看到它就转主链。
# 用前缀而不是异常：`run` 的返回值本来就是给用户看的文本，加一个可识别的
# 哨兵比新增一条异常路径改动小，也不会让降级链路误以为是故障。
RELAY_PREFIX = "[[RELAY]]"


def _build_system(session) -> str:
    today = date.today()
    head = (
        f"{SYSTEM_PROMPT}\n"
        f"【今天】{today.isoformat()} 周{_WEEKDAYS[today.weekday()]}"
    )
    # 现场先于上下文：先让他知道「眼下有什么」，再谈「上一轮说到哪」。
    try:
        live = pm_awareness.overview()
    except Exception:
        logger.exception("读取感知全景失败")
        live = ""
    ctx = session.render_ctx() if session else ""
    # 旧对话提示放最后，紧贴下面要拼进去的 turns —— 位置本身就是提醒。
    stale = session.stale_note() if session else ""
    tail = "\n\n".join(x for x in (live, ctx, stale) if x)
    return head + ("\n\n" + tail if tail else "")


def _chat(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Optional[dict]:
    """单次 LLM 调用，返回原始 message 字典。失败返回 None。"""
    try:
        resp = requests.post(
            LLM_BASE_URL,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.2,
            },
            timeout=STEP_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            logger.warning("LLM 非 200: %s %s", resp.status_code, resp.text[:200])
            return None
        return resp.json()["choices"][0]["message"]
    except Exception as e:
        logger.warning("LLM 调用失败: %s", e)
        return None


def _clean_assistant(msg: dict) -> Dict[str, Any]:
    """只保留协议字段。

    网关可能塞 reasoning_content 之类的东西，原样回灌下一轮有概率被拒。
    """
    out: Dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                },
            }
            for i, tc in enumerate(msg["tool_calls"])
        ]
    return out


def run(text: str, sender: str) -> Optional[str]:
    """跑一轮对话。返回给用户的文本；完全失败返回 None 让上层降级。

    注意：**成功才写会话**。中途失败不落任何痕迹，
    否则降级路径再处理一次，会话里会留下两条重复的用户消息。
    """
    session = pm_session.get(sender)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": _build_system(session)}]
    messages.extend(session.render_turns())
    messages.append({"role": "user", "content": text})

    tools = spec()
    ctx = ToolContext(sender=sender, session=session)
    deadline = time.monotonic() + TOTAL_BUDGET_SEC

    for step in range(MAX_STEPS):
        if time.monotonic() >= deadline:
            logger.warning("对话超过总预算 %ds，用已有结果收口", TOTAL_BUDGET_SEC)
            break

        msg = _chat(messages, tools)
        if msg is None:
            logger.warning("步骤 %d LLM 无响应，终止循环", step)
            return None

        tool_calls = msg.get("tool_calls") or []

        # ── 直接回答 ──
        if not tool_calls:
            answer = (msg.get("content") or "").strip()
            if not answer:
                return None
            # 域外问题按协议回 `RELAY: <原话>`，原样再抛给上层转主链。
            # 不在这里直接调 relay：`pm_agent` 只管对话，投递是 daemon 的事，
            # 而且 relay 失败时要能退回本地兜底——那需要 daemon 的上下文。
            if answer.upper().startswith("RELAY:"):
                logger.info("判定为域外问题，抛给上层转发主链")
                return RELAY_PREFIX + answer.split(":", 1)[1].strip()
            session.say("user", text)
            session.say("assistant", answer)
            return answer

        # ── 调工具 ──
        messages.append(_clean_assistant(msg))

        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}

            logger.info("执行工具 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])
            outcome = execute(name, args, ctx)

            # 成品直接给用户，不让模型复述
            if outcome.render:
                session.say("user", text)
                session.say("assistant", outcome.render)
                return outcome.render

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": outcome.content[:4000],
            })

    # 步数用尽还没收敛，用最后一次工具结果保底
    logger.warning("达到最大步数 %d，未收敛", MAX_STEPS)
    last = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "tool" and m.get("content")),
        None,
    )
    if last:
        session.say("user", text)
        session.say("assistant", last)
        return last
    return None