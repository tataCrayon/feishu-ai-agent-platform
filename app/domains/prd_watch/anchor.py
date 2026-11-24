"""easyprd 段落锚点 — 把识别到的 quote_text 对到 PRD 的块 ID。

定位优先级（先服务端筛，再本地兜）：
1. `docs +fetch --scope keyword` 让飞书按关键词返回命中块（一次请求，最准）
2. 拿不到就整篇取块，本地按「包含 / 归一化后包含」找

找不到不是错误：新增条款本来就没有对应段落，降级成整篇评论并在文案里声明
（设计稿 §3.3 决策 4：静默丢弃等于漏变更）。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Protocol

from app.protocols.lark_cli import LarkCli, LarkCliError

logger = logging.getLogger("prd_watch.anchor")

# 归一化掉空白与中文标点差异：讨论里引用常带全角/半角混用
_PUNCT_RE = re.compile(r"[\s，,。.、；;：:（）()【】\[\]\"'“”‘’\-—_]+")


_DEL_RE = re.compile(r"<del>(.*?)</del>", re.S)


def normalize(text: str) -> str:
    """比对用的归一化：解 HTML 实体 + 去空白/中文标点。

    为什么必须解实体：飞书 DocxXML 里 `>` 存成 `&gt;`、`&` 存成 `&amp;`，
    于是正文「待受理-> 待成交」在块文本里是「待受理-&gt; 待成交」。只去标点会
    剩下一个 `gt`，两边永远不相等，跨符号的引用（箭头、比较符、`A/B`）整条锚空。
    """
    return _PUNCT_RE.sub("", html.unescape(str(text or "")))


def strip_deleted(xml_text: str) -> str:
    """去掉删除线内容（`<del>…</del>`）。

    飞书 docx 的删除线是 **XML 标签**而不是 markdown 的 `~~`（实测：markdown 导出
    才变成 `~~`，XML 导出是 `<del>`）。不剥掉的话，已划掉的条款会和现行条款一起
    进识别——模型会对着一段"已作废"的规则提变更建议。
    """
    return _DEL_RE.sub("", str(xml_text or ""))


def struck_segments(xml_text: str) -> list[str]:
    """删除线片段（纯文本，已剥内层标签）。"""
    out: list[str] = []
    for m in _DEL_RE.finditer(str(xml_text or "")):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        text = " ".join(text.split())
        if text:
            out.append(text)
    return out


# 飞书文档块：带 id 的任意顶层元素。要区分标题（h1~h6）才能算出「这条原文属于哪一节」
_BLOCK_WITH_TAG_RE = re.compile(r"<(?P<tag>[a-zA-Z0-9_]+)\b[^>]*\bid=\"(?P<id>[^\"]+)\"[^>]*>(?P<text>.*?)</(?P=tag)>", re.S)
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "heading1", "heading2", "heading3"}


class BlockReader(Protocol):
    """只声明锚点需要的读能力（LarkCli 与单测桩都满足它）。

    为什么不用 `lark: LarkCli | None`：那样单测要造假 LarkCli 就得绕类型，
    而这里真正依赖的只有 fetch_block_ids 一个方法。
    """

    async def fetch_block_ids(self, doc: str, *, keyword: str = "") -> list[dict[str, str]]: ...

    async def fetch_blocks(self, doc: str) -> list[dict[str, str]]: ...


class BlockAnchor:
    """一个文档的块索引（同一窗口内多条变更共用，避免每轮重新下载全文）。"""

    def __init__(self, lark: BlockReader | None = None):
        self._lark: BlockReader = lark or LarkCli()
        self._cache: dict[str, list[dict[str, str]]] = {}
        self._tag_cache: dict[str, list[dict[str, str]]] = {}

    async def locate(self, doc: str, quote: str) -> str:
        """返回块 ID；定位不到返回空串（调用方据此走整篇评论）。

        `quote` 允许带残留标记：执行面在卡片参数里照抄 PRD 原文，飞书正文里的
        反斜杠转义（`\\rightarrow`）、行内代码反引号常被一起抄进来，归一化不等价
        就整条锚空。下面三级逐级放宽，不改变「归一化后必须包含」这条硬约束。
        """
        needle = normalize(quote)
        if not doc or not needle:
            return ""
        # 0) 去掉行内代码反引号再归一化：`待成交` 与 待成交 是同一处原文
        bare = normalize(str(quote).replace("`", ""))
        forms = [n for n in dict.fromkeys((needle, bare)) if n]

        # 1) 服务端关键词筛：前 24 字，太长会筛空
        probe = "".join(str(quote).replace("`", "").split())[:24]
        try:
            hits = await self._lark.fetch_block_ids(doc, keyword=probe)
            for h in hits:
                text = normalize(h.get("text", ""))
                if any(n in text for n in forms):
                    return h["block_id"]
            # 1b) 关键词整段没命中时退到**中段**再筛一次：
            #     跨行引用的片段（表格单元格、被软换行切开的句子）开头 24 字
            #     常落在别的块里，服务端按前缀筛会回空，而关键词命中为空时
            #     fetch_block_ids 会退回全文（84 块），本地长文本匹配就白跑了。
            rest = str(quote).replace("`", "").strip()[len(probe) // 2:]
            mid = (rest or "")[:24]
            if mid and mid != probe:
                for h in await self._lark.fetch_block_ids(doc, keyword=mid):
                    text = normalize(h.get("text", ""))
                    if any(n in text for n in forms):
                        return h["block_id"]
            # 1c) 长片段兜底：截一半再筛。`quote` 跨块（截断引用）时整段永远不包含，
            #     用前半段做一个"软锚"——命中文本里包含这个半段就算这处原文。
            half = "".join(str(quote).replace("`", "").split())[: max(12, len(probe) // 2)]
            for candidate in dict.fromkeys(x for x in (half, needle[:14], bare[:14]) if x):
                for h in await self._lark.fetch_block_ids(doc, keyword=candidate):
                    if candidate in normalize(h.get("text", "")):
                        return h["block_id"]
        except LarkCliError as e:
            logger.debug("关键词定位失败（转全文兜底）: %s", e)

        # 2) 本地兜底（见 _locate_local）
        return await self._locate_local(doc, quote)

    async def locate_in_doc(self, doc: str, quote: str) -> tuple[str, str]:
        """→ (块 ID, 块文本)。卡片要「章节」和「原文链接」，回填块文本最省一次请求。"""
        block_id = await self.locate(doc, quote)
        if not block_id:
            return "", ""
        for b in await self._all_blocks(doc):
            if b["block_id"] == block_id:
                return block_id, b.get("text", "")
        return block_id, ""

    async def section_of(self, doc: str, block_id: str) -> str:
        """块所在的章节标题（最近的上级 h1~h6），如「二、产品规则 > 2.3 可见性」。

        实测必要性：不同模型的输出稳定度不同，光有「原文」看不出这条改的是文档哪一块；
        用户在卡片上要「变更项写明原文所在章节」。取不到就返回空串，渲染层不写。
        """
        if not doc or not block_id:
            return ""
        blocks = await self._blocks_with_tags(doc)
        path: list[tuple[int, str]] = []      # (标题级别, 标题文本)
        for b in blocks:
            if b["block_id"] == block_id:
                named = [(lv, t) for lv, t in path
                         if not (lv == 1 and len(path) > 1)]   # h1 多是文档名，已在 PRD 列显示
                named = named or path
                return " > ".join(t for _, t in named[-2:])
            if b.get("tag") in _HEADING_TAGS and b.get("text"):
                level = _heading_level(b["tag"])
                # 只保留两级：完整层级在长文档里会把卡片撑爆。
                # 按**级别**裁剪（新的 h2 顶掉前面的 h2/h3，但不顶掉 h1）
                path = [(lv, t) for lv, t in path if lv < level]
                path.append((level, b["text"][:24]))
        return ""

    async def _locate_local(self, doc: str, quote: str) -> str:
        """本地匹配：逐级放松，仍取不到就返回空串。

        三级策略（实测收紧版本的必要性：原先只做"包含"时，长段落型 PRD
        ——每个块几百字——一条都锚不上，同一场会 5 篇里有一篇 0/5）：
        1. 归一化后双向包含（原行为，最准）
        2. 片段与块的最长公共子串够长（≥12 个字）：口语转述与原文措辞不完全一致时命中
        3. 双向 token 覆盖率达标：短 quote 用"块覆盖 quote"，长 quote 用"quote 覆盖块"
        """
        needle = normalize(quote)
        blocks = await self._all_blocks(doc)

        best, best_len = "", 0
        for b in blocks:
            text = normalize(b.get("text", ""))
            if needle and (needle in text or text in needle) and len(text) > best_len:
                best, best_len = b["block_id"], len(text)
        if best:
            return best

        # 2) 最长公共子串
        best, best_run = "", 0
        for b in blocks:
            run = _longest_common_run(needle, normalize(b.get("text", "")))
            if run > best_run:
                best, best_run = b["block_id"], run
        if best_run >= 12:
            return best

        # 3) token 覆盖率（中文按二元组切，粒度≈"关键词"）
        best, best_ratio = "", 0.0
        for b in blocks:
            ratio = _bigram_coverage(needle, normalize(b.get("text", "")))
            if ratio > best_ratio:
                best, best_ratio = b["block_id"], ratio
        return best if best_ratio >= 0.55 else ""

    async def excerpt(self, doc: str, max_chars: int = 4000) -> str:
        """给识别器当「PRD 原文节选」的纯文本（块文本拼接）。"""
        blocks = await self._all_blocks(doc)
        text = "\n".join(b.get("text", "") for b in blocks if b.get("text"))
        return text[:max_chars]

    async def excerpt_for(self, doc: str, discussion: str,
                          max_chars: int = 4000) -> str:
        """按讨论关键词**挑块**给识别器，而不是无脑取前 N 字。

        原因：长 PRD 前 4000 字往往只是背景与名词解释，模型看到
        的与原讨论相关的条款可能在文档后半段；每次给的地盘都可能不同 → 归属与条数
        跟着抖。按"讨论里出现过的二元组"给相关块排序，命中块前后各带 1 块上下文，
        再按**文档原顺序**拼回——模型看到的是同一批相关条款，输出才稳定。
        讨论为空或没有任何命中时退回前 N 字（原行为）。
        """
        blocks = await self._all_blocks(doc)
        if not blocks:
            return ""
        probe = normalize(discussion)
        scores = [_coverage(_bigrams(probe), _bigrams(normalize(b.get("text", ""))))
                  for b in blocks] if probe else [0.0] * len(blocks)
        # 相对阈值：取最高分的 40% 以上（二分位附近），避免长文档里"处处都像相关"
        # 导致节选退化成全文前 N 字——那正是每次给模型的上下文都不一样、输出漂移的根因。
        top = max(scores) if scores else 0.0
        cut = max(0.15, top * 0.4)
        picked: set[int] = set()
        for i, s in enumerate(scores):
            if s >= cut:
                picked.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(blocks))
        if not picked:
            return "\n".join(b.get("text", "") for b in blocks)[:max_chars]
        # 预算按**相关度**优先分配（同分再按文档顺序），最后按文档顺序拼回：
        # 先按顺序填会让前面无关的块吃光预算，最相关那段反而被截掉。
        ranked = sorted(picked, key=lambda i: (-scores[i], i))
        chosen: set[int] = set()
        used = 0
        for i in ranked:
            text = blocks[i].get("text", "")
            if not text:
                continue
            if used + len(text) > max_chars:
                continue                    # 这块放不下就试下一块，不直接停
            chosen.add(i)
            used += len(text)
        if not chosen:
            return blocks[ranked[0]].get("text", "")[:max_chars]
        return "\n".join(blocks[i].get("text", "") for i in sorted(chosen))

    async def locate_by_terms(self, doc: str, terms: list[str],
                              min_ratio: float = 0.5) -> str:
        """按**一组关键词**在全文块里找最优块（建议表述没有逐字原文时用）。

        与 `locate` 的区别：`locate` 要求"整段被包含"，而模型写的建议表述几乎从不在
        文档里逐字出现；这里只要求"这块命中足够多的关键词"。

        判据用**字符集合覆盖率**而不是"整词包含"：关键词是从建议表述里滑窗切出来的，
        文档里必然只对上其中一部分（实测「点击「清空篩／空篩選」时，／时，除重置全…」
        只有第一个片段能整词命中）。按字符覆盖率算，同一块能命中 3~4 个片段，
        而无关块几乎为 0。

        阈值 `min_ratio=0.5`：一块要覆盖掉一半关键词才算候选。一个都不达标就返回空串
        —— 宁可降级成整篇评论，也不硬凑一个不相关的块。
        """
        keys = [k for k in (normalize(t) for t in terms) if k]
        if not doc or not keys:
            return ""
        blocks = await self._all_blocks(doc)
        best_id, best_ratio, best_len = "", 0.0, 0
        for b in blocks:
            text = normalize(b.get("text", ""))
            if not text:
                continue
            chars = set(text)
            ratio = sum(1 for k in keys if len(set(k) & chars) / len(set(k)) >= 0.6) / len(keys)
            if ratio < min_ratio:
                continue
            if ratio > best_ratio or (ratio == best_ratio and len(text) < best_len):
                best_id, best_ratio, best_len = b["block_id"], ratio, len(text)
        return best_id

    async def title(self, doc: str) -> str:
        """文档标题（第一块）。卡片 PRD 列用它当链接文本，比 Story 标题更准。"""
        blocks = await self._all_blocks(doc)
        return blocks[0].get("text", "").strip() if blocks else ""

    async def _all_blocks(self, doc: str) -> list[dict[str, str]]:
        if doc not in self._cache:
            try:
                self._cache[doc] = await self._lark.fetch_block_ids(doc)
            except LarkCliError as e:
                logger.warning("PRD 块读取失败 %s: %s", doc, e)
                self._cache[doc] = []
        return self._cache[doc]

    async def _blocks_with_tags(self, doc: str) -> list[dict[str, str]]:
        """带元素类型（h1/h2/p…）的块列表，章节归属用。取不到返回空列表。

        单独一份缓存：`fetch_blocks` 只读一次全文，标签信息**只服务于章节显示**，
        不该污染主锚点路径（那条路径已经被单测钉住）。
        """
        if doc not in self._tag_cache:
            try:
                self._tag_cache[doc] = await self._lark.fetch_blocks(doc)
            except Exception as e:
                logger.info("块标签读取失败 %s: %s", doc[:12], str(e)[:80])
                self._tag_cache[doc] = []
        return self._tag_cache[doc]

    def stats(self) -> dict[str, Any]:
        return {"docs_cached": len(self._cache),
                "blocks": sum(len(v) for v in self._cache.values())}


def _heading_level(tag: str) -> int:
    digits = "".join(ch for ch in tag if ch.isdigit())
    return int(digits) if digits else 2


def _longest_common_run(a: str, b: str) -> int:
    """最长公共连续子串长度（a 是待找片段，通常 ≤60 字，DP 足够）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else {text}


def _coverage(needle: str | set[str], block: str | set[str]) -> float:
    """双向覆盖率取小值：短 quote 要看"它是否被块覆盖"，长 quote 要看"块是否被它覆盖"。

    接受字符串（旧调用）或已切好的二元组集合（挑节选时批量算，避免重复切分）。
    """
    n = _bigrams(needle) if isinstance(needle, str) else needle
    b = _bigrams(block) if isinstance(block, str) else block
    if not n or not b:
        return 0.0
    return min(len(n & b) / len(n), len(n & b) / len(b))


def _bigram_coverage(needle: str, block: str) -> float:
    return _coverage(_bigrams(needle), _bigrams(block))
