"""知识库索引 —— 切块 + 中文友好检索。

职责：给定一个问题，返回最相关的若干「块」（文件 + 标题路径 + 原文）。
不做回答，回答在 pm_kb_answer。也不写文件，知识库只读。

为什么要另起一份，不用 pm_knowledge_hub.search_knowledge_base
------------------------------------------------------------
那个实现按空格切词。中文提问没有空格，整串变成一个 token：

    "发版流程"  → 0 篇命中（文档里没有连续这四个字）
    "发版 流程" → 5 篇命中（手打空格才行）

用户不会为了让机器人搜到而空格断词。这里改成字符 bigram + BM25：
「发版流程」切成 发版/版流/流程，其中「版流」是噪声但 IDF 会把它压到接近 0，
「发版」「流程」负责拉分。不需要分词器，也不引额外依赖。

覆盖范围：knowledge-base/（L1~L3）、SOP/、people-map/。
daily-log 与 inbox 不在这里 —— 它们是流水，走 search_notes。
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# handlers → agents → app → feishu-agent-platform → project
PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
SOURCE_DIRS = ("knowledge-base", "SOP", "people-map")

# 单块上限。SOP 那种 30KB 的大文件必须切开，否则一次检索把整篇塞进 prompt，
# 既贵又稀释重点；600~700 字恰好是「一个概念讲完」的粒度。
MAX_CHUNK_CHARS = 700

# 索引在进程内缓存。知识库是人工维护的，5 分钟内不会变；
# 每次检索都重读 20 个文件属于没必要的 I/O。
CACHE_TTL_SEC = 300

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[a-zA-Z0-9]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# BM25 参数：语料是小规模中文文档，b 取 0.75 是常规起点，
# 块长度差异大（表格块短、流程块长），需要长度归一。
_K1 = 1.5
_B = 0.75


@dataclass(frozen=True)
class Chunk:
    source: str   # 相对 project 的路径，如 "SOP/私募研发流程SOP-V2.3.md"
    heading: str  # 标题路径，如 "私募研发流程 > G5 提测准入"
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float

    def cite(self) -> str:
        return f"{self.chunk.source} > {self.chunk.heading}"


# ══════════════════════════════════════════════════════════════════════
# 切块
# ══════════════════════════════════════════════════════════════════════

def _pack(body: str, limit: int = MAX_CHUNK_CHARS) -> List[str]:
    """按空行分段，再把段落拼到接近 limit。超长段落硬切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    out: List[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 2 <= limit:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            out.append(cur)
            cur = ""
        while len(p) > limit:
            out.append(p[:limit])
            p = p[limit:]
        cur = p
    if cur:
        out.append(cur)
    return out


def _chunks_of_text(text: str, source: str) -> List[Chunk]:
    """按 markdown 标题切成 section，section 过长再切块。

    标题栈决定 heading 路径。之所以要保留路径，是因为「G5 提测准入」单独出现
    时说不清属于哪条流程，而「私募研发流程 > G5 提测准入」能直接当出处引用。
    """
    out: List[Chunk] = []
    stack: List[Tuple[int, str]] = []
    buf: List[str] = []
    heading = ""

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        for piece in _pack(body):
            out.append(Chunk(source=source, heading=heading or source, text=piece))

    for line in text.splitlines():
        m = _HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, m.group(2).strip()))
        heading = " > ".join(t for _, t in stack)

    flush()
    return out


def load_chunks(dirs: Tuple[str, ...] = SOURCE_DIRS) -> List[Chunk]:
    chunks: List[Chunk] = []
    for d in dirs:
        root_dir = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(root_dir):
            continue
        for root, _, files in os.walk(root_dir):
            for name in files:
                if not name.endswith(".md"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, PROJECT_ROOT).replace("\\", "/")
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read()
                except Exception:
                    continue
                chunks.extend(_chunks_of_text(text, rel))
    return chunks


# ══════════════════════════════════════════════════════════════════════
# 分词（bigram）
# ══════════════════════════════════════════════════════════════════════

def grams(text: str) -> List[str]:
    """CJK 连续段切 2-gram，拉丁与数字按词。返回允许重复（词频要用）。"""
    out: List[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(w.lower() for w in _LATIN_WORD.findall(text))
    return out


# 口语化提问的噪声。中文没有空格，「怎么处理」在 bigram 下变成 怎么/么处/处理，
# 「哪些闸门」变成 哪些/些闸/闸门 —— 这些片段在语料里普遍出现，
# 不滤掉的话，问「票据上线要走哪些闸门」会优先命中随便哪篇写了「哪些」的文档。
_QUESTION_WORDS = (
    "什么", "怎么", "怎样", "如何", "哪些", "哪个", "哪里", "多少", "多大",
    "是不是", "有没有", "为什么", "怎么办", "请问", "帮我", "帮忙", "告诉",
    "一下", "应该", "需要", "可以", "能不能", "要不要", "我们", "你们",
)

# 只收语气助词。像「要」「会」「有」「一」这些是实词构件
# （要求 / 会议 / 有效 / 统一），删掉会把词切碎，反而制造假 bigram。
_STOP_CHARS = set("的了吗呢吧啊呀哦嗯着呗嘛")

# query 里少于这么多实词 bigram 命中时，视为没找到 —— 单个片段偶然命中不可信。
MIN_GRAM_HITS = 2


def _denoise(text: str) -> str:
    """把疑问壳和语气词换成空格（不是删除）。

    删除会把「准入要准备」拼成「准入准备」，再切出「入准」这种不存在的词；
    换成空格就断开了，bigram 只会在真实的词内部产生。
    """
    for w in _QUESTION_WORDS:
        text = text.replace(w, " ")
    return "".join(" " if ch in _STOP_CHARS else ch for ch in text)


def query_grams(question: str) -> List[str]:
    """问题侧的 bigram：先去噪，再切，最后去重。"""
    return list(dict.fromkeys(grams(_denoise(question or ""))))


class Index:
    """一次构建，反复检索。语料在几万字量级，内存里算完是毫秒级。"""

    def __init__(self, chunks: List[Chunk]):
        self.chunks = chunks
        self.tfs: List[Counter] = []
        self.lengths: List[int] = []
        df: Counter = Counter()

        for c in chunks:
            # 标题权重 3：问「提测」时，标题就叫「G5 提测准入」的块
            # 应该排在正文里偶然提了一句「提测」的块前面。
            tf = Counter(grams(c.heading))
            for g, n in tf.items():
                tf[g] = n * 3
            tf.update(grams(c.text))
            self.tfs.append(tf)
            self.lengths.append(sum(tf.values()))
            for g in tf:
                df[g] += 1

        n = len(chunks)
        self.avg_len = (sum(self.lengths) / n) if n else 0.0
        self.idf: Dict[str, float] = {
            g: math.log((n - d + 0.5) / (d + 0.5) + 1) for g, d in df.items()
        }

    def search(self, question: str, k: int = 4) -> List[Hit]:
        q_grams = query_grams(question)
        if not q_grams or not self.chunks:
            return []

        # 短问题（「发版」「票据」）本身只有一两个 bigram，不能套用多片段门槛。
        need = min(MIN_GRAM_HITS, len(q_grams))

        scored: List[Hit] = []
        for i, tf in enumerate(self.tfs):
            score = 0.0
            hit = 0
            norm = _K1 * (1 - _B + _B * (self.lengths[i] / (self.avg_len or 1)))
            for g in q_grams:
                f = tf.get(g)
                if not f:
                    continue
                hit += 1
                score += self.idf.get(g, 0.0) * (f * (_K1 + 1)) / (f + norm)

            if hit < need or score <= 0:
                continue
            # 除以命中片段数的平方根：长问题不会因为片段多而自动占优，
            # 也让「只蹭到一个高频词」的块排不过「命中多个实词」的块。
            scored.append(Hit(chunk=self.chunks[i], score=score / math.sqrt(hit)))

        scored.sort(key=lambda h: -h.score)
        return scored[:k]


# ══════════════════════════════════════════════════════════════════════
# 对外
# ══════════════════════════════════════════════════════════════════════

_index: Optional[Index] = None
_index_at: float = 0.0


def get_index(force: bool = False) -> Index:
    global _index, _index_at
    now = time.time()
    if force or _index is None or now - _index_at > CACHE_TTL_SEC:
        _index = Index(load_chunks())
        _index_at = now
    return _index


def search(question: str, k: int = 4) -> List[Hit]:
    return get_index().search(question, k)