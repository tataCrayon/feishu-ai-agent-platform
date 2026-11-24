"""用户负反馈检测 — 从下一条消息识别对上一轮回复的不满。

设计：
- 规则优先（零成本、毫秒级）：短文本（≤20字）+ 负面表达模式 → 判定为负反馈
- 命中后回填到 TaskRecorder 最近一条任务记录的 feedback 字段，
  关联出「哪类 agent / 哪类问题效果不好」
- 不拦截消息本身：判定后消息照常进入编排流程（用户可能边吐槽边补充需求）
- 保守策略：长查询里偶然出现"不对"等词不误伤（长度门槛挡住）

正反馈暂不记录——负反馈才是优化信号，符合 EDD 原则。
"""

from __future__ import annotations

import re

# 负面表达模式（精确匹配意图，避免长句误伤）
_NEGATIVE_PATTERNS: list[str] = [
    "不对", "错了", "答非所问", "胡说", "乱答", "瞎说", "没用",
    "听不懂", "没听懂", "不是这个意思", "重新回答", "重试", "再来一次",
    "什么玩意", "垃圾", "差劲", "太差", "不准确", "不靠谱", "离谱",
    "答错了", "理解错了", "搞错了", "别乱说", "牛头不对马嘴",
]

# 编译为单 pattern（交替），全词包含即命中
_NEGATIVE_RE = re.compile("|".join(re.escape(p) for p in _NEGATIVE_PATTERNS))

# 反馈判定最大长度：超过视为正常业务消息（内含负面词不算反馈）。
# 用户对 bot 的吐槽基本是短句（"不对"/"答非所问"/"重新回答"），
# 业务讨论中偶含"不对"的句子通常远长于该阈值。
_FEEDBACK_MAX_LEN = 20


def detect_negative_feedback(text: str) -> dict | None:
    """检测是否为负反馈表达。

    Args:
        text: 用户消息文本。

    Returns:
        命中返回 {"negative": True, "text": ..., "matched": ...}；未命中 None。
    """
    t = (text or "").strip()
    if not t or len(t) > _FEEDBACK_MAX_LEN:
        return None

    m = _NEGATIVE_RE.search(t)
    if not m:
        return None

    return {
        "negative": True,
        "text": t[:100],
        "matched": m.group(0),
    }
