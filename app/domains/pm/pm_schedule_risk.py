"""排期体检 —— 从 story 的节点时间读出「这条到底能不能按时上线」。

职责边界
--------
只做一件事：拿飞书项目的节点排期，算风险和结论。
    不写工作簿（pm_weekly_write）、不判业务归属（pm_scope）、
    不起草话术（pm_nudge）、不决定问谁（pm_nudge 里那套 ask_days）。

为什么不能只列字段
------------------
他原话：「根据那个 agent 去分析他们的排期」。
把「实现中 11-02~11-16、SIT 未排」抄一遍给他，等于把飞书项目页面复述一遍，
他自己会看。他真正需要的是**判断**：这条赶不赶得上发版日、断在哪、该压谁。

所以这里的输出结构是 [结论一句话] + [节点链] + [风险清单]，
不是字段字典。

判断只依赖两类硬事实
--------------------
1. 节点时间（start/end）—— 飞书项目里人填的，可核对
2. 迭代名里的日期（V2.43.8-20261112 → 11-12 发版日）—— 双周四节拍

工时**不参与判断，也不出现在输出里**。原因：duration 和「实际工时」单位口径不明，
且研发填报的工时有普遍低报倾向，拿它算欠排得出的结论站不住。排期靠不靠谱，
日期比工时可辩护得多 —— 节点有没有时间窗、环环能不能接上，这才是硬约束。
评估工作量是另一件事，走 pm_estimate 那条线，和排期判断不混。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Tuple

from app.domains.pm import pm_project_mcp as mcp
from app.domains.pm import pm_story_source as src

logger = logging.getLogger("pm_hub.schedule_risk")


# 节点流顺序。list_schedule 返回的节点是乱序的（实测：生产验证、待评审、
# 需求中、待上线、规划中、UAT验收、已上线、SIT测试、容量评估、实现中），
# 靠返回顺序讲故事会把「生产验证」说成第一步。
PIPELINE: Tuple[str, ...] = (
    "规划中", "待评审", "需求中", "容量评估", "实现中",
    "SIT测试", "UAT验收", "生产验证", "待上线", "已上线",
)

# 开发交出去之后的环节 —— 这些没排期，就等于上线日没有落点。
_TEST_STAGES = ("SIT测试", "UAT验收", "生产验证", "待上线")

# 子任务里出现这些词，说明要跟别的系统对接口，延期高发。
_CROSS_SYS = ("联调", "对接", "接入", "权限", "鉴权", "接口")

# 迭代名里的发版日：20261112 优先，其次 11.26 / 11-26 / 11/26
_YMD = re.compile(r"(20\d{2})(\d{2})(\d{2})")
_MD = re.compile(r"(\d{1,2})[.\-/](\d{1,2})(?!\d)")


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class NodeSpan:
    """一个节点的现状。时间为空本身就是结论。"""

    name: str
    start: Optional[date]
    end: Optional[date]
    reached: bool
    passed: bool
    people: Tuple[Tuple[str, float], ...]   # ((userKey, score), ...)

    @property
    def scheduled(self) -> bool:
        return bool(self.start and self.end)

    @property
    def current(self) -> bool:
        """到了但没过 —— 这就是「现在卡在这」。"""
        return self.reached and not self.passed


@dataclass(frozen=True)
class Plan:
    """一条需求在飞书项目里的排期全貌。"""

    story_id: int
    name: str
    iteration: str
    stage: str
    nodes: Tuple[NodeSpan, ...]
    subtasks: Tuple[str, ...]

    def node(self, name: str) -> Optional[NodeSpan]:
        for n in self.nodes:
            if n.name == name:
                return n
        return None

    @property
    def current(self) -> Optional[NodeSpan]:
        for n in self.nodes:
            if n.current:
                return n
        return None

    @property
    def release(self) -> Optional[date]:
        return release_date(self.iteration)

    @property
    def last_end(self) -> Optional[date]:
        ends = [n.end for n in self.nodes if n.end]
        return max(ends) if ends else None


@dataclass(frozen=True)
class Risk:
    level: str      # block / warn / info
    code: str
    text: str


# ══════════════════════════════════════════════════════════════════════
# 日期小工具
# ══════════════════════════════════════════════════════════════════════

def _to_date(raw: Optional[str]) -> Optional[date]:
    """"2026-11-16 23:59:59" → date(2026,11,16)。解不了就当没排。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).date()
    except ValueError:
        return None


def workdays(start: Optional[date], end: Optional[date]) -> int:
    """闭区间内的周一~周五天数。不算节假日 —— 节假日只会让真实剩余更少，
    拿它当「乐观上限」是安全的（乐观都排不下，那就真排不下）。"""
    if not start or not end or end < start:
        return 0
    n, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def release_date(iteration_name: str, today: Optional[date] = None) -> Optional[date]:
    """迭代名 → 发版日。取不到就返回 None，不猜。

    取**最后**一个 MM.DD 而不是第一个：迭代名常常带着产品版本号，
    「财富票据v3.4.1（11.26）」先命中的是 v3.4，会算成 3 月 4 日。
    发版日永远写在名字尾部。
    """
    name = iteration_name or ""
    m = _YMD.search(name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    hits = _MD.findall(name)
    if hits:
        month, day = hits[-1]
        try:
            return date((today or date.today()).year, int(month), int(day))
        except ValueError:
            pass
    return None


def fmt_day(d: Optional[date]) -> str:
    """话术里统一用 11-12 这种写法，ISO 全写读起来太笨。"""
    return f"{d:%m-%d}" if d else "未定"


def weekday_cn(d: date) -> str:
    return "周" + "一二三四五六日"[d.weekday()]


# ══════════════════════════════════════════════════════════════════════
# 取数
# ══════════════════════════════════════════════════════════════════════

def _node_spans(raw_nodes: Iterable[dict]) -> Tuple[NodeSpan, ...]:
    """按 PIPELINE 排序，未知节点排到最后（不丢数据）。"""
    spans = []
    for n in raw_nodes or []:
        st = n.get("state") or {}
        t = n.get("time") or {}
        people = tuple(
            (str((w.get("user_info") or {}).get("userKey", "")), float(w.get("score") or 0))
            for w in n.get("user_workload_list") or []
        )
        spans.append(
            NodeSpan(
                name=st.get("state_name") or "",
                start=_to_date(t.get("start")),
                end=_to_date(t.get("end")),
                reached=bool(st.get("reached")),
                passed=bool(st.get("passed")),
                people=people,
            )
        )
    order = {name: i for i, name in enumerate(PIPELINE)}
    return tuple(sorted(spans, key=lambda s: order.get(s.name, len(PIPELINE))))


def _subtask_names(raw_nodes: Iterable[dict]) -> Tuple[str, ...]:
    out: List[str] = []
    for n in raw_nodes or []:
        for sub in n.get("subtasks") or []:
            name = (sub.get("name") or "").strip()
            if name:
                out.append(name)
    return tuple(out)


def plan_of(story_id: int) -> Plan:
    """拉一条需求的排期全貌。两处失败都往上抛 McpError，由调用方说「没拉到」。"""
    body = mcp.call_tool("list_schedule", {
        "project_key": mcp.PROJECT_KEY,
        "work_item_type_key": "story",
        "work_item_id": str(story_id),
    })
    iw = body.get("instance_workload") or {}
    info = iw.get("work_item_info") or {}
    raw_nodes = iw.get("nodes") or []

    # 迭代名和状态从 MQL 拿（list_schedule 只给状态 id 不给可读名）
    snap = src.stories_by_ids([story_id]).get(story_id)

    return Plan(
        story_id=story_id,
        name=(snap.name if snap else "") or str(info.get("name") or ""),
        iteration=(snap.iteration if snap else ""),
        stage=(snap.stage if snap else ""),
        nodes=_node_spans(raw_nodes),
        subtasks=_subtask_names(raw_nodes),
    )


# ══════════════════════════════════════════════════════════════════════
# 判断
# ══════════════════════════════════════════════════════════════════════

def risks_of(plan: Plan, today: Optional[date] = None) -> List[Risk]:
    """一条需求的风险清单。空列表 = 排期自洽，不用管。"""
    today = today or date.today()
    out: List[Risk] = []
    cur = plan.current
    rel = plan.release

    if cur is None:
        out.append(Risk("info", "no_current", "没有落在「到了但没过」的节点上，流程上已经走完录入了。"))
    else:
        if cur.scheduled and cur.end < today:
            out.append(Risk(
                "block", "overdue",
                f"「{cur.name}」计划 {fmt_day(cur.end)} 结束，今天 {fmt_day(today)} 已经过了 —— "
                f"要么排期没更新，要么真卡住了，得先问清是哪种。",
            ))

        if rel and cur.end and cur.end > rel:
            gap = (cur.end - rel).days
            out.append(Risk(
                "block", "miss_release",
                f"「{cur.name}」排到 {fmt_day(cur.end)}，比迭代名里的发版日 {fmt_day(rel)} 还晚 {gap} 天。"
                f"按现在这个排法，这版赶不上，会顺延到下一个发版窗口。",
            ))

        if rel and cur.end and cur.end <= rel:
            left = workdays(today, cur.end)
            out.append(Risk(
                "info", "on_track",
                f"「{cur.name}」排到 {fmt_day(cur.end)}，在发版日 {fmt_day(rel)} 之前。"
                f"从今天算还有 {left} 个工作日给它。",
            ))

        if not cur.people:
            out.append(Risk("warn", "no_owner", f"「{cur.name}」节点上没有挂人力 —— 没人认领。"))
        elif len(cur.people) == 1:
            out.append(Risk(
                "warn", "single_person",
                f"「{cur.name}」只有 1 个人扛，没有备份。这个人一请假就直接延期。",
            ))

    # 后面还有环节没排 —— 断链。这是最容易被他忽略、也最致命的一条：
    # 实现排到哪天都无所谓，只要 SIT/UAT/上线全空，就等于「上线日没落点」。
    order = {name: i for i, name in enumerate(PIPELINE)}
    if cur and cur.name in order:
        after = [
            n for n in plan.nodes
            if n.name in _TEST_STAGES and order.get(n.name, -1) > order[cur.name]
        ]
        broken = [n.name for n in after if not n.scheduled]
        if broken:
            out.append(Risk(
                "block", "chain_broken",
                f"{cur.name}之后的 {'、'.join(broken)} 全都没排期 —— "
                f"测试和上线没有时间窗，上线日等于悬空。",
            ))

    # 跨系统子任务。不是排期数字，但是延期最真实的原因。
    cross = [t for t in plan.subtasks if any(k in t for k in _CROSS_SYS)]
    if cross:
        ratio = f"{len(cross)}/{len(plan.subtasks)}"
        out.append(Risk(
            "warn", "cross_system",
            f"{ratio} 个子任务涉及联调/接入/权限，要跟别的系统对接口。"
            f"这类活最容易在联调那一步卡住，排期上没留缓冲就会变成延期。",
        ))

    return out


def verdict(plan: Plan, risks: Sequence[Risk]) -> str:
    """一句话结论。他扫一眼就要知道「这条到底行不行」。"""
    blocks = [r for r in risks if r.level == "block"]
    rel = fmt_day(plan.release)
    if plan.release is None:
        head = f"迭代名「{plan.iteration or '空'}」里读不出发版日，只能看节点本身。"
    elif any(r.code == "miss_release" for r in blocks):
        head = f"这条赶不上 {rel} 这班车。"
    elif any(r.code == "overdue" for r in blocks):
        head = f"这条已经逾期，{rel} 之前交不出来。"
    elif any(r.code == "chain_broken" for r in blocks):
        head = f"实现能排上，但测试和上线没排，{rel} 之前收不了口。"
    else:
        head = f"排期目前自洽，{rel} 之前能走完。"

    if not blocks:
        warns = [r for r in risks if r.level == "warn"]
        if warns:
            head += f"有 {len(warns)} 个隐患要盯。"
    return head


# ══════════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════════

_LEVEL_MARK = {"block": "×", "warn": "!", "info": "·"}


def _node_line(n: NodeSpan) -> str:
    """一行一个节点。不做列对齐 —— 中文混排按字符数补空格永远补不齐，
    飞书里也不是等宽字体，假装是表格反而更乱。"""
    if n.scheduled:
        span = f"{fmt_day(n.start)}→{fmt_day(n.end)}"
    else:
        span = "未排期"
    mark = "▶" if n.current else ("✓" if n.passed else "○")
    who = f"　{len(n.people)} 人" if n.people else "　无人力"
    return f" {mark} {n.name}　{span}{who}"


def render(plan: Plan, today: Optional[date] = None) -> str:
    risks = risks_of(plan, today)
    lines = [f"{plan.name}（{plan.story_id}）", verdict(plan, risks), ""]

    lines.append("节点：")
    lines.extend(
        _node_line(n) for n in plan.nodes
        if n.scheduled or n.current or n.name in _TEST_STAGES
    )
    lines.append("")

    blocks = [r for r in risks if r.level == "block"]
    warns = [r for r in risks if r.level == "warn"]
    infos = [r for r in risks if r.level == "info"]

    if blocks:
        lines.append("卡住的地方：")
        lines.extend(f" {_LEVEL_MARK[r.level]} {r.text}" for r in blocks)
        lines.append("")
    if warns:
        lines.append("要盯的：")
        lines.extend(f" {_LEVEL_MARK[r.level]} {r.text}" for r in warns)
        lines.append("")
    if infos:
        lines.append("供参考：")
        lines.extend(f" {_LEVEL_MARK[r.level]} {r.text}" for r in infos)

    return "\n".join(lines).rstrip()


# ══════════════════════════════════════════════════════════════════════
# 迭代视角 —— 「他们的排期」通常指一整个版本
# ══════════════════════════════════════════════════════════════════════

def render_iteration(
    keyword: str,
    today: Optional[date] = None,
    iters: Optional[Sequence[src.Iteration]] = None,
) -> str:
    """迭代级：节拍 + 谁还没交。不发问、不催，只给判断。

    他要的「分析他们的排期」多数时候是这个视角 —— 关心的是这版能不能按期发，
    而不是某一条的字段。所以这里给的是分布和节拍，不是节点明细。
    """
    today = today or date.today()
    hits = list(iters) if iters is not None else src.resolve(keyword)
    if not hits:
        return f"飞书项目里没有名字带「{keyword}」的迭代。"

    name = hits[0].name
    rel = release_date(name, today)
    stories = src.stories_of([name])

    opened = [s for s in stories if not s.handed_over]
    testing = [s for s in stories if s.handed_over]

    lines = [f"迭代 {name}　共 {len(stories)} 条"]
    if len(hits) > 1:
        others = "、".join(h.name for h in hits[1:4])
        lines.append(f"（「{keyword}」还命中 {others}，先看这一个，要换说一声）")

    if rel:
        left = workdays(today, rel)
        lines.append(f"发版日 {fmt_day(rel)}（{weekday_cn(rel)}），从今天算还有 {left} 个工作日")
    else:
        lines.append("迭代名里读不出发版日，节拍只能按经验推")
    lines.append("")

    lines.append(f"已经交到测试/上线的 {len(testing)} 条")
    for s in testing:
        lines.append(f"· {s.name[:30]}　{s.stage}")
    lines.append("")

    if opened:
        lines.append(f"还没交的 {len(opened)} 条")
        for s in opened:
            who = f"　@{s.operator}" if s.operator else "　（没负责人）"
            lines.append(f"· {s.name[:30]}　{s.stage}{who}")
        lines.append("")
        # 整体判断：还没交的还堆在开发段，而发版日临近 —— 这是节拍风险
        if rel and len(opened) > len(testing):
            lines.append(
                f"节拍上偏紧：还有 {len(opened)} 条没进测试，"
                f"留 {fmt_day(rel)} 这个窗口的话，测试和验收会挤在一起。"
            )

    return "\n".join(lines).rstrip()


# ══════════════════════════════════════════════════════════════════════
# 统一入口 —— 他只说「看看这条排期」，不该要求他区分 id / 迭代 / 需求名
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Subject:
    """一次分析的落点：要么是一条具体需求，要么是一个迭代。

    plan 为 None 且 iters 非空 = 迭代视角（只做分布和节拍，不需要逐节点分析）。
    """

    plan: Optional[Plan]
    iters: Tuple["src.Iteration", ...] = ()


def _pick(target: str) -> Optional[Subject]:
    """把 target 解成唯一分析对象。解不出唯一目标就返回 None，交给 report 兜底。"""
    text = (target or "").strip()
    if not text:
        return None

    if text.isdigit():
        return Subject(plan_of(int(text)))

    iters = src.resolve(text)
    if iters:
        return Subject(None, tuple(iters))

    hits = src.stories_named([text])
    if len(hits) == 1:
        return Subject(plan_of(hits[0].item_id))
    return None


def report(target: str, today: Optional[date] = None) -> str:
    """给迭代关键字、需求名、story id 中的任意一种，都能出排期判断。

    判定顺序是有讲究的：先试迭代，再退化成需求名反查。
    因为「11.26」「票据」这类词既可能是迭代也可能是需求名的一部分，
    而他要看的东西九成是「这一版」，不是「某一条」。
    """
    text = (target or "").strip()
    if not text:
        return "给我个版本关键字、需求名，或者 story id，我去飞书项目拉。"

    if text.isdigit():
        return render(plan_of(int(text)), today)

    iters = src.resolve(text)
    if iters:
        return render_iteration(text, today, iters)

    hits = src.stories_named([text])
    if not hits:
        return f"飞书项目里没找到跟「{text}」对应的迭代，也没有名字带这个词的需求。"

    if len(hits) == 1:
        return render(plan_of(hits[0].item_id), today)

    lines = [f"名字带「{text}」的需求有 {len(hits)} 条，说要哪条我细看：", ""]
    for s in hits[:8]:
        lines.append(f"· {s.name[:32]}　{s.stage}　{s.item_id}")
    if len(hits) > 8:
        lines.append(f"…还有 {len(hits) - 8} 条")
    return "\n".join(lines).rstrip()


# ══════════════════════════════════════════════════════════════════════
# 判断层 —— 规则负责「事实一致」，模型负责「这意味着什么」
# ══════════════════════════════════════════════════════════════════════
#
# 为什么要分两层
# --------------
# 纯规则能发现「11-16 晚于 11-12」，发现不了「12 个子任务里有两个是重写权限体系，
# 9 人日根本装不下」。反过来，纯模型会编节点、编日期。
#
# 所以：规则先把事实钉死（可核对、零幻觉），模型只在事实之上做判断，
# 并被要求「用到推断的地方明说」。模型挂了就退回规则结论，不空手。

OPINION_SYSTEM = """你是李四的 PM 参谋。他管公募、私募、ESOP、票据四条业务线的研发交付。

他会给你一条需求的排期事实。你要给**判断**，不是复述事实。

按这个顺序说：
1. 最可能出问题的地方（一句话，要具体）
2. 为什么 —— 只用给的事实推；用到推断的地方明确写「推断」
3. 他今天该做的一件事 —— 具体到找谁、说什么。最多两件

硬性要求：
- 不要编事实，不要补他没给的数据
- 事实不足以判断时，直接说缺什么
- 只谈时间。有没有时间窗、环环接不接得上、谁的空档被踩了
- 不要谈工时、人天、工作量大小 —— 他不看这个
- 不用 markdown、表格、加粗，飞书不渲染。短行，· 和序号分隔
- 12 行以内，不要开场白和总结"""


def facts_of(plan: Plan, today: Optional[date] = None) -> str:
    """把一条需求摊成给模型看的事实包。

    这里只放**从飞书项目真实读到的字段**。任何加工过的结论（比如规则判出的
    风险）放在最后单独一段，标注来源，避免模型把它当成原始事实再推一遍。
    """
    today = today or date.today()
    facts = [f"今天：{today.isoformat()} {weekday_cn(today)}"]
    facts.append(
        f"迭代：{plan.iteration}（迭代自身的完成时间字段为空，"
        f"发版日只能从名字里的日期推）"
        if plan.iteration else "迭代：未填"
    )
    facts.append(f"需求：{plan.name}（{plan.story_id}）　当前状态 {plan.stage}")

    facts.append("节点排期：")
    for n in plan.nodes:
        if n.scheduled:
            state = "已通过" if n.passed else ("进行中" if n.current else "未开始")
            facts.append(
                f"  {n.name}　{n.start}~{n.end}　{len(n.people)} 人　{state}"
            )
        elif n.name in _TEST_STAGES or n.current:
            facts.append(f"  {n.name}　未排期")

    if plan.subtasks:
        facts.append(f"子任务 {len(plan.subtasks)} 个：")
        facts.extend(f"  - {t[:80]}" for t in plan.subtasks)

    try:
        siblings = src.stories_of([plan.iteration]) if plan.iteration else []
    except Exception:
        siblings = []
    if siblings:
        opened = [s for s in siblings if not s.handed_over]
        done = [s for s in siblings if s.handed_over]
        facts.append(
            f"同迭代共 {len(siblings)} 条：已进测试/上线 {len(done)} 条，"
            f"还压在开发段 {len(opened)} 条"
        )
        if opened:
            facts.append("  还在开发段的：" + "；".join(
                f"{s.name[:18]}({s.stage},{s.operator})" for s in opened
            ))

    risks = risks_of(plan, today)
    if risks:
        facts.append("（以下为规则层判出的结论，不是原始字段）")
        facts.extend(f"  [{r.level}] {r.text}" for r in risks)

    return "\n".join(facts)


def _opinion(facts: str, timeout: int = 90) -> Optional[str]:
    """让模型出判断。网关不通/超时返回 None —— 调用方据此降级。

    延迟 import：pm_intent_router 带着 LLM 凭据，和本模块的日期计算没有关系，
    顶层 import 会让「只算个工作日」也要拖上 requests。
    """
    try:
        from app.domains.pm.pm_intent_router import call_llm_text
        raw = call_llm_text(OPINION_SYSTEM, facts, timeout=timeout)
    except Exception:
        logger.warning("排期判断层不可用，退回规则结论", exc_info=False)
        return None
    return (raw or "").strip() or None


def analyse(target: str, today: Optional[date] = None) -> str:
    """排期分析：事实由规则钉死，判断交给模型，模型不可用就退回规则。

    和 report 的区别只有一点：report 是纯计算结果，analyse 会带观点。
    他要「你的意见」时用这个，要「核对一遍数据」时用 report。
    """
    sub = _pick(target)
    if sub is None:
        # 多条命中 / 没找到 —— 这类回答不需要模型
        return report(target, today)

    if sub.plan is None:
        # 迭代视角本来就是统计分布，模型的增量信息有限
        return render_iteration(target, today, sub.iters)

    facts = facts_of(sub.plan, today)
    opinion = _opinion(facts)
    if opinion:
        return opinion

    # 降级：把规则结论顶上，并明确告知这不是模型判断
    return (
        render(sub.plan, today)
        + "\n\n（判断层暂时连不上，上面是规则算的结论，没有经过分析。）"
    )