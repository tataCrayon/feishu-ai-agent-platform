"""dsh 链路的任务档案 — 让控制台能看到走内核的消息。

为什么单独一层：`TaskRecord` 只由旧编排链路（`crew.py`）写过。dsh 迁移后主链路
改由 `DshGateway` 承担，控制台的「任务档案」「📄 报告」两张表因此完全看不到线上
消息——用户看到的是「最近没有任务」，实际是记到别处去了。

口径（与编排层一致，不要另立一套）：
- `route_chain` 记这轮用过的工具名（dsh 里没有「路由到某 Agent」这一步，
  能力名体现在技能调用与 MCP 工具上）
- `status` 取答复有无；`error` 只在调用抛错时填
- 回复全文进 ReplyStore（`logs/task_replies/{task_id}.md`），档案只留 200 字预览

失败语义：本模块**不抛**。档案是观测面，写不进去不该让用户少拿一条回复。
"""

from __future__ import annotations

import logging

from app.observability.task_record import TaskRecord, reply_store, task_recorder
from app.observability.turn_trace import TurnStep, TurnTrace, turn_tracer

logger = logging.getLogger("dsh.task_record")

# route_chain 里最多保留的工具名：一轮工具调用可能几十次，全铺开档案会撑大
# 且控制台的链路视图放不下；只留前 N 个去重后的名字。
_MAX_CHAIN = 8

__all__ = ["record_dsh_task"]


def record_dsh_task(
    *,
    task_id: str,
    chat_id: str,
    sender_id: str,
    sender_name: str,
    query: str,
    reply: str,
    tool_names: list[str] | None = None,
    duration_ms: float = 0.0,
    error: str = "",
    merged_count: int = 1,
    memory: str = "",
    steps: list[dict[str, str]] | None = None,
) -> None:
    """落一条 dsh 任务档案 + 回复全文。

    Args:
        task_id: 与 trace_id 同值（Dashboard 按它拉全文）。
        tool_names: 本轮调用过的工具名（进度事件里收集，可重复）。
        error: 非空即 status=error；空则按有无答复判 success/no_response。
        memory: 本轮注入的长期记忆（空串 = 没召回）。
        steps: `DshGateway.last_turn.step_list()` 的工具步骤（名/状态/入参/返回）。
            传了就同时落 `logs/turns/`（Markdown 全文 + 索引行），供复盘与优化。
    """
    chain: list[str] = []
    for name in tool_names or []:
        if name and name not in chain:
            chain.append(name)
        if len(chain) >= _MAX_CHAIN:
            break
    status = "error" if error else ("success" if reply else "no_response")
    try:
        task_recorder.write(TaskRecord(
            task_id=task_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            query=query[:300],
            final_agent="dsh",
            route_chain=chain or ["dsh"],
            results_quality="ok" if reply else "failed",
            status=status,
            duration_ms=round(duration_ms, 1),
            response_preview=reply[:200],
            error=error[:200],
            merged_count=merged_count,
        ))
        reply_store.write(
            task_id,
            reply,
            meta={"task_id": task_id, "agent": "dsh", "status": status,
                  "query": query[:200]},
        )
    except Exception as e:
        logger.warning("[dsh] 任务档案写入失败（不影响回复）：%s", e)

    # 轨迹单写一次：档案写失败不该连带丢掉「怎么处理的」过程证据
    # （排查时前者能重建，后者不能——工具入参只在内存里活一轮）。
    if steps is not None or memory:
        turn_tracer.write(TurnTrace(
            task_id=task_id,
            query=query,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            memory=memory,
            steps=[TurnStep(**step) for step in (steps or [])],
            reply=reply,
            status=status,
            error=error,
            duration_ms=duration_ms,
        ))
