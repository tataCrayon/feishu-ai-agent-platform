"""创建评估 — 判定定时任务请求是否超出当前能力面，超出即驳回。

红线（需求约束，全部硬编码，评估 LLM 意见只是补充说明）：
1. 执行语义必须落在本 bot 已注册 Agent 的能力清单内（agent 参数须在
   registry.enabled() 名单中；skill 合成 Agent 除外——skill 不承诺常驻）
2. 输出形态只允许「发消息」：目标必须是 oc_/ou_ 开头的真实 ID
3. 扫描间隔 ≥ 10 分钟（store.validate_schedule 兜底二次校验）
4. 不支持：定时改配置/触发发布/发文件/建文档等写操作

评估不调 LLM —— 能力面判定是确定性的（查 registry + 前缀 + 间隔），
LLM 反而引入漏判风险。评估结论逐条落在 eval_reason，供回执卡片展示。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.ops.scheduler.store import (
    MIN_INTERVAL_MINUTES,
    ScheduledTask,
)

logger = logging.getLogger("scheduler.evaluator")


@dataclass
class EvalReport:
    """创建评估结论。"""

    ok: bool
    reasons: list[str] = field(default_factory=list)   # 通过说明
    blocks: list[str] = field(default_factory=list)    # 驳回原因（非空即拒）

    def summary_lines(self) -> list[str]:
        """卡片展示行：通过项 + 驳回项合并。"""
        lines = [f"✅ {r}" for r in self.reasons]
        lines.extend(f"❌ {b}" for b in self.blocks)
        return lines

    def block_lines(self) -> list[str]:
        """只取驳回原因：驳回卡片不需要把「哪几项通过了」也列一遍。"""
        return list(self.blocks)


class ScheduledTaskEvaluator:
    """定时任务创建评估器 — 确定性规则判定是否超出能力。"""

    def __init__(self, allowed_agents: list[str] | None = None):
        """allowed_agents：可承接定时执行的执行方名单（技能目录里的能力名）。

        缺省给 `aihot` 兜底名单（单元测试/独立脚本场景）；`ScheduledTaskService`
        启动时注入带默认可调用能力的名单，热插拔后追加。
        """
        self._allowed_agents = allowed_agents or ["aihot"]

    def reload_agents(self, agents: list[str]) -> None:
        """热插拔后刷新可承接 Agent 名单（HotReloader.rebuild 链调用）。"""
        self._allowed_agents = list(agents)

    def evaluate(self, task: ScheduledTask) -> EvalReport:
        """对一条新任务做全量评估。驳回时档案迁移 rejected/，不再纳入调度。"""
        reasons: list[str] = []
        blocks: list[str] = []

        # ── 1. 承接 Agent 在能力清单内 ──
        if task.agent in self._allowed_agents:
            reasons.append(f"执行方 {task.agent} 在当前能力清单内")
        else:
            blocks.append(
                f"执行方 {task.agent} 不在当前能力清单内"
                f"（可用：{'、'.join(self._allowed_agents)}）"
            )

        # ── 2. 输出形态：只允许发消息，目标是真实 ID ──
        if task.target_type == "group":
            reasons.append("投递目标为群聊（发消息）")
        elif task.target_type == "user":
            reasons.append("投递目标为私聊（发消息）")
        else:
            blocks.append(
                "投递目标不是有效的会话 ID（群 oc_ / 用户 ou_ 开头）——"
                "定时任务只支持向群或用户发消息，不支持按名称投递"
            )

        # ── 3. 调度参数（含 10 分钟下限红线）──
        sched_err = task.validate_schedule()
        if sched_err:
            blocks.append(sched_err)
        else:
            reasons.append(f"调度合法（{task.describe()}，≥{MIN_INTERVAL_MINUTES} 分钟）")

        # ── 4. 指令非空 ──
        if task.instruction.strip():
            reasons.append("执行指令已登记")
        else:
            blocks.append("执行指令为空")

        ok = not blocks
        if not ok:
            logger.info(
                "[scheduler] eval rejected: task=%s blocks=%s",
                task.task_id, blocks,
            )
        return EvalReport(ok=ok, reasons=reasons, blocks=blocks)


__all__ = ["EvalReport", "ScheduledTaskEvaluator"]
