"""easyprd 数据结构 — 字段与 bot/prompts/agents/prd-watch-agent.md 的输出 JSON 一一对应。

为什么用 dataclass 而不是 dict 传来传去：评论模板、台账行、日志三处都要同一批字段，
dict 少写一个键要到运行时才发现。加字段先改这里，再改 prompt 的 schema，两处不一致
会在 extract 的严格解析里暴露（未知键丢弃 + 缺键默认值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 置信度分带（分带语义见 prd-watch-agent.md；阈值本身在 config 里可调）
BAND_NORMAL = "normal"   # ≥ flag → 正式变更建议
BAND_FLAGGED = "flagged"  # min ≤ c < flag → 写评论但标「⚠️ AI 自动识别，请人工确认」
BAND_DROPPED = "dropped"  # < min → 丢弃，只计数

_CHANGE_TYPES = ("参数调整", "新增条款", "删除条款", "流程变更")


@dataclass
class PrdCandidate:
    """一个候选 PRD（= 一个 Story 的文档节选），识别器从中判断变更归属哪一篇。

    存在理由：一场「WebPortal RWA 接口对齐」会同时改动
    货架/申购/赎回/订单/持仓 5 篇 PRD。旧管道是「一群→一 Story→一 PRD」，把整场会
    塞给一篇 PRD 判，结果是把别的模块的结论也评论进这篇文档（错位比漏报更糟）。
    """

    story_id: str = ""
    story_title: str = ""
    prd_token: str = ""
    excerpt: str = ""

    @property
    def label(self) -> str:
        return f"{self.story_title}（Story #{self.story_id}）"


@dataclass
class ChangePoint:
    """一条 PRD 变更建议（= 评论里的一条 CR）。"""

    title: str = ""
    current_text: str = ""           # PRD 原文逐字引用（新增条款为空）
    suggested_text: str = ""
    quote_text: str = ""             # 定位用片段（anchor 拿它找 block_id）
    is_new_clause: bool = False
    change_type: str = "参数调整"
    modules: list[str] = field(default_factory=list)
    compliance: str = "无"
    evidence_quote: str = ""
    speaker: str = ""
    scenario: str = ""
    rationale: str = ""
    confidence: float = 0.0
    band: str = BAND_DROPPED
    block_id: str = ""               # 由 anchor 填；空 → 整篇评论降级
    # 归属的 Story（多 PRD 识别时由模型给出，extract 校验必须在候选集里）。
    # 空 = 单 PRD 路径（群讨论只绑一个 Story），由调用方补填。
    story_id: str = ""
    # 全链唯一编号，由 watcher 在出评论时填（模型不产这个，所以不在 _FIELDS 里）。
    # 放这儿是为了卡片能按条展示 CR，而不是让调用方另维护一份 id(cp)→cr 映射。
    cr_id: str = ""

    @property
    def located(self) -> bool:
        """是否锚到 PRD 段落（False 走 --full-comment 降级）。"""
        return bool(self.block_id)

    def to_ledger_row(self, cr_id: str, story_id: str, source_ref: str) -> dict[str, Any]:
        """台账「变更记录」行。字段名与多维表格列名对齐（写回前按实际列名校一次）。"""
        return {
            "CR编号": cr_id,
            "StoryID": story_id,
            "变更标题": self.title,
            "变更类型": self.change_type,
            "影响模块": "、".join(self.modules),
            "合规影响": self.compliance,
            "变更前后": f"{self.current_text} → {self.suggested_text}",
            "提出人": self.speaker,
            "来源": f"{self.scenario} · {source_ref}" if source_ref else self.scenario,
            "置信度": round(self.confidence, 2),
            "决策状态": "pending",
        }


@dataclass
class WatchResult:
    """一次监听窗口的产出（供卡片渲染、TaskRecord 观测与单测断言）。"""

    story_id: str = ""
    chat_id: str = ""
    source_ref: str = ""
    candidates: int = 0      # LLM 返回的原始条数
    changes: list[ChangePoint] = field(default_factory=list)
    dropped: int = 0         # 低于 min 被丢的条数（调阈值全靠这个数）
    flagged: int = 0
    unanchored: int = 0      # 定位失败降级整篇的条数
    unattributed: int = 0    # 多 PRD 模式下归属不在候选集里被丢的条数（错位拦截数）
    duplicates: int = 0      # 归一/去重掉的条数（批内近义合并 + 跨轮重复）
    already_commented: int = 0  # 文档里已有等价评论 → 没重复写（跨进程判重）
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        base = (f"候选 {self.candidates} / 出评论 {len(self.changes)}"
                f"（⚠️待确认 {self.flagged}，未定位 {self.unanchored}，丢弃 {self.dropped}）")
        if self.unattributed:
            base += f"，无归属丢弃 {self.unattributed}"
        if self.duplicates:
            base += f"，归一/去重 {self.duplicates}"
        if self.already_commented:
            base += f"，已有评论跳过 {self.already_commented}"
        return base
