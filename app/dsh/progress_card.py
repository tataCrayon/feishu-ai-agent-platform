"""进度卡渲染 — 一轮处理的「过程 + 答案」压在同一张卡里。

## 为什么答案进卡片

用户明确要求「只发一条消息」。进度卡收尾时会原地刷成完成态，如果答案另发一条
文本，会话里就是「卡片 + 文本」两条；把答案写进卡片正文，语音/通知都只有一次，
过程与结论也不会因为刷新顺序错位。

代价：卡片正文不可选中复制（文本消息可以），而且飞书卡片 markdown 不支持
GFM 表格语法——直接塞管道表格会渲染成原始竖线。所以答案里的表格要拆成原生
`table` 组件（`_answer_elements`），代码块与列表仍走 markdown。

## 头部给结论

`AgentMate` 这种空壳标题占掉了卡片唯一必然被看到的位置。改成答案首句
（截断到一行），用户一眼就知道这轮查到了什么；没有答案时退回用户问题。
"""

from __future__ import annotations

import re
from typing import Any

from app.dsh.timeline import Timeline

# 头部标题上限：超了飞书会把标题折行，反而看不清。
_TITLE_LIMIT = 28
# 答案超过这个长度就不往卡片里塞：卡片 JSON 有体积上限，塞不下会被整卡拒绝，
# 那时连过程也一起没了。实测中文答复约 9k 字时整卡 28KB，取 8000 留余量
# （p90 答复 1578 字、历史最长 4116 字，正常答复都能走卡片）。
_MAX_ANSWER_CHARS = 8000
# 折叠面板里过程正文的预算，与 Timeline 的裁剪口径保持一致。
_PANEL_LIMIT = 5200
# 运行态的过程上限：只值「此刻」——最近 3 块、每块思考 200 字、整段 700 字。
# 整轮的过程留给完成态（面板收起），运行中铺满一屏推理只会挤掉「还在动」这个信号。
_RUNNING_BLOCKS = 3
_RUNNING_THOUGHT_CHARS = 200
_RUNNING_BUDGET = 700
# 完成态的折叠面板只在真有多步可回看时才出现：1 步的「处理过程」既不解释
# 任何东西，又占掉答案前面的位置。
_DONE_MIN_STEPS = 2

_TITLE_NOISE = re.compile(r"[#>*`_~\[\]]+")

# 耗时行的预估刻度（秒）：本轮跑到第几个刻度，就用第几档「同类任务的历史耗时」。
# 刻度不是拍脑袋，取自 logs/tasks 的实测分布（dsh 链路 n=36）：
# 一次工具都没调就答完的多是闲聊/单查，p50≈5s；一旦开始连续调工具，
# 单步模型思考 + 工具往返就把中位数推到 120s 以上。
_ETA_LADDER_SEC = (15.0, 60.0, 180.0, 300.0)


def _eta_text(elapsed: float, step_count: int) -> str:
    """预估总耗时的一句话。

    只按「已经走到哪一格」给区间，不拟合精确曲线：进度卡上的数字一旦报得很准，
    用户就会拿它当承诺；而这台机器上的耗时主要由模型思考 + 外部接口决定，
    方差不小。档位随步骤上移、且**只升不降**，至少不会出现「预估越来越短」的错觉。
    """
    idx = min(max(step_count - 1, 0), len(_ETA_LADDER_SEC) - 1)
    total = max(_ETA_LADDER_SEC[idx], elapsed)
    if elapsed >= total:
        return f"预估 ≥ {int(_ETA_LADDER_SEC[idx])}s（已超，仍在处理）"
    return f"预估约 {int(total)}s（还剩约 {int(total - elapsed)}s）"


def build_running(ask: str, timeline: Timeline, elapsed: float) -> dict[str, Any]:
    """运行态：只露出**此刻**在想什么、在调什么，末尾一行耗时。

    运行中的卡片不是「过程回放」：整轮积累的推理铺在展开面板里，用户得到的是
    一屏读不完的英文推理，还挤掉了重点——他只需要知道机器人还在动、动到哪一步。
    历史步骤留给完成态（面板收起、按需展开），这里只留最近几块。
    """
    from lark_channel import CardBuilder

    b = CardBuilder()
    b.header(f"⏳ 正在处理：{_clip(ask, _TITLE_LIMIT)}", template="blue")
    body = timeline.to_markdown(
        max_blocks=_RUNNING_BLOCKS,
        thought_chars=_RUNNING_THOUGHT_CHARS,
        budget=_RUNNING_BUDGET,
    )
    b.raw(_panel("处理过程", body or "正在思考…", expanded=True))
    b.markdown(
        f"<font color='grey'>已处理 {elapsed:.0f}s · "
        f"{_eta_text(elapsed, timeline.step_count)}</font>"
    )
    return b.to_dict()


def build_done(
    ask: str,
    timeline: Timeline,
    answer: str,
    task_id: str = "",
) -> dict[str, Any]:
    """完成态：过程面板收起，答案直接跟在下面；反馈按钮同卡。"""
    from lark_channel import CardBuilder

    b = CardBuilder()
    b.header(f"✅ {_title_of(answer, ask)}", template="green")
    body = timeline.to_markdown()
    if body and timeline.step_count >= _DONE_MIN_STEPS:
        b.raw(_panel(f"处理过程 · {timeline.step_count} 步", body, expanded=False))
    for element in _answer_elements(answer):
        b.raw(element)
    if task_id:
        b.raw(_feedback_row(task_id))
    return b.to_dict()


def answer_fits_card(answer: str) -> bool:
    """答案能否安全地放进卡片正文（见 `_MAX_ANSWER_CHARS` 的理由）。"""
    return len(answer or "") <= _MAX_ANSWER_CHARS


# ── 内部 ──


def _panel(title: str, body: str, *, expanded: bool) -> dict[str, Any]:
    if len(body) > _PANEL_LIMIT:
        body = "…\n\n" + body[-_PANEL_LIMIT:]
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "6px"},
        "vertical_spacing": "8px",
        "padding": "8px 12px 8px 12px",
        "elements": [{"tag": "markdown", "content": body}],
    }


def _feedback_row(task_id: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "margin": "8px 0px 0px 0px",
        "columns": [
            {"tag": "column", "width": "auto", "elements": [
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "🙂 有帮助"},
                 "value": {"fb": "up", "fb_task": task_id}},
            ]},
            {"tag": "column", "width": "auto", "elements": [
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "🙃 不太行"},
                 "value": {"fb": "down", "fb_task": task_id}},
            ]},
        ],
    }


def _answer_elements(answer: str) -> list[dict[str, Any]]:
    """答案 → 卡片元素序列：GFM 表格转原生 table，其余仍是 markdown。"""
    text = (answer or "").strip()
    if not text:
        return []
    elements: list[dict[str, Any]] = []
    chunk: list[str] = []

    def flush() -> None:
        body = "\n".join(chunk).strip()
        chunk.clear()
        if body:
            elements.append({"tag": "markdown", "content": body})

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        table = _parse_table(lines, index)
        if table is None:
            chunk.append(lines[index])
            index += 1
            continue
        flush()
        headers, rows, index = table
        elements.append({
            "tag": "table",
            "page_size": 10,
            "columns": [{"name": f"col_{i}", "display_name": h, "data_type": "lark_md"}
                        for i, h in enumerate(headers)],
            "rows": [{f"col_{i}": cell for i, cell in enumerate(row)} for row in rows],
        })
    flush()
    return elements


def _parse_table(lines: list[str], start: int) -> tuple[list[str], list[list[str]], int] | None:
    """从 `start` 试着读一张 GFM 表格；不是表格返回 None。"""
    if start + 1 >= len(lines):
        return None
    head = lines[start].strip()
    sep = lines[start + 1].strip()
    if not (head.startswith("|") and head.endswith("|")):
        return None
    if not re.fullmatch(r"\|[\s:|-]+\|", sep):
        return None
    headers = _cells(head)
    if not all(headers):
        return None
    rows: list[list[str]] = []
    index = start + 2
    while index < len(lines):
        line = lines[index].strip()
        if not (line.startswith("|") and line.endswith("|")):
            break
        rows.append(_cells(line))
        index += 1
    return headers, rows, index


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _title_of(answer: str, ask: str) -> str:
    """头部标题：答案首句优先，其次用户问题。

    首句只取到第一个句末标点为止：答案常以「查了下当前状态，如实说：」这种
    引导句开头，整句搬进标题就成了半截话——用户读到的标题比没有标题更糟。
    """
    for line in (answer or "").splitlines():
        clean = _TITLE_NOISE.sub("", line).strip()
        if not clean:
            continue
        head = re.split(r"[。！？!?]", clean, maxsplit=1)[0].strip(" :：，,、-—")
        return _clip(head or clean, _TITLE_LIMIT)
    return _clip(_TITLE_NOISE.sub("", ask or "").strip(), _TITLE_LIMIT) or "已处理"


def _clip(text: str, limit: int) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


__all__ = ["answer_fits_card", "build_done", "build_running"]
