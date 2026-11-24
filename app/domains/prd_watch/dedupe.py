"""跨轮归一/去重 — 同一份素材重跑不应产出"新的"重复评论。

背景：同一份会议纪要连跑 3 次，条数已稳定在 7/7/7，但每条标题措辞
每轮不同（「申购取消 KYT 校验」/「申购流程不再执行 KYT」）。如果每次都落评论，同一处
变更会在 PRD 上堆出 3 条近义评论——这是**比措辞漂移更实际的危害**。

两层（都在代码里做，不额外调 LLM —— 多一次模型调用只会引入新的不确定性）：
1. **批内归一**：同一篇 PRD 里两条变更表述近似 → 合并成一条（保留高置信的，模块取并集）。
   模型偶尔会把同一结论拆两条（尤其 change_type 一拆二）。
2. **跨轮去重**：同一 (Story, 素材) 已经出过的变更，重跑时不再重复（按近似度判定）。

刻意**不做**的事：不缓存识别结果本身。PRD 正文改了、会议又讨论了同一处，就该出新建议；
去重键里带素材标识，正好把这两种情况分开。
"""

from __future__ import annotations

import re
from typing import Any

from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL, ChangePoint

# 合并/去重阈值（字符集包含度）。实测标定：同义改写对 0.50~1.00，异义对 0.00~0.29；
# 0.50 落在两者之间。**宁可漏合也不误合**：漏合只是多一条近义评论（人眼可辨），
# 误合会把一条真变更吞掉（产品看不到，且不会报错）。
DEFAULT_THRESHOLD = 0.50

_PUNCT = re.compile(r"[\s，,。.、；;：:（）()【】\[\]\"'“”‘’\-—_*/]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).lower()


def bigrams(text: str) -> set[str]:
    t = normalize(text)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def similarity(a: str, b: str) -> float:
    """字符集包含度 `|A∩B| / min(|A|,|B|)`（去标点后的小写字符集合）。

    为什么不用二元组：中文同义改写（「取消 KYT」↔「不再执行 KYT」）二元组几乎不重叠
    （实测 0.12~0.2），会漏合并；字符集能抓住"申购/KYT/校验"这类实词
    （同一对实测 0.56）。为什么用 min 而不是并集：标题长短差异大，长标题会把
    并集压低到无论多相近都过不了阈值。
    """
    x, y = _charset(a), _charset(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))


def _charset(text: str) -> set[str]:
    return set(normalize(text))


# 变更的"主语"：出现这些词说明改的是申购/赎回那条链路，两条主语不同的变更不合并。
# 实测必要性：「申购取消 KYT」vs「赎回取消 KYT」字符集相似度 0.714，会被判成同一条。
_SUBJECTS = ("申购", "赎回", "货架", "详情", "持仓", "订单")


def _subjects(title: str) -> set[str]:
    return {s for s in _SUBJECTS if s in str(title or "")}


def _same_subject(a: str, b: str) -> bool:
    """两标题的主语集合不冲突（任一为空视为不冲突，交给相似度判断）。"""
    sa, sb = _subjects(a), _subjects(b)
    return not sa or not sb or bool(sa & sb)


def _title_similar(a: str, b: str) -> float:
    """标题近似度（跨轮去重只看标题）。

    为什么跨轮只比标题：同一篇 PRD 的多条变更常**共用同一段原文**（原文是块级的），
    把原文/建议放进指纹会让"同一块里的两处不同改动"被判成同一条 → 重跑时后出现的
    那条被误删。措辞漂移只发生在标题上，正好是我们要拦的维度。
    """
    return similarity(a, b)


def _best_score(a: ChangePoint, b: ChangePoint) -> float:
    """近似度只在**同一维度**上比，不做跨字段取最大值。

    注意：取 title/current_text/suggested_text 三者最大值时，
    同一篇 PRD 里的两条不同变更常常共用同一段原文（原文是"块级"的，一条块里能提两处），
    于是 sim(current_text) 很高 → 两条被误合并，真变更被吞掉。
    所以：标题相近且原文/建议也沾边才算同一处；原文相同但标题不同 = 两条不同变更。
    """
    title_sim = similarity(a.title, b.title)
    if title_sim < 0.35:
        return 0.0
    return max(title_sim,
               0.5 * (title_sim + similarity(a.suggested_text, b.suggested_text)),
               0.4 * (title_sim + similarity(a.current_text, b.current_text)))


def merge_points(points: list[ChangePoint],
                 threshold: float = DEFAULT_THRESHOLD) -> tuple[list[ChangePoint], int]:
    """批内归一：近似的两条合成一条（保留置信度高的），返回 (结果, 合并数)。

    合并时把另一条的模块并进来、`evidence_quote` 取更长的（信息不丢），
    `band` 取两者里更保守的（任一条是低置信，合并结果仍标低置信——不能因为
    另一条置信高就把"需要人看"的标记洗掉）。
    """
    kept: list[ChangePoint] = []
    merged_n = 0
    for cp in points:
        hit = None
        for i, other in enumerate(kept):
            if (cp.story_id and other.story_id and cp.story_id != other.story_id):
                continue                     # 不同 PRD 的不合并（合并会串篇）
            if not _same_subject(cp.title, other.title):
                # 主语不同（申购 vs 赎回）→ 同一动作也是两件事。
                # 实测「申购取消 KYT」与「赎回取消 KYT」相似度 0.714，纯相似度会误合。
                continue
            if _best_score(cp, other) >= threshold:
                hit = i
                break
        if hit is None:
            kept.append(cp)
            continue
        merged_n += 1
        kept[hit] = _combine(kept[hit], cp)
    return kept, merged_n


def _combine(a: ChangePoint, b: ChangePoint) -> ChangePoint:
    """保留信息量更大的那条，再把另一条的补充字段并进来。"""
    main, extra = (a, b) if a.confidence >= b.confidence else (b, a)
    mods = list(dict.fromkeys([*main.modules, *extra.modules]))
    main.modules = mods[:6]
    if len(extra.evidence_quote or "") > len(main.evidence_quote or ""):
        main.evidence_quote = extra.evidence_quote
    if not main.quote_text and extra.quote_text:
        main.quote_text = extra.quote_text
    if not main.current_text and extra.current_text:
        main.current_text = extra.current_text
        main.is_new_clause = extra.is_new_clause
    if extra.band == BAND_FLAGGED and main.band == BAND_NORMAL:
        main.band = BAND_FLAGGED           # 保守：低置信标记不被洗掉
    main.confidence = min(main.confidence, extra.confidence) if main.band == BAND_FLAGGED \
        else main.confidence
    return main


class EmittedIndex:
    """同一 (Story, 素材) 已出过的变更指纹（进程内）。

    为什么不做持久化：重跑同一份素材是**同一次灰度/联调**里的行为，进程重启后
    重来一次最多多一条评论（且那条评论本身正确）；而落库要在台账里再加一列，
    未知列会让整行 upsert 失败——代价远大于收益。
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold
        self._seen: dict[tuple[str, str], list[str]] = {}

    def reset(self) -> None:
        self._seen.clear()

    def is_duplicate(self, story_id: str, source_ref: str, cp: ChangePoint) -> bool:
        key = (story_id, source_ref)
        for old_title in self._seen.get(key, []):
            if (not _same_subject(old_title, cp.title)):
                continue                     # 申购 ≠ 赎回：主语不同不是重复
            if _title_similar(old_title, cp.title) >= self._threshold:
                return True
        return False

    def record(self, story_id: str, source_ref: str, cp: ChangePoint) -> None:
        # 只记标题：原文是块级的、同一块里可能有两处不同改动，记进指纹会把它们判成同一条
        self._seen.setdefault((story_id, source_ref), []).append(cp.title)

    def drop_duplicates(self, story_id: str, source_ref: str,
                        points: list[ChangePoint]) -> tuple[list[ChangePoint], int]:
        """跨轮去重 + 批内归一（顺序不能反：先批内合并，再和已出过的比）。"""
        merged, merged_n = merge_points(points, self._threshold)
        fresh = [cp for cp in merged if not self.is_duplicate(story_id, source_ref, cp)]
        return fresh, merged_n + (len(merged) - len(fresh))

    def snapshot(self) -> dict[str, Any]:
        return {"keys": len(self._seen),
                "signatures": sum(len(v) for v in self._seen.values())}


# ── 文档侧已有评论检测（跨进程、跨重启）──────────────────────────────
#
# 进程内指纹只防「同一素材重跑」。真正需要防的还有两种，它们跨进程存在：
# 1. 上一轮讨论已经写过的同一处变更（进程重启后指纹全丢）；
# 2. 别的入口写进去的评论（人工提的、旧版本 bot 提的）。
# 两者都只能从**文档本身**看出来 —— 飞书评论列表里带着锚定块 id 与原文。


def comment_title(body: str) -> str:
    """从评论正文里取出「变更项 / 建议调整」那行的标题。

    bot 写的评论有两种版式，都要认（旧版没删干净时混在一起，只认一种就漏判）：
        📋 PRD变更建议 · CR-…     /  🤖 Bot EasyPRD 自动评论 · CR-…
        ▎变更项  /  【改什么】
        <标题>
    human 写的评论没有这个结构 → 返回空串（此时只按块 id 判重，不猜语义）。
    """
    text = str(body or "")
    if "PRD变更建议" not in text and "Bot EasyPRD" not in text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.startswith("▎变更项") and i + 1 < len(lines):
            return lines[i + 1]
        if ln.startswith("▎建议调整") and i + 1 < len(lines):
            return lines[i + 1][:40]
    # 新版板式：标题是【改什么】的下一行（与旧版「▎变更项」等价的位置）
    for i, ln in enumerate(lines):
        if ln.startswith("【改什么】") and i + 1 < len(lines):
            return lines[i + 1][:60]
    return ""


def _bodies(comment: dict[str, Any]) -> list[str]:
    """一条评论里所有可能带标题的文本（正文 + 原文引用）。"""
    return [str(comment.get("body") or ""), str(comment.get("quote") or "")]


# 动作方向：同一段原文里的"收敛/删减"与"新增/扩充"是两件事，不能因为同块就判重。
_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "reduce": ("收敛", "删除", "去掉", "取消", "移除", "减少", "精简", "降", "收敛为", "删"),
    "add": ("新增", "增加", "补充", "添加", "扩展", "提高到", "提升"),
    "change": ("改为", "调整", "变更", "修改", "改成"),
}


def _action_vector(title: str) -> set[str]:
    """标题隐含的动作方向（reduce / add / change）；判断不出返回空集。

    空集 = 不参与"同块放宽"判据：宁可多问一次（多写一条评论），
    也不要因为两个标题都认不出动作就把真变更吞掉。
    """
    t = str(title or "")
    return {k for k, words in _ACTION_WORDS.items() if any(w in t for w in words)}


def duplicate_of(
    cp: ChangePoint, comments: list[dict[str, Any]], threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any] | None:
    """这条变更是不是**文档里已经有一条等价评论**了？是就返回那条评论，否则 None。

    判据顺序（从严到宽，命中即返回）：
    1. 同一锚定块 + 标题近似 —— 最像"同一处变更"；
    2. 标题近似（块没锚上或块不同，但说的就是同一件事）；
    3. 锚定块相同 + 我的标题出现在那条评论里（措辞变化较大但明显同源）。
    第 2 条不加块约束是刻意的：锚点本轮可能定位失败、或落到相邻块上，而
    "同一个变更提两遍"对产品的伤害大于"少提一条"。

    为什么不用 CR 编号判重：CR 是按「Story + 当天序号」生成的，跨天/跨进程会变，
    它只能证明"这条评论是我们写的"，不能证明"这条评论说的是同一处变更"。
    """
    title = cp.title or ""
    block = cp.block_id or ""
    for c in comments:
        if c.get("is_whole"):
            continue                     # 整篇评论没有定位，无从比对，不作为判重依据
        c_block = str(c.get("block_id") or "")
        c_title = comment_title(str(c.get("body") or ""))
        same_block = bool(block) and block == c_block
        if c_title and title:
            sim = _title_similar(title, c_title)
            action = _action_vector(title) & _action_vector(c_title)
            # 主语不同（申购 ≠ 赎回）一律不算重复：同一段原文里两条不同链路的改动，
            # 字符集相似度会很高（实测 0.71），只靠相似度会把真变更吞掉。
            guard = _same_subject(title, c_title)
            # 标题对同一处变更的措辞会漂（「筛选项收敛为产品类型等三项」vs
            # 「筛选下拉改为三个」，字符集相似度只有 0.36），但**落在同一段原文**
            # 且**动作方向一致**（都是「收敛/删减」）时就是同一处。
            if same_block and guard and action and sim >= threshold * 0.4:
                return c
            if same_block and guard and sim >= threshold * 0.7:
                return c
            if sim >= threshold and guard:
                return c
        if same_block and _same_subject(title, " ".join(_bodies(c))) and \
                _bigrams_hit(title, _bodies(c)):
            return c
    return None


def _bigrams_hit(title: str, texts: list[str]) -> bool:
    """标题是否几乎逐字出现在这些文本里（字符覆盖 ≥ 0.75）。

    阈值不能低：评论正文里必然带着「申购上限」「申购」这类**领域常用词**，
    0.5 的覆盖度在「申购上限 5万→20万」与「申购/赎回按钮说明」之间就能凑齐
    （实测 0.56），把同一段原文下的另一条真变更吞掉。
    """
    chars = _charset(title)
    if not chars:
        return False
    for t in texts:
        inter = chars & _charset(t)
        if len(inter) / len(chars) >= 0.75:
            return True
    return False


# 定位关键词里的泛词：这些词几乎每段规则都有，拿它们去搜会命中一堆无关块。
_GENERIC = ("的", "与", "和", "及", "了", "对", "在", "为", "是", "要求", "规则",
            "条款", "表述", "说明", "补充", "新增", "调整", "统一", "规范", "处理")


def locator_terms(text: str, limit: int = 5) -> list[str]:
    """从一句建议表述里切出**有区分度**的关键词（3~6 字，去掉泛词与同源重复）。

    为什么不用整句去搜：文档里几乎不会逐字出现模型写的建议表述，整句匹配必然失败。
    切词后再让 anchor 按「多个词共现」找块，命中率远高于「搜整句」。

    为什么要去同源：滑窗切出来的「中台产品 / 台产品库 / 产品库未」其实是同一个短语的
    三个偏移，全留着等于把一个词算三遍，会让"恰好含这个词"的错块赢过正确的块
    （实测「价格确定方式」的变更被定位到了申购权限那一段，就因为那段的「中台产品库」）。
    所以长窗口优先，与已选片段字符重叠过半的候选直接丢。
    """
    s = "".join(str(text or "").split())
    if len(s) < 3:
        return []
    picked: list[str] = []
    for size in (6, 5, 4, 3):
        for i in range(0, len(s) - size + 1):
            if len(picked) >= limit:
                return picked
            piece = s[i:i + size]
            if any(g in piece for g in _GENERIC):
                continue
            chars = set(piece)
            if any(len(chars & set(p)) / len(chars) >= 0.5 for p in picked):
                continue
            picked.append(piece)
    return picked
