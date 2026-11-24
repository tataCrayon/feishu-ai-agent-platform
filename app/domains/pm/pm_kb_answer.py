"""知识库问答 —— 检索材料，让模型基于材料作答。

职责只有一段：
    问题 → pm_kb_index 取块 → 拼 prompt → 模型回答 → 附上出处。

刻意不做的事：
    不编造。材料里没有的，就回答「知识库里没写」，并把缺什么说清楚 ——
    这个机器人是给 PM 做交付判断用的，一个编出来的「流程是三步」比不回答更坏。
    不落盘、不缓存答案。知识库随时在改，缓存答案等于让旧结论长期生效。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

from app.domains.pm import pm_kb_index as kb
from app.domains.pm.pm_intent_router import call_llm_text

logger = logging.getLogger("pm_hub.kb_answer")

TOP_K = 4

# 知识库回答要组织步骤和出处，输出比单轮问答长得多，30s 会稳定超时；
# 网关抖动时再补一次，否则他问一句「提测准入要准备什么」只能拿到文件列表。
KB_TIMEOUT_SEC = 90

SYSTEM_PROMPT = (
    "你是示例科技项目部的知识库助手，服务对象是 PMO 李四。\n"
    "你只能依据给定的材料回答，不得使用材料之外的知识补充细节。\n"
    "风格：直接、业务语言、可执行。不说客套话。"
)

_USER_TEMPLATE = """以下是项目部知识库里检索到的材料。

{materials}

问题：{question}

回答要求：
1. 先用 1~3 句给结论，要能直接照做。
2. 需要步骤就列 3~6 条；责任人、闸门编号（G1~G7）、时效这些只在材料里写了才写。
3. 正文里用 [1][2] 标注每条信息来自哪份材料。
4. 材料没覆盖的部分，直接写「知识库里没写这块」，不要用常识补齐。
5. 纯文本输出，不要用 markdown 加粗、表格、代码块（飞书文本消息不渲染）。"""


def _mtime(source: str) -> float:
    try:
        return os.path.getmtime(os.path.join(kb.PROJECT_ROOT, source))
    except OSError:
        return 0.0


_VERSION = re.compile(r"[Vv](\d+)\.(\d+)")


def _rank(source: str) -> tuple:
    """同小节多份副本里谁更权威：先看文件名版本号，再看修改时间。

    只按 mtime 会选错 —— V2.2 被改过一次就比 V2.3 新，但现行流程是 V2.3。
    版本号是语义上的权威，mtime 只在同版本之间做兜底。
    """
    m = _VERSION.search(os.path.basename(source))
    version = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return (version, _mtime(source))


def _dedupe(hits: List[kb.Hit]) -> List[kb.Hit]:
    """同一小节只留一份。

    SOP 有 V2.2 / V2.3 / _v22_check 三份副本，同一节在两三份里都命中，
    材料里就出现三段几乎一样的话，模型于是写成「[1][2][3][4]」——
    看着像交叉验证，其实是一份文档的三个备份。

    去重键取小节名的最后一级（「四、研发实施阶段」）。不比较正文，
    因为三份副本的正文是逐字微调过的，比正文等于没去重。
    保留修改时间最新的那份，也就是 V2.3。
    """
    best: Dict[str, kb.Hit] = {}
    order: List[str] = []

    for h in hits:
        key = h.chunk.heading.split(" > ")[-1]
        old = best.get(key)
        if old is None:
            best[key] = h
            order.append(key)
        elif _rank(h.chunk.source) > _rank(old.chunk.source):
            best[key] = h

    return [best[k] for k in order]


def _format_materials(hits: List[kb.Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        blocks.append(
            f"【材料 {i}】{h.chunk.source}\n"
            f"小节：{h.chunk.heading}\n"
            f"{h.chunk.text}"
        )
    return "\n\n".join(blocks)


def answer(question: str, k: int = TOP_K) -> Dict[str, Any]:
    """返回 {ok, answer, sources, reason}。调用方据此决定是否降级话术。"""
    q = (question or "").strip()
    if not q:
        return {"ok": False, "reason": "empty", "answer": "", "sources": []}

    hits = kb.search(q, k=k)
    if not hits:
        return {"ok": False, "reason": "no_hit", "answer": "", "sources": []}

    hits = _dedupe(hits)

    prompt = _USER_TEMPLATE.format(materials=_format_materials(hits), question=q)
    text = call_llm_text(SYSTEM_PROMPT, prompt, timeout=KB_TIMEOUT_SEC)
    if not text:
        text = call_llm_text(SYSTEM_PROMPT, prompt, timeout=KB_TIMEOUT_SEC)
    if not text:
        return {"ok": False, "reason": "llm_failed", "answer": "", "sources": []}

    # 出处由代码补，不指望模型自觉写 —— 它偶尔会漏，而漏一次就等于答案没法核。
    sources: List[str] = []
    for h in hits:
        cite = h.cite()
        if cite not in sources:
            sources.append(cite)

    return {
        "ok": True,
        "reason": "",
        "answer": text.strip(),
        "sources": sources[:3],
    }


def render(question: str) -> str:
    """给飞书私聊用的成品文案。检索空 / 模型挂了都在这里说清楚。"""
    res = answer(question)

    if res["ok"]:
        body = res["answer"]
        cites = "\n".join(f"· {s}" for s in res["sources"])
        return f"{body}\n\n📎 依据\n{cites}"

    if res["reason"] == "no_hit":
        # 无命中不编。顺带告诉他库里现在有什么，避免他以为机器人坏了。
        names = sorted({c.source.split("/")[-1] for c in kb.get_index().chunks})
        sample = "、".join(names[:6])
        return (
            "📚 知识库里没有匹配的内容。\n"
            f"现在库里有：{sample}{' 等' if len(names) > 6 else ''}。\n"
            "换个说法，或者告诉我该补哪一块 —— 这块内容库里确实是空的。"
        )

    if res["reason"] == "llm_failed":
        hits = kb.search(question, k=3)
        cites = "\n".join(f"· {h.cite()}" for h in hits)
        return (
            "模型这会儿连不上，先把命中的原文给你：\n\n"
            f"{cites}\n\n稍后再问一次，我再整理成结论。"
        )

    return "问点具体的，比如「提测准入要准备什么」「G5 谁签字」。"