"""回复反馈按钮 — 每条正式答复自带 🙂/🙃，点一下即回填到该轮轨迹。

## 为什么要有这一层

负反馈原先只有一条通道：用户紧接着发一句「不对 / 答非所问」（`feedback.py` 的
规则匹配）。实测漏得多——大部分人不满就不说话了，或者隔很久才补一句，而规则
只认 20 字以内的短句。结果就是「哪轮答得不好」只能靠人工翻轨迹猜。

按钮把判定变成一次明确的点击：**点 🙃 就是一次确凿的负反馈**，不必用户再打字。
点击走卡片回调（`card_action`），payload 里带 `task_id`，直接定位到那一轮，
不需要「找该用户最近一条档案」这种有歧案的推断。

## 为什么不把按钮画在同一个气泡里

飞书 markdown 文本消息不能带交互按钮，按钮只在卡片里。所以答复正文之后**追加
发送一张极简反馈卡**（只有一行 + 两个按钮）。这样：
- 正文保持原样（长报告仍是 markdown 排版，不被卡片渲染规则改写）
- 反馈卡可随时点、不影响阅读；点完原地更新成「已记录」
- 关掉这个开关（`config.toml [projects.messaging].feedback_buttons=false`）就退回
  纯文本，不改变现有行为

## 失败语义

不抛。反馈卡发不出去 / 点击处理失败，都不该影响答复本身——观测是副产品。
"""

from __future__ import annotations

import logging
from typing import Any

from app.protocols.feishu_card import ensure_button_behaviors

logger = logging.getLogger("protocols.feedback_card")

# 回调 payload 的判别键：卡片回调会打到所有注册的 handler，靠 `fb` 认领
FB_KEY = "fb"
# 键名不能叫 `task`：定时任务审批卡用的是 `{task, act}`，同名会让审批 handler
# 把反馈卡的点击当成「点了一张已失效的审批卡」，回用户一句「未找到定时任务档案」。
TASK_KEY = "fb_task"
ACTION_UP = "up"
ACTION_DOWN = "down"

__all__ = [
    "ACTION_DOWN",
    "ACTION_UP",
    "FB_KEY",
    "build_done_card",
    "build_feedback_card",
    "is_feedback_action",
    "parse_feedback_value",
]


def build_feedback_card(task_id: str) -> dict[str, Any]:
    """答复后追加的极简反馈卡（一行提示 + 两个按钮）。"""
    if not task_id:
        raise ValueError("task_id 不能为空")
    return ensure_button_behaviors({
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "这条回答对你有帮助吗？",
                },
                {
                    "tag": "column_set",
                    "flex_mode": "flow",
                    "columns": [
                        {"tag": "column", "width": "auto", "elements": [
                            {"tag": "button", "text": {"tag": "plain_text", "content": "🙂 有帮助"},
                             "type": "default",
                             "value": {FB_KEY: ACTION_UP, TASK_KEY: task_id}},
                        ]},
                        {"tag": "column", "width": "auto", "elements": [
                            {"tag": "button", "text": {"tag": "plain_text", "content": "🙃 不太行"},
                             "type": "default",
                             "value": {FB_KEY: ACTION_DOWN, TASK_KEY: task_id}},
                        ]},
                    ],
                },
            ]
        },
    })


def build_done_card(task_id: str, positive: bool, operator: str = "") -> dict[str, Any]:
    """点击后原地更新的回执卡（无按钮：一次反馈即终态，避免重复点改判）。"""
    mark = "🙂 已记录：有帮助" if positive else "🙃 已记录：这条不太行，会拿去做改进"
    who = f"（{operator}）" if operator else ""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"{mark}{who}"},
                {"tag": "markdown", "content": f"<font color='grey'>task {task_id}</font>"},
            ]
        },
    }


def is_feedback_action(event: Any) -> bool:
    """是不是我们这张反馈卡的点击。

    卡片回调会广播给所有注册的 handler（能力审批、定时任务、通知），每个都得
    自己判断「这条是不是我的」。判据只看 value 里的 `fb` 键——不认识就静默让过。
    """
    value = _action_value(event)
    return isinstance(value, dict) and value.get(FB_KEY) in (ACTION_UP, ACTION_DOWN)


def parse_feedback_value(event: Any) -> tuple[str, bool]:
    """→ (task_id, 是否正向)。非反馈卡返回 ("", False)。"""
    value = _action_value(event)
    if not isinstance(value, dict):
        return "", False
    task_id = str(value.get(TASK_KEY) or "")
    return task_id, value.get(FB_KEY) == ACTION_UP


def _action_value(event: Any) -> Any:
    action = getattr(event, "action", None)
    return getattr(action, "value", None) if action is not None else None
