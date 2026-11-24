"""需求分析 — 给一条 Story 链接，产出「这个需求现在能看出什么」。

三类产出，**确定性与不确定性分开摆**（这是本模块最要紧的纪律）：

1. **机械自检**（确定性高）：只做代码能证明的检查，不调模型——
   数量与列表长度不符、集合差集（支持链 vs 校验链）、删除线残留、
   术语同义异名、章节覆盖缺口。每条都带**原文块 id**，卡片上能直接跳过去。
2. **同族需求**（确定性中）：按需求名**前缀**（如 `【webportal】VA`）查同一产品线的
   Story。前缀是产品线的人工约定，比让模型读标题猜关系可靠；但只**列候选**，
   不自动建关联（写错关系比漏一条贵）。
3. **会议素材**（确定性低）：按需求名词搜妙记，只报「有没有相关会、能不能读」，
   不推断会里讲了什么。

刻意不做：不调 LLM 编「可优化点」。分析卡片上的每一条都必须能指回 PRD 的一段原文，
否则产品会开始怀疑这个工具（误报比漏报贵）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.domains.prd_watch.anchor import BlockAnchor, normalize

logger = logging.getLogger("prd_watch.analysis")

# Story 链接形态：`/story/detail/7124060677`
_STORY_ID_RE = re.compile(r"/story/detail/(\d+)")
_BARE_ID_RE = re.compile(r"^\d{6,}$")

# 需求名里的产品线前缀：`【webportal】VA - 钱包管理&钱包绑定` → `【webportal】VA`
_PREFIX_RE = re.compile(r"^(【[^】]+】)?\s*([A-Za-z0-9]+)?")
# 版本号形态（V1.1 / v2 / 3.4）
_VERSION_RE = re.compile(r"[Vv](\d+(?:\.\d+)*)")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}


@dataclass
class Finding:
    """一条机械自检结论（每条都必须能指回原文块）。"""

    title: str = ""
    quote: str = ""            # 逐字原文（卡片「当前PRD原文」栏）
    suggestion: str = ""       # 建议表述
    block_id: str = ""
    section: str = ""
    jump_url: str = ""
    kind: str = ""             # 数量/集合/删除线/术语/覆盖
    confidence: float = 0.85

    @property
    def located(self) -> bool:
        return bool(self.block_id)


@dataclass
class StoryDigest:
    """一条 Story 的分析素材（卡片渲染与工具回执共用）。"""

    story_id: str = ""
    title: str = ""
    status: str = ""
    node: str = ""
    url: str = ""
    space_name: str = ""
    priority: str = ""
    business: str = ""
    prd_url: str = ""
    prd_token: str = ""
    prd_title: str = ""
    design_url: str = ""
    design_field: str = ""
    owners: dict[str, str] = field(default_factory=dict)
    related: list[dict[str, str]] = field(default_factory=list)   # 同前缀 + 同迭代候选
    related_basis: str = ""
    # 两条线索分开记，卡片上按组列（同一需求可能同时命中两组，按 story_id 去重）
    prefix_group: str = ""
    sprint_group: str = ""
    meetings: list[dict[str, str]] = field(default_factory=list)
    meeting_errors: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # PRD↔UI 差异（kind="UI差异"）：与机械自检同一份 findings 列表，卡片统一渲染
    ui_checked: bool = False
    ui_errors: list[str] = field(default_factory=list)
    # 设计稿取数问题单独记：与 PRD 读取错误分开，否则「没有设计稿」会被读成
    # 「没有 PRD」，排查方向直接跑偏（实测卡片上就出现过这种误导）。
    design_errors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def people(self) -> str:
        order = ("产品", "需求提出人", "测试", "设计师", "项目")
        bits = [f"{r} {self.owners[r]}" for r in order if self.owners.get(r)]
        rest = [f"{r} {n}" for r, n in self.owners.items() if r not in order]
        return "｜".join(bits + rest)


def parse_story_ref(text: str) -> str:
    """Story 链接 / 裸 ID → Story ID。解析不出返回空串（不猜）。"""
    raw = (text or "").strip()
    m = _STORY_ID_RE.search(raw)
    if m:
        return m.group(1)
    if _BARE_ID_RE.match(raw):
        return raw
    return ""


def name_prefix(title: str) -> str:
    """需求名 → 产品线前缀（用于「同族需求」检索）。

    `【webportal】VA - 钱包管理&钱包绑定` → `【webportal】VA`：
    取【】标签与紧随其后的英文/数字段。取不到就退回【】标签本身。
    """
    m = _PREFIX_RE.match((title or "").strip())
    if not m:
        return ""
    bracket = (m.group(1) or "").strip()
    lead = (m.group(2) or "").strip()
    if bracket and lead:
        return f"{bracket}{lead}"
    return bracket or ""


def version_tokens(text: str) -> list[str]:
    """文本里的版本号（`V1.1` → `1.1`），用于「同版本」检索。"""
    return [f"V{v}" for v in _VERSION_RE.findall(text or "")]


def _cell_text(block: dict[str, str]) -> str:
    return str(block.get("text") or "")


def _section_map(blocks: list[dict[str, str]]) -> dict[str, str]:
    """块 id → 所在章节（与 BlockAnchor.section_of 同口径，但一次遍历算全文）。"""
    out: dict[str, str] = {}
    path: list[tuple[int, str]] = []
    for b in blocks:
        tag = str(b.get("tag") or "")
        text = _cell_text(b).strip()
        if tag.startswith("h") or tag.startswith("heading"):
            digits = "".join(ch for ch in tag if ch.isdigit())
            level = int(digits) if digits else 2
            if text:
                path = [(lv, t) for lv, t in path if lv < level]
                path.append((level, text[:24]))
        named = [(lv, t) for lv, t in path if not (lv == 1 and len(path) > 1)] or path
        out[str(b.get("block_id") or "")] = " > ".join(t for _, t in named[-2:])
    return out


# 术语同义异名：左边是文档里出现的说法，右边是建议统一成的写法。
# 只收实测踩过的对（钱包归属类型/归属方 等）；不追求全，误报比漏报贵。
_TERM_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("钱包归属方", "钱包归属类型"),
    ("使用权", "权限"),
)


def _count_enumerations(text: str) -> list[tuple[int, int]]:
    """找「A、B、C（共 N 个/条/种）」这类枚举，返回 [(声称数, 实际数)]。"""
    out: list[tuple[int, int]] = []
    for m in re.finditer(r"([一-龥A-Za-z0-9、，,\s]{4,200}?)[（(]共\s*(\d+)\s*(?:个|条|种|项)[）)]",
                         text or ""):
        listed = [x for x in re.split(r"[、,，]", m.group(1)) if x.strip()]
        claimed = int(m.group(2))
        # 只统计「像枚举」的片段（每段 ≤ 12 字且不含句号），避免把整段话当列表
        items = [x.strip() for x in listed if 0 < len(x.strip()) <= 12 and "。" not in x]
        if len(items) >= 2:
            out.append((claimed, len(items)))
    return out


def mechanical_findings(blocks: list[dict[str, str]], *, prd_url: str = "") -> list[Finding]:
    """在 PRD 块上做**可证明**的检查，每条带块 id。"""
    out: list[Finding] = []
    sections = _section_map(blocks)
    joined = "\n".join(_cell_text(b) for b in blocks)
    base = (prd_url or "").split("#")[0]

    def _jump(block_id: str) -> str:
        return f"{base}#{block_id}" if base and block_id else ""

    def _first_block(*needles: str) -> dict[str, str] | None:
        for b in blocks:
            text = _cell_text(b)
            if all(n in text for n in needles):
                return b
        return None

    # ① 枚举数量与列表长度不符
    for m in re.finditer(r"[^\n]{0,300}?（共\s*(\d+)\s*[个条种项]）", joined):
        block = _first_block(m.group(0)[:20]) or _first_block(f"共 {m.group(1)}")
        claim = int(m.group(1))
        seg = m.group(0)
        items = [x.strip() for x in re.split(r"[、,，]", seg.split("：")[-1].split("（")[0])
                 if x.strip()]
        items = [x for x in items if 0 < len(x) <= 8]
        if block is not None and len(items) >= 2 and claim != len(items):
            bid = str(block.get("block_id") or "")
            out.append(Finding(
                title=f"枚举数量与列表不符：写「共 {claim} 个」，实列 {len(items)} 个",
                quote=_cell_text(block).strip()[:200],
                suggestion=f"把「共 {claim} 个」改为「共 {len(items)} 个」，或补上缺的那一项",
                block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
                kind="数量", confidence=0.9))

    # ② 集合差集：支持链 vs 地址校验链。
    # 校验链在 `li` 块里（「支持校验主流区块链地址格式：Ethereum、Solana、Tron、Polygon」），
    # 标题块只有「1. 钱包地址格式错误校验」——先找那条 li，找不到再退回标题块。
    chain_block = _first_block("支持的区块链网络") or _first_block("区块链网络")
    check_block = (_first_block("支持校验主流")
                   or _first_block("地址格式校验")
                   or _first_block("地址格式"))
    if chain_block is not None and check_block is not None:
        supported = _chain_set(_cell_text(chain_block))
        checked = _chain_set(_cell_text(check_block))
        extra = sorted(checked - supported)
        missing = sorted(supported - checked)
        if extra or missing:
            bid = str(check_block.get("block_id") or "")
            bits = []
            if extra:
                bits.append("校验清单多出 " + "、".join(extra))
            if missing:
                bits.append("缺少地址校验规则 " + "、".join(missing))
            out.append(Finding(
                title="链集合与地址校验集合不一致",
                quote=_cell_text(check_block).strip()[:200],
                suggestion="；".join(bits) + "；支持链与校验链两处列同一份清单",
                block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
                kind="集合", confidence=0.88))

    # ③ 删除线残留：整段被划掉但仍在原位（编辑残留的典型形态）。
    # 删除线在 docx XML 里是 `<del>` 标签；markdown 导出才是 `~~`，两种都认。
    struck_blocks: list[dict[str, str]] = []
    for b in blocks:
        raw = str(b.get("raw") or "")
        text = _cell_text(b)
        if "<del>" in raw or "~~" in raw or "~~" in text:
            struck_blocks.append(b)
    if struck_blocks:
        # 整块划线 = 整段作废但没删：一条 finding 汇总（逐块报会把卡片刷满）
        heads = [_cell_text(b).strip()[:24] for b in struck_blocks[:4]]
        first = struck_blocks[0]
        bid = str(first.get("block_id") or "")
        out.append(Finding(
            title=f"删除线残留：{len(struck_blocks)} 个段落已划掉但仍留在正文",
            quote=" ／ ".join(h for h in heads if h)[:220],
            suggestion="确认后删除已作废段落（或恢复并说明保留原因）；"
                       "同时检查引用这些段落的现行条款是否已同步",
            block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
            kind="删除线", confidence=0.85))

    # ④ 术语同义异名
    for wrong, right in _TERM_SYNONYMS:
        if wrong in joined and right in joined:
            block = _first_block(wrong)
            if block is None:
                continue
            bid = str(block.get("block_id") or "")
            out.append(Finding(
                title=f"术语不一致：「{wrong}」与「{right}」并存",
                quote=_cell_text(block).strip()[:200],
                suggestion=f"统一为「{right}」，并同步设计稿与表单字段名",
                block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
                kind="术语", confidence=0.85))

    # ⑤ 状态机覆盖：状态流转表的「目标状态」集合 vs 列表页展示的状态集合。
    # 只看**表内**（表格块的 raw 里带 `状态`/`目标状态`），不看全文——
    # 原文别处出现「删除」两个字不等于状态机覆盖了删除路径（那会误报）。
    # 状态流转表是 `table` 块（不是段落），按「目标状态」表头找它
    flow_block = next((b for b in blocks
                       if b.get("tag") == "table" and "目标状态" in _cell_text(b)),
                      None) or _first_block("状态流转")
    if flow_block is not None:
        flow_raw = str(flow_block.get("raw") or _cell_text(flow_block))
        flow_states = _status_set(flow_raw)
        list_block = _first_block("按照钱包状态进行区分") or _first_block("全部、可用")
        list_states = _status_set(_cell_text(list_block) if list_block else joined)
        missing = sorted(list_states - flow_states)
        if missing and flow_states:
            bid = str(flow_block.get("block_id") or "")
            out.append(Finding(
                title=f"状态流转表缺 {len(missing)} 个状态：{'、'.join(missing)}",
                quote=_cell_text(flow_block).strip()[:200],
                suggestion="在状态流转表补齐这些状态：从哪些状态可进入、可执行什么操作、"
                           "离开到哪个状态",
                block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
                kind="覆盖", confidence=0.7))
    return out


def permissiveness_findings(blocks: list[dict[str, str]], *, prd_url: str = "") -> list[Finding]:
    """「放行词」检查 — 规则里出现「无限制/一律/全部/仅」这类绝对表述时提示复核。

    为什么值得单独查：这类词在评审里最容易被忽略，但每条都是一个**未定义边界**
    （「手机号无限制」没说格式、「一律放行」没说风控口径）。它不判断对错，
    只要求产品明确写清边界——所以置信度给 0.7，且标题写成疑问句。
    """
    out: list[Finding] = []
    sections = _section_map(blocks)
    base = (prd_url or "").split("#")[0]
    words = ("无限制", "不限制", "不限", "一律", "全部放行", "无需校验", "不做校验",
             "任意", "随便填", "无校验")
    for block in blocks:
        text = _cell_text(block)
        hit = next((w for w in words if w in text), "")
        if not hit:
            continue
        bid = str(block.get("block_id") or "")
        out.append(Finding(
            title=f"绝对表述「{hit}」需明确边界",
            quote=text.strip()[:200],
            suggestion=f"把「{hit}」替换成可验证的边界：限制类型/长度/范围，或写明"
                       "「不做校验」的后果由谁承担",
            block_id=bid, section=sections.get(bid, ""),
            jump_url=f"{base}#{bid}" if base and bid else "",
            kind="放行词", confidence=0.7))
        if len(out) >= 5:
            break
    return out


def consistency_findings(blocks: list[dict[str, str]], *, prd_url: str = "") -> list[Finding]:
    """跨章节一致性：同一术语在「正文」与「异常场景」两处出现不同枚举值。

    只查一种可证明的形态：同一组枚举（如「可用/待验证/验证中」）在两块里
    集合不同。差异列举给出，不下「哪个对」的结论——口径由产品定。
    """
    out: list[Finding] = []
    sections = _section_map(blocks)
    base = (prd_url or "").split("#")[0]
    # 只在**语义相近**的两块之间比：同一个枚举组必须共享主标识词，
    # 否则「区块链网络：BTC、ETH、TRON、Solana」会和「币种：BTC、ETH、USDT、USDC、
    # TRX、SOL」被判成不一致——那是两个不同维度（链 vs 币），不是冲突（实测误报）。
    keywords = ("状态", "枚举", "类型", "方式", "渠道")
    enums: list[tuple[dict[str, str], set[str]]] = []
    for block in blocks:
        text = _cell_text(block)
        for m in re.finditer(r"([一-龥A-Za-z0-9]{2,10}(?:[、/，,][一-龥A-Za-z0-9]{2,10}){2,})", text):
            items = {x.strip() for x in re.split(r"[、/，,]", m.group(1)) if x.strip()}
            if len(items) >= 3:
                enums.append((block, items))
    used: set[str] = set()
    for i, (b1, s1) in enumerate(enums):
        for b2, s2 in enums[i + 1:]:
            if b1.get("block_id") == b2.get("block_id"):
                continue
            # 同一维度才比：两块都要出现同一个维度词（状态/枚举/类型…）
            ctx1, ctx2 = _cell_text(b1), _cell_text(b2)
            shared = [k for k in keywords if k in ctx1 and k in ctx2]
            if not shared:
                continue
            overlap = s1 & s2
            # 只比「明显是同一个枚举组」的：交集至少 2 项，且两边各有独有项
            if len(overlap) < 2:
                continue
            only1, only2 = s1 - s2, s2 - s1
            if not (only1 and only2):
                continue
            key = str(b1.get("block_id"))
            if key in used:
                continue
            used.add(key)
            bid = str(b1.get("block_id") or "")
            out.append(Finding(
                title=f"同一「{shared[0]}」枚举在两处写法不一致",
                quote=f"此处：{'、'.join(sorted(s1))}"
                      f"\n另一处：{'、'.join(sorted(s2))}",
                suggestion=f"统一枚举：多出「{'、'.join(sorted(only1))}」"
                           f"／缺「{'、'.join(sorted(only2))}」，确认哪份是准的",
                block_id=bid, section=sections.get(bid, ""),
                jump_url=f"{base}#{bid}" if base and bid else "",
                kind="一致性", confidence=0.75))
            break
        if len(out) >= 3:
            break
    return out


_CHAIN_ALIASES = {
    "BTC": ("btc", "bitcoin", "比特币"),
    "ETH": ("eth", "ethereum", "以太坊"),
    "TRON": ("tron", "trx", "波场"),
    "Solana": ("solana", "sol"),
    "Polygon": ("polygon", "matic"),
}


def _chain_set(text: str) -> set[str]:
    low = (text or "").lower()
    return {chain for chain, alias in _CHAIN_ALIASES.items()
            if any(a in low for a in alias)}


def _status_set(text: str) -> set[str]:
    known = ("可用", "待所有权验证", "验证中", "已过期", "验证失败")
    return {s for s in known if s in (text or "")}


def _status_flow_targets(text: str) -> set[str]:
    return _status_set(text)


_PLACEHOLDER_WORDS = ("待补充", "待定", "TBD", "TODO", "待确认内容", "xx", "XX", "？？", "???")


def _heading_level(block: dict[str, str]) -> int:
    tag = str(block.get("tag") or "")
    digits = "".join(ch for ch in tag if ch.isdigit())
    return int(digits) if digits else 2


def _has_body_before_next_peer(blocks: list[dict[str, str]], start: int, level: int) -> bool:
    """标题 `start` 之后、遇到同级或更高级标题之前，有没有非标题正文。"""
    for b in blocks[start + 1:]:
        tag = str(b.get("tag") or "")
        if tag.startswith("h") or tag.startswith("heading"):
            if _heading_level(b) <= level:
                return False
            continue
        if _cell_text(b).strip():
            return True
    return False


def structure_findings(blocks: list[dict[str, str]], *, prd_url: str = "") -> list[Finding]:
    """结构完整性：占位符残留 + 空章节。

    这两类在评审里最常被跳过：占位符留在正文会被当成已定稿（「待补充」上线），
    空章节让目录看着齐全、实际没写。都是**零判断**的事实，不含模型意见。
    """
    out: list[Finding] = []
    sections = _section_map(blocks)
    base = (prd_url or "").split("#")[0]

    def _jump(block_id: str) -> str:
        return f"{base}#{block_id}" if base and block_id else ""

    holders: list[dict[str, str]] = []
    for b in blocks:
        text = _cell_text(b)
        if any(w in text for w in _PLACEHOLDER_WORDS):
            holders.append(b)
    if holders:
        first = holders[0]
        bid = str(first.get("block_id") or "")
        out.append(Finding(
            title=f"占位符残留：{len(holders)} 处仍写着「待补充/待定」。"
                  if len(holders) > 1 else "占位符残留：正文里还写着「待补充/待定」",
            quote=" ／ ".join(_cell_text(b).strip()[:24] for b in holders[:4]),
            suggestion="补齐内容或删掉该条；带占位符的条款不能作为开发依据",
            block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
            kind="占位符", confidence=0.8))

    empty: list[dict[str, str]] = []
    for i, b in enumerate(blocks):
        tag = str(b.get("tag") or "")
        if not (tag.startswith("h") or tag.startswith("heading")):
            continue
        # 只把**叶子标题**纳入判定：紧接着就是更高级标题，说明它是父标题，
        # 内容在子节里，不算空（「异常场景处理」下有 1./2./3. 子节）。
        digits = "".join(ch for ch in tag if ch.isdigit())
        level = int(digits) if digits else 2
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if nxt is None:
            empty.append(b)                     # 文末标题
            continue
        ntag = str(nxt.get("tag") or "")
        if not (ntag.startswith("h") or ntag.startswith("heading")):
            continue
        # 紧跟的下一个就是标题时，再往后找同级的下一节：只有「本级下一节之前
        # 一个正文都没有」才算空。子节里写了内容不算空（「异常场景处理」下有三条）。
        if _has_body_before_next_peer(blocks, i, level):
            continue
        empty.append(b)
    if empty:
        first = empty[0]
        bid = str(first.get("block_id") or "")
        out.append(Finding(
            title=(f"空章节：{len(empty)} 个标题下没有内容"
                   if len(empty) > 1 else "空章节：标题下没有内容"),
            quote=" ／ ".join(_cell_text(b).strip()[:20] for b in empty[:4]),
            suggestion="补内容或把该章节从目录里去掉；空章节会让评审误以为已覆盖",
            block_id=bid, section=sections.get(bid, ""), jump_url=_jump(bid),
            kind="结构", confidence=0.75))
    return out


async def build_digest(
    *, story_ref: str, meegle: Any, anchor: BlockAnchor | None = None,
    search_meetings: Any = None, related_limit: int = 12,
    ui_matcher: Any = None, figma_token: str = "",
) -> StoryDigest:
    """取一条 Story 的全部分析素材。失败逐项记 errors，不整体抛。

    `meegle` 是 `MeegleClient`（鸭子类型：`get_story` / `list_by_name`）；
    `search_meetings` 是 async fn(query) → list[dict]，不注入就跳过会议段。
    """
    sid = parse_story_ref(story_ref)
    digest = StoryDigest(story_id=sid)
    if not sid:
        digest.errors.append("没解析出 Story ID（需要 /story/detail/<id> 链接或纯数字 ID）")
        return digest
    try:
        story = meegle.get_story(sid)
    except Exception as e:
        digest.errors.append(f"Story 读取失败: {str(e)[:160]}")
        return digest
    digest.title = story.title
    digest.status = story.status
    digest.node = story.node
    digest.url = story.url
    digest.space_name = story.space_name
    digest.priority = story.priority
    digest.business = story.business
    digest.prd_url = story.prd_url
    digest.prd_token = story.prd_token
    digest.design_url = story.design_url
    digest.design_field = story.design_field
    digest.owners = dict(story.owners)
    # Story 字段为空 → 再查「设计工单」（本需求正是这种：稿子挂在设计工单上）
    if not digest.design_url:
        try:
            url, field, work_id = meegle.design_link_from_workorder(digest.title)
        except Exception as e:
            url, field, work_id = "", "", ""
            digest.design_errors.append(f"设计工单查询失败: {str(e)[:120]}")
        if url:
            digest.design_url = url
            digest.design_field = f"{field}（设计工单 #{work_id}）"
    if not digest.design_url:
        digest.design_errors.append(
            "Story 的设计字段与「设计工单」都没有设计稿链接——设计稿可能只在飞书图片块里，"
            "需要人工贴 Figma 链接")

    # 同族需求：两条独立线索，各自成组（同前缀 = 同一产品线；同迭代 = 一起上线）
    prefix = name_prefix(digest.title)
    if prefix:
        try:
            rows = meegle.list_by_name(prefix, limit=related_limit + 1)
            digest.prefix_group = f"同前缀「{prefix}」"
            digest.related = [{
                "story_id": r.story_id, "title": r.title, "status": r.status,
                "design_url": r.design_url, "url": r.url, "basis": digest.prefix_group,
            } for r in rows if r.story_id != sid][:related_limit]
        except Exception as e:
            digest.errors.append(f"同前缀需求查询失败: {str(e)[:120]}")
    # 同迭代：Story 上没有迭代字段，只能从 Sprint 侧「迭代下需求」反查
    try:
        sprint_name, _sprint_id, sprint_stories = meegle.sprint_of(sid)
    except Exception as e:
        sprint_name, _sprint_id, sprint_stories = "", "", []
        digest.errors.append(f"同迭代需求查询失败: {str(e)[:120]}")
    if sprint_stories:
        digest.sprint_group = f"同迭代「{sprint_name}」" if sprint_name else "同迭代"
        in_prefix = {r["story_id"] for r in digest.related}
        digest.related.extend({
            "story_id": s.story_id, "title": s.title, "status": s.status,
            "design_url": s.design_url, "url": s.url or meegle.story_url(s.story_id),
            "basis": digest.sprint_group,
        } for s in sprint_stories if s.story_id not in in_prefix)
    digest.related_basis = "；".join(
        b for b in (digest.prefix_group, digest.sprint_group) if b)
    versions = version_tokens(f"{digest.title} {digest.prd_url}")
    if versions:
        digest.related_basis += "；版本标记 " + "、".join(sorted(set(versions)))

    # PRD：块读 + 机械自检
    blocks: list[dict[str, str]] = []
    if digest.prd_token:
        reader = anchor or BlockAnchor()
        try:
            blocks = await reader._all_blocks(digest.prd_token)  # noqa: SLF001 — 同模块族
            digest.findings = mechanical_findings(blocks, prd_url=digest.prd_url)
            # 追加三组轻量检查：绝对表述（放行词）、跨章节枚举一致性、结构完整性
            digest.findings.extend(permissiveness_findings(blocks, prd_url=digest.prd_url))
            digest.findings.extend(consistency_findings(blocks, prd_url=digest.prd_url))
            digest.findings.extend(structure_findings(blocks, prd_url=digest.prd_url))
            try:
                digest.prd_title = await reader.title(digest.prd_token)
            except Exception:
                digest.prd_title = ""
        except Exception as e:
            digest.errors.append(f"PRD 正文读取失败: {str(e)[:140]}")
    else:
        digest.errors.append("Story 上没有可解析的 PRD 链接（「需求文档」字段为空或非飞书文档）")

    # PRD↔UI：只要给了 matcher 就尝试，**没设计稿也要说清为什么没比**
    # （早前把这两个分支写成一个 if/else，导致「PRD 有、设计稿无」时被报成「没 PRD」）
    if ui_matcher is not None:
        if not digest.design_url:
            digest.ui_errors.extend(digest.design_errors[:1])
        elif blocks:
            digest.findings.extend(
                await _ui_findings(digest, blocks, ui_matcher, figma_token))

    # 会议：只看有没有、能不能读
    if search_meetings is not None:
        queries = [q for q in _meeting_queries(digest.title) if len(q) >= 2][:2]
        seen: set[str] = set()
        for q in queries:
            try:
                hits = await search_meetings(q)
            except Exception as e:
                digest.meeting_errors.append(f"妙记搜索失败({q}): {str(e)[:100]}")
                continue
            for hit in hits or []:
                token = str(hit.get("token") or "")
                if token and token not in seen:
                    seen.add(token)
                    digest.meetings.append({**hit, "query": q})
    return digest


async def _ui_findings(digest: StoryDigest, blocks: list[dict[str, str]],
                       ui_matcher: Any, figma_token: str) -> list[Finding]:
    """PRD↔UI 差异 → Finding（与机械自检同一条链，卡片与按钮一致）。

    取不到素材时不编差异：把原因写进 `ui_errors`，卡片上如实显示
    「未比对」以及为什么（token 过期 / 画板无 TEXT 图层 / 无设计稿链接）。
    """
    from app.domains.prd_watch.ui import build_ui_frame_text, load_frames

    frames, errors = await load_frames(digest.design_url, figma_token)
    if not frames:
        digest.ui_errors.extend(errors[:2] or ["设计稿没有取到可比较的文本层素材"])
        return []
    excerpt = "\n".join(str(b.get("text") or "") for b in blocks
                        if b.get("text"))[:7000]
    points, stats = await ui_matcher.match(build_ui_frame_text(frames), excerpt)
    digest.ui_checked = True
    if stats.get("error"):
        digest.ui_errors.append(str(stats["error"])[:120])
    if not points and not digest.ui_errors:
        digest.ui_errors.append("")      # 空串 = 已比对且一致，卡片据此写「未发现差异」
    out: list[Finding] = []
    for cp in points:
        # UI 差异复用自己的锚点能力：在 PRD 里找它引用的那段原文
        block = _match_block(blocks, cp.current_text or cp.quote_text)
        bid = str(block.get("block_id") or "") if block else ""
        out.append(Finding(
            title=cp.title or "UI 与 PRD 不一致",
            quote=cp.current_text or "（PRD 未写这条，稿子里有）",
            suggestion=cp.suggested_text or "",
            block_id=bid,
            section=_section_of(blocks, bid),
            jump_url=(f"{digest.prd_url.split('#')[0]}#{bid}"
                      if digest.prd_url and bid else ""),
            kind="UI差异", confidence=cp.confidence or 0.8))
    return out


def _match_block(blocks: list[dict[str, str]], quote: str) -> dict[str, str] | None:
    """按原文找块（与 BlockAnchor 同口径的轻量版，只用于 UI 差异定位）。"""
    needle = normalize(quote)[:40]
    if not needle:
        return None
    for b in blocks:
        if needle in normalize(str(b.get("text") or "")):
            return b
    return None


def _section_of(blocks: list[dict[str, str]], block_id: str) -> str:
    if not block_id:
        return ""
    return _section_map(blocks).get(block_id, "")


def _meeting_queries(title: str) -> list[str]:
    """需求名 → 会议检索词：去掉【】前缀与分隔符后取最长的两段。"""
    name = title or ""
    for sep in ("】", "]"):
        if sep in name:
            name = name.split(sep, 1)[-1]
    parts = [p.strip(" -—（()）：:") for p in re.split(r"\s+|[-—]", name)]
    parts = sorted({p for p in parts if len(p) >= 2}, key=len, reverse=True)
    return parts
