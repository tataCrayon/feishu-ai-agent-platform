"""能力开关 — Agent 路由的运行时上下线名单（控制台动态控制）。

落盘 bot/todos/agent_switches.json：

    {"skill-aihot": false, "schedule-agent": true}

语义：值为 false 的 Agent 被「强制下线」——路由工具表、/help 清单、
复杂任务分解列表全部不可见（registry.routable() 联合判定），但
enabled/健康探测/直通分派（/dev）不受影响；缺省（不在文件中）= 开。
值为 true 仅表示「未禁用」，不会把 agent.yaml 里 routable=false 的
Agent（如 dev-bot）强行上线。

写路径：控制台 POST /api/agents/switch → save_switches() →
registry.apply_switches() → CrewOrchestrator.rebuild()（见 app/gateway/api/dashboard.py）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("agents.switches")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # app/agents/ -> feishu-agent-platform/
SWITCHES_FILE = PROJECT_DIR / "bot" / "todos" / "agent_switches.json"


def load_switches() -> set[str]:
    """读取强制下线名单；文件缺失/损坏返回空集（全部可用）。"""
    if not SWITCHES_FILE.exists():
        return set()
    try:
        data = json.loads(SWITCHES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[switches] read failed %s: %s", SWITCHES_FILE, e)
        return set()
    if not isinstance(data, dict):
        return set()
    return {name for name, on in data.items() if on is False and name}


def save_switches(switch_off: set[str]) -> None:
    """原子写回开关文件（只持久化被禁用的名字，开=缺省不落盘）。"""
    SWITCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: False for name in sorted(switch_off)}
    tmp = SWITCHES_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(SWITCHES_FILE)
