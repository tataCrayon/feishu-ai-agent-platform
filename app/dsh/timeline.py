"""处理过程时间线 — 把「思考」与「调了哪个能力」按真实到达顺序缝成一段。

## 为什么不是两份清单

旧卡片是两张清单拼在一起：上面一行行工具名（`加载能力说明 ✓`），下面另起一段
思考。用户看到的是「做了什么」和「想了什么」各说各话，读不出「为什么调这一步」。
dsh 的事件流本来就带着时序：thought 帧、tool_call 帧按发生顺序到达，照原样交错
渲染就是这段过程最真实的叙述。

## 合并规则

- thought 帧紧邻上一个 thought 块 → 续写同一块（同一段推理被切成几十帧）
- 中间夹了工具事件 → 自动断开，下一段思考另起一块（推理真的翻页了）
- 工具事件按动作名原地更新：`进行中 → 完成 ✓`，失败保留 ❌ 并带可行动原因

## 容量

思考可能上万字，卡片放不下（飞书卡片有体积上限，用户也读不完）。单块只留尾部
`_MAX_THOUGHT_CHARS` 字（正在推理时最新那几句才是重点），整条时间线只留最近
`_MAX_BLOCKS` 块；完整过程仍在 `logs/turns/` 的轨迹里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 单块思考保留的字符数：够看清一两段推理，又不至于把卡片撑爆。
_MAX_THOUGHT_CHARS = 900
# 时间线保留的块数：一屏能读完的量，再长用户也不会往下翻。
_MAX_BLOCKS = 12
# 整条时间线的字符预算：飞书卡片 JSON 有体积上限，留足余量。
_MAX_TOTAL_CHARS = 5000

_RUNNING = "running"
_DONE = "done"
_FAILED = "failed"


@dataclass(slots=True)
class Block:
    """时间线上的一个片段：一段思考，或一次动作。"""

    kind: str                    # thought | action
    text: str = ""               # 思考正文 / 动作文案
    action: str = ""             # 动作名（同一动作原地更新用）
    status: str = _RUNNING


@dataclass(slots=True)
class Timeline:
    """一轮处理的过程记录。纯内存、纯函数式拼装，不碰 IO。"""

    blocks: list[Block] = field(default_factory=list)

    # ── 写入 ──

    def observe(self, stage: str, detail: str) -> bool:
        """吃一个进度事件；返回是否改变了时间线（供调用方决定是否刷卡）。"""
        if not detail:
            return False
        if stage == "thought":
            self.add_thought(detail)
            return True
        if stage == "tool_start":
            return self._upsert_action(detail, _RUNNING)
        if stage == "tool_done":
            return self._upsert_action(detail, _DONE)
        if stage == "agent_error":
            return self._upsert_action(detail, _FAILED)
        return False

    def add_thought(self, text: str) -> None:
        chunk = (text or "").strip()
        if not chunk:
            return
        if self.blocks and self.blocks[-1].kind == "thought":
            block = self.blocks[-1]
            block.text = f"{block.text}{chunk}" if block.text else chunk
        else:
            self.blocks.append(Block(kind="thought", text=chunk))
        self._trim()

    def _upsert_action(self, detail: str, status: str) -> bool:
        action = (detail or "").strip()
        if not action:
            return False
        # 从尾部往回找同名动作：一次调用有 started/done 两帧，失败重试还有更多帧。
        # 超过窗口的历史行改不动也看不见，回扫到窗口边界即可。
        floor = max(0, len(self.blocks) - _MAX_BLOCKS)
        for index in range(len(self.blocks) - 1, floor - 1, -1):
            block = self.blocks[index]
            if block.kind != "action" or block.action != action:
                continue
            # 失败行留着：用户要知道这一步曾经没成；重试的新一帧另起一块。
            if block.status == _FAILED and status != _FAILED:
                break
            block.status = status
            block.text = action
            return True
        self.blocks.append(Block(kind="action", text=action, action=action, status=status))
        self._trim()
        return True

    def _trim(self) -> None:
        if len(self.blocks) <= _MAX_BLOCKS:
            return
        # 丢最早的一批：最近的推理才是用户在等的。
        del self.blocks[: len(self.blocks) - _MAX_BLOCKS]

    # ── 渲染 ──

    def to_markdown(
        self,
        *,
        max_blocks: int | None = None,
        thought_chars: int | None = None,
        budget: int | None = None,
    ) -> str:
        """整条时间线的 Markdown 文本（空时间线返回空串）。

        超出块预算时**按整块丢**，不从头部砍字符：字符级截断会把最新那段思考的
        开头切掉，让用户读到半句话开头、逻辑接不上的推理——比少一段旧推理更糟。
        每块自己已按尾部裁剪（`_clip_thought`），保留的都是最新内容。

        三个参数是给运行态用的：运行中的卡片只值「此刻在想什么、在调什么」，
        把整轮积累的过程铺开，用户看到的是一屏读不完的推理（见 `progress_card`）。
        """
        limit_blocks = max_blocks or _MAX_BLOCKS
        limit_chars = thought_chars or _MAX_THOUGHT_CHARS
        limit_total = budget or _MAX_TOTAL_CHARS

        visible = list(self.blocks)[-limit_blocks:]
        text = self._render(visible, limit_chars)
        while len(visible) > 1 and len(text) > limit_total:
            visible.pop(0)
            text = self._render(visible, limit_chars)
        return text

    @staticmethod
    def _render(blocks: list[Block], thought_chars: int) -> str:
        lines: list[str] = []
        last_action_index = max(
            (i for i, b in enumerate(blocks) if b.kind == "action"), default=-1
        )
        for index, block in enumerate(blocks):
            if block.kind == "thought":
                lines.append(_clip_thought(block.text, thought_chars))
                continue
            if block.status == _FAILED:
                lines.append(f"❌ {block.text}")
            elif block.status == _RUNNING and index == last_action_index:
                lines.append(f"**🔎 {block.text}**")
            else:
                lines.append(f"<font color='grey'>· {block.text} ✓</font>")
        return "\n\n".join(line for line in lines if line)

    def has_content(self) -> bool:
        return bool(self.blocks)

    @property
    def step_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "action")


def _clip_thought(text: str, limit: int = _MAX_THOUGHT_CHARS) -> str:
    """单块思考超长时保留尾部：正在想的那几句才是重点。"""
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


__all__ = ["Block", "Timeline"]
