"""专属领域 agent 集合 — 从 agent.yaml 的 exclusive_domain 声明派生。

用途：编排层读这个集合，保证「专属领域不被通用兜底稀释」：
1. Router 多意图分派时抑制专属域的并行 extra_calls（其 extra 是同一意图的
   冗余分派，cc-connect 会话繁忙时会拖满 90s 超时导致整条消息空返回）。
2. /help 与运行时能力清单按专属域分组展示。

（原「纠错重路由规则表」REROUTE_RULES / match_reroute_rule 已随 judge+recover
节点一并移除：178 条任务实测真实救回率 0.6%，且把「诚实失败」转成「编造成功」。）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── 兜底能力边界 ──────────────────────────────────────────────────────────

# 强专业领域集合 — registry 缺席时的静态兜底。
# 这些领域都有专属工具能力，通用 Agent（dev-bot / direct_answer）没有对应工具，
# 任何把它们的请求推给通用 Agent 的路径都必然答非所问。
#
# 数据来源已迁移到各 agent.yaml 的 `exclusive_domain: true` 声明
# （见 exclusive_domain_agents()）；本常量仅作为 registry 缺席时的静态兜底保留。
LEGACY_HARD_PROFESSIONAL_AGENTS = {
    "db-agent",
}


def exclusive_domain_agents(registry) -> set:
    """从 registry 派生专属领域 agent 集合（agent.yaml 声明 exclusive_domain: true）。

    Args:
        registry: AgentRegistry 实例（可为 None — 返回 legacy 静态集合兜底）。

    Returns:
        专属领域 agent 名集合。
    """
    if registry is None:
        return set(LEGACY_HARD_PROFESSIONAL_AGENTS)
    try:
        return {a.name for a in registry.list() if a.exclusive_domain}
    except Exception as e:  # registry 异常不阻断编排，退回静态集合
        logger.warning("[reroute-rule] exclusive_domain derivation failed: %s", e)
        return set(LEGACY_HARD_PROFESSIONAL_AGENTS)
