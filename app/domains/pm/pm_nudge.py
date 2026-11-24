"""PM Nudge - 催办草稿：从版本计划里挑出该催的人，起草 → 修改 → 确认 → 发送

为什么单独一个模块
------------------
催办不是"查询"，也不能"一条消息直接发出去"。它是本系统里第一个
**以机器人的身份向外发言、且对方不是本人** 的动作。风险有三层：

    1. 挑错了人  —— 把测试的问题甩给开发，群里当场社死
    2. 说错了话  —— 语气像告警机器人，或者上下文不准
    3. 发错了地方 —— 发到客户群、老板群

所以本模块把这三层都做成显式结构，而不是埋在流程里：

    挑人  pick_nudges()      纯函数，规则可读可改
    起草  draft()            模板拼装，变量一次填满
    审批  stage/revise/commit 两段式，与 pm_weekly_write 同构

关于"催谁"
----------
sht000 一条需求有开发/测试/产品三个责任人，但某个时刻只有一个人在推进。
判定依据是「需求状态」这一列，映射表见 STATUS_TO_ROLE —— 这是**唯一**
需要随流程调整的地方，改了这里全链路生效。

关于"发到哪"
------------
PMO 群里只有李四和易山武，开发测试都不在里面。
所以 commit() 不内置目标群，由调用方传入 chat_id（或私聊 open_id）。
留空则只把成稿回给用户，他自己复制走 —— 这是当前可用的最稳妥方式。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.domains.pm.pm_config import pm_config
from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    StoryItem,
    _calc_workdays_diff,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm import pm_followup, pm_people, pm_scope, pm_send, pm_story_source

logger = logging.getLogger("pm_hub.nudge")


# ══════════════════════════════════════════════════════════════════════
# 规则层 —— 唯一需要随流程调整的配置
# ══════════════════════════════════════════════════════════════════════

# 状态 → 该找谁。值取 "dev" / "qa" / "pm"。
# 未被定义的状态（如"待确认"）不产生催办——宁可漏，不可错。
STATUS_TO_ROLE: Dict[str, str] = {
    "待开发": "dev",     # 开发还没开工，催进度
    "实现中": "dev",     # 开发在做，催提测
    "待提测": "dev",     # 已就绪待提交
    "SIT测试": "qa",     # 已提测，催测试结论
    "UAT验证": "pm",     # 测试过了，等产品/PM 验收
    "待上线": "pm",      # 只差 PM 提工单
}

ROLE_LABEL = {"dev": "开发", "qa": "测试", "pm": "产品"}

# 提前几天开始催。工作日在倒排日 <= 这个值时进入催办窗口。
NUDGE_WINDOW_WORKDAYS = 5

# 昵称表。留空则用表格里的全名。
# 不要凭猜测填——叫错称呼比叫全名更糟。等用户确认了再往里加。
NICKNAMES: Dict[str, str] = {}


def _short_version(v: str) -> str:
    """"10.29" → "10.29 版本"。留作其他渲染处使用。"""
    return f"{v} 版本" if v else "本周版本"


def _story_label(story) -> str:
    """需求名去掉【业务线】前缀，句子更顺。

    接受两种 story 对象：工作簿的 StoryItem（有 .title）和飞书项目的
    pm_story_source.Story（有 .name）。催办话术不关心数据从哪来。
    """
    raw = getattr(story, "title", None) or getattr(story, "name", "") or ""
    return re.sub(r"^【[^】]*】", "", raw).strip() or raw


def _call_name(full: str) -> str:
    """把 '郑灿、汪佳' 这类多人字段拆成可称呼的形式。"""
    if not full or full in ("待定", "-"):
        return ""
    people = [p.strip() for p in re.split(r"[、,，/]", full) if p.strip()]
    return "、".join(NICKNAMES.get(p, p) for p in people)


# ══════════════════════════════════════════════════════════════════════
# 挑人
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Nudge:
    """一条待催办事项。字段齐到能直接渲染，不回头查表。"""
    story: StoryItem
    role: str                 # dev / qa / pm
    audience: str             # 该催的人（已套昵称）
    workdays_left: int
    reason: str               # 为什么催，用于草稿里的依据句


def pick_nudges(today: Optional[date] = None) -> List[Nudge]:
    """从 sht000 挑出今天该催的事项，按紧急度升序（越急越靠前）。

    判定：状态有明确承接角色 + 落在催办窗口内（含已过期）。
    """
    today = today or date.today()
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N60")
    stories = parse_stories(vals)
    radar = evaluate_daily_radar(stories, today=today)
    left_by_version = {v["version"]: v["workdays_left"] for v in radar.get("active_versions", [])}

    out: List[Nudge] = []
    for s in stories:
        role = STATUS_TO_ROLE.get(s.status)
        if not role:
            continue

        left = left_by_version.get(s.version)
        if left is None:
            continue
        if left > NUDGE_WINDOW_WORKDAYS:
            continue

        audience = _call_name(s.dev_owner if role == "dev" else s.qa_owner if role == "qa" else s.pm_owner)
        if not audience:
            continue  # 责任人待定，催不了，留给盲区提示去报

        if left < 0:
            reason = f"已过上线日 {abs(left)} 个工作日，还卡在「{s.status}」"
        elif left == 0:
            reason = f"今天就是 {s.version} 上线日，还卡在「{s.status}」"
        else:
            reason = f"距 {s.version} 上线还剩 {left} 个工作日，还卡在「{s.status}」"

        out.append(Nudge(
            story=s,
            role=role,
            audience=audience,
            workdays_left=left,
            reason=reason,
        ))

    out.sort(key=lambda n: (n.workdays_left, n.story.title))
    return out


# ══════════════════════════════════════════════════════════════════════
# 从飞书项目 story 挑人
#
# pick_nudges 读工作簿，责任人列是人手抄的，可能已经过时；而且工作簿的
# 「批次」和「目标上线日」本来就互相打架（全表 5 处冲突）。
# 这条路改读 story：用「当前负责人」定位现在真正卡住的人，
# 用迭代名里的日期算还剩几个工作日。
#
# 两条路并存：工作簿那套留给「上线日只有表里有」的场景，
# 这条给「story 已经更新了」的场景。draft 语气两边共用。
# ══════════════════════════════════════════════════════════════════════

# story 状态 → 该找谁。和 STATUS_TO_ROLE 语义一致，键用 story 归一化后的口径。
ROLE_BY_STAGE: Dict[str, str] = {
    "需求中": "pm",     # PRD 还没定，卡在产品
    "待开发": "dev",    # 排进来了没开工
    "实现中": "dev",    # 在做，问提测
    "sit测试": "qa",
    "uat测试": "pm",
    "生产验证": "pm",
}

_ITER_8 = re.compile(r"(20\d{2})(\d{2})(\d{2})")
_ITER_MD = re.compile(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})")

# 他自己。不给自己起草催办。名字从配置读——写死会在换人后静默失效，
# 变成机器人给自己发催办（:313 的过滤条件）。
ME = (pm_config().identity.names or [""])[0]


def _month_day(m: "re.Match[str]", today: date) -> date:
    """补年份。迭代名里的日期不带年，早于今天太久的当作明年。"""
    month, day = int(m.group(1)), int(m.group(2))
    guess = date(today.year, month, day)
    return guess if (today - guess).days < 180 else date(today.year + 1, month, day)


def iteration_date(name: str, today: Optional[date] = None) -> Optional[date]:
    """从迭代名解析上线日。解析不出来就返回 None —— 不猜。

        "20261126"                 → 2026-11-26
        "财富票据v3.4.1（11.26）"   → 当年 11-26
        "财富票据v3.5.0（预计12.17）" → 当年 12-17
        "星企通 V1.6.4"             → None（那是版本号，不是日期）

    只认两种形态：8 位日期串，或括号里的「月.日」。绝不全文找 —— 那样会把
    V1.6.4 读成 1 月 6 日。
    """
    today = today or date.today()
    m = _ITER_8.search(name or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for chunk in re.findall(r"[（(]([^）)]*)[）)]", name or ""):
        m = _ITER_MD.search(chunk)
        if m:
            return _month_day(m, today)
    return None


def _primary_owner(field: str) -> str:
    """当前负责人可能是多人，取第一个当主责。称呼里塞六个名字没法说话。"""
    return re.split(r"[、,，/]", field or "")[0].strip()


# ══════════════════════════════════════════════════════════════════════
# 问询节拍：上线前一周的周二、周三
#
# 不是「提前一天」—— 那时候改不动了，问了也白问；
# 也不是「提前三周」—— 开发还没排期，问了只会得到「还没到」。
# 提前一周多、卡在周中问，他手上刚好能给出确定答复。
# ══════════════════════════════════════════════════════════════════════

def ask_days(launch: date) -> tuple:
    """发版前一周里该问的日子，从配置的「提前几天」反推。

    `[projects.pm.rules.release].ask_days_before` 是**自然日**提前量，
    相对发版日算：默认 `[8]` = 上周三，要周二+周三写 `[8, 9]`。

        release_weekday=4（周四）, ask_days_before=[8]  → (11-18,)
        release_weekday=4, ask_days_before=[8, 9]       → (11-17, 11-18)

    为什么不用「发版前的周二、周三」这种写法：那等于把「什么时候问」和
    「什么时候必须过闸门」放在两套坐标系里各算各的，改发版日就要跟着改这里。
    """
    days = pm_config().rules.release.ask_days_before
    return tuple(launch - timedelta(days=int(d)) for d in days)


def next_ask_day(launch: date, today: date) -> Optional[date]:
    """今天及之后最近的一个发问日；窗口已过返回 None。"""
    for d in sorted(ask_days(launch)):
        if d >= today:
            return d
    return None


def is_ask_day(launch: date, today: date) -> bool:
    return today in ask_days(launch)


def _fallback_launch(today: date) -> date:
    """迭代名解析不出上线日时的兜底：配置里的每周发版日（本周或下周）。

    这条兜底存在的原因是「发版日是硬节拍」：迭代没建、名字不规范、版本压根没
    进飞书项目时，`iteration_date` 返回 None，`due_iterations` 会**静默**跳过，
    那一周就没人核对提测。兜底走的是「每周四发版」这条规律本身。
    """
    wd = pm_config().rules.release.release_weekday
    delta = (int(wd) - today.weekday()) % 7
    return today + timedelta(days=delta)


def due_iterations(today: Optional[date] = None) -> List["pm_story_source.Iteration"]:
    """今天该问的迭代。上线日还没过 + 今天正好是它的某个发问日。

    迭代名里命中明确不归他管的项目（星企通、7709）先剔掉，省一次 story 拉取。

    解析不出上线日时走**兜底节拍**（配置里的每周发版日），并在日志里写明这次
    走的是哪条路。兜底只在「没有一个迭代解析出上线日」时启用——按迭代逐个兜底
    会把所有名字不规范的迭代在同一天全问一遍，那是刷屏不是提醒。

    拉不到迭代列表时（飞书项目 MCP 未接 / 凭据过期）**走兜底而不是抛异常**：
    这是每日调度里的一个子步骤，抛出去只会让整轮早间推送挂掉，
    而用户真正想要的是「到点问一句提测」，迭代名只是其中一种定位方式。
    """
    today = today or date.today()
    try:
        iterations = pm_story_source.resolve("")
    except Exception as e:
        logger.warning("拉迭代列表失败，改走每周节拍兜底: %s", e)
        iterations = []

    parsed: List[tuple["pm_story_source.Iteration", date]] = []
    unparsed: List["pm_story_source.Iteration"] = []
    for it in iterations:
        if pm_scope.out_of_scope_hit(it.name):
            continue
        launch = iteration_date(it.name, today)
        if launch:
            parsed.append((it, launch))
        else:
            unparsed.append(it)

    out = [it for it, launch in parsed if launch >= today and is_ask_day(launch, today)]
    if out:
        return out

    launch = _fallback_launch(today)
    if is_ask_day(launch, today):
        # 兜底时没有真实迭代名可用，用发版日造一个占位——`nudges_from_iteration`
        # 只用到 `.name` 做版本匹配，占位名不会命中任何 story，结果只依赖表格。
        logger.warning(
            "今天是提测询问日（兜底发版日 %s），迭代名不可用：%d 个解析不出上线日，"
            "拉取失败或列表为空时按占位迭代继续",
            launch.isoformat(), len(unparsed),
        )
        return [_FallbackIteration(launch.strftime("%m.%d"))]
    return out


@dataclass
class _FallbackIteration:
    """兜底迭代：只有名字，用来让 `nudges_from_iteration` 走完流程。

    为什么不复用 `pm_story_source.Iteration`：那个类带 `item_id`，
    构造一个假的 id 会让下游误以为能去飞书项目查详情。
    """

    name: str


def pending_ask_plan(today: Optional[date] = None) -> List[dict]:
    """今天/明天该发的提测询问清单，**不发送**。

    给调度层用：它把这份计划落成持久化的「待发」记录，真正发出去才消。
    这样「进程没开 / 机器休眠 / 槽被占」都不会让一次发版询问永久丢失——
    跨日续发由调度层根据记录里的 `due` 判断，不在这里做。
    """
    today = today or date.today()
    plan: List[dict] = []
    for it in due_iterations(today):
        try:
            items = nudges_from_iteration([it], today=today)
        except Exception as e:
            logger.warning("挑催办失败（迭代 %s）: %s", it.name, e)
            continue
        if items:
            plan.append({
                "iteration": it.name,
                "due": today.isoformat(),
                "count": len(items),
            })
    return plan


def nudges_from_iteration(keyword, today: Optional[date] = None) -> List[Nudge]:
    """按迭代关键字（或直接给 Iteration 列表）挑出该问的人。

    一人一条，同一人的多条需求合并。三条硬规则：
    已交付的不问、不在他范围的不问、他自己的不问 —— 最后一条不是不管他，
    是他名下的需求没人可问，那属于「等你这边的动作」，在 pm_story_pull 里列。

    产物和 pick_nudges 同构，可以直接走 stage() 的暂存确认链路。
    """
    today = today or date.today()
    if isinstance(keyword, (list, tuple)):
        iters = list(keyword)
    else:
        iters = pm_story_source.resolve(keyword)
    if not iters:
        return []
    stories = pm_story_source.stories_of([i.name for i in iters])

    # 用最紧的那个迭代日期作为这批的上线日；都没日期就明说没定，不编数字
    dates = [d for d in (iteration_date(i.name, today) for i in iters) if d]
    left = _calc_workdays_diff(min(dates), today) if dates else None

    buckets: Dict[str, List] = {}
    for s in stories:
        if s.handed_over:
            continue
        if not pm_scope.in_scope(f"{s.name} {s.line}"):
            continue
        role = ROLE_BY_STAGE.get(s.stage)
        if not role:
            continue
        who = _primary_owner(s.operator)
        if not who or who == ME:
            continue
        buckets.setdefault(f"{role}|{who}", []).append(s)

    if left is None:
        base = "上线日还没定"
    elif left >= 0:
        base = f"{min(dates).month}.{min(dates).day:02d} 这版还剩 {left} 个工作日"
    else:
        base = f"{min(dates).month}.{min(dates).day:02d} 这版已过上线日 {abs(left)} 个工作日"

    out: List[Nudge] = []
    for key, items in buckets.items():
        role, audience = key.split("|", 1)
        reason = base + (f"，你手上还有 {len(items)} 条没交" if len(items) > 1 else "")
        out.append(Nudge(
            story=items[0],
            role=role,
            audience=audience,
            workdays_left=left if left is not None else 0,
            reason=reason,
        ))
    out.sort(key=lambda n: (n.workdays_left, n.audience))
    return out


# ══════════════════════════════════════════════════════════════════════
# 起草
# ══════════════════════════════════════════════════════════════════════

def draft(n: Nudge) -> str:
    """按角色套语气。目标是像他自己在说话，不是系统通知。

    开发：问提测进度
    测试：问测试结论
    产品：问验收/工单

    语气分两档，别在还早的时候催 —— 离上线还有三周就"今天能给个准信不"
    是骚扰，对方会烦，他也会被烦。过了催办窗口才是催，没到只是问一句节奏。
    """
    name = n.audience
    label = _story_label(n.story)
    urgent = n.workdays_left <= NUDGE_WINDOW_WORKDAYS

    if n.role == "dev":
        if not urgent:
            return (
                f"哈喽 {name}，{label} 这边什么节奏？\n"
                f"{n.reason}，我看看要不要提前排联调。"
            )
        return (
            f"哈喽 {name}，{label} 提测了嘛？\n"
            f"{n.reason}，今天能给个准信不？"
        )
    if n.role == "qa":
        return (
            f"哈喽 {name}，{label} 的 SIT 结论出来了吗？\n"
            f"{n.reason}。"
        )

    # 同为「产品」，两种处境话术完全不同：需求还没定，问的是 PRD；
    # 已经测完，问的是验收。只有 story 源知道细分状态（工作簿没有这层）。
    if getattr(n.story, "stage", "") == "需求中":
        return (
            f"哈喽 {name}，{label} 的 PRD 这周能定下来吗？\n"
            f"{n.reason}，定了我这边好排开发和测试。"
        )
    return (
        f"哈喽 {name}，{label} 可以验收了吗？\n"
        f"{n.reason}，确认后我就提上线工单。"
    )


def draft_all(nudges: List[Nudge]) -> List["PendingNudge"]:
    return [PendingNudge(nudge=n, body=draft(n)) for n in nudges]


# ══════════════════════════════════════════════════════════════════════
# 暂存与审批
#
# 与 pm_weekly_write 用同一套「暂存 → 确认 → 执行」形状，但**独立的槽位**：
# 改表状态和发催办消息语义完全不同，共用一个槽位会让"确认"指代不明。
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PendingNudge:
    nudge: Nudge
    body: str
    edited: bool = False      # 是否被用户改过


@dataclass
class PendingNudgeBatch:
    sender: str
    items: List[PendingNudge]
    created_at: datetime = field(default_factory=datetime.now)
    # 卡片回调靠 batch_id 认「点的是哪一批」——用户可能翻出几天前的旧卡片，
    # 只按 sender 认会让旧卡片的点击改到当前草稿。
    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # 卡片发出后回填，用于「点完原地更新」而不是再发一条
    message_id: str = ""
    # 已点「改这条」的条目号（1 起）。仅用于卡片重绘时高亮，不影响权威状态。
    editing: int = 0


_PENDING: Dict[str, PendingNudgeBatch] = {}

# 「确认 / 取消」口令与 pm_weekly_write 保持一致，避免用户要记两套说法。
CONFIRM_WORDS = frozenset({
    "确认", "确定", "是", "好", "好的", "嗯", "对", "ok", "okay", "yes", "发", "发送", "发吧", "就这样",
})
CANCEL_WORDS = frozenset({"取消", "算了", "不用", "不发", "no", "cancel"})

# 「改写」引导词。命中后，剩余部分整体替换草稿正文。
_REVISE_PREFIX = re.compile(r"^(?:改成|改为|换成|修改为|改写为|编辑)\s*[:：]?\s*")
_REVISE_ITEM = re.compile(r"^第\s*(\d+)\s*条\s*(?:改成|改为|换成)\s*[:：]?\s*")


def has_pending(sender: str) -> bool:
    return sender in _PENDING


def pending_count(sender: str) -> int:
    batch = _PENDING.get(sender)
    return len(batch.items) if batch else 0


def stage(sender: str, nudges: List[Nudge]) -> str:
    """把草稿放进待确认队列，返回预览。不发送任何消息。"""
    if not nudges:
        return (
            "📭 当前没有需要催的事项。\n\n"
            f"催办窗口是距上线 {NUDGE_WINDOW_WORKDAYS} 个工作日以内，"
            "且状态还停在开发/测试环节。"
        )

    batch = PendingNudgeBatch(sender=sender, items=draft_all(nudges))
    _PENDING[sender] = batch

    lines = [f"✍️ 拟了 {len(batch.items)} 条催办，先看一眼："]
    for i, item in enumerate(batch.items, 1):
        n = item.nudge
        lines.append("")
        lines.append(f"—— 第 {i} 条 · 找{ROLE_LABEL[n.role]} {n.audience} ——")
        lines.append(item.body)

    lines.append("")
    if len(batch.items) == 1:
        lines.append("要改就说「改成：……」。说「确认」我就以你的身份发出去，发出去收不回。")
    else:
        lines.append("要改哪条就说「第n条改成：……」。说「确认」我就以你的身份全发出去，发出去收不回。")
    return "\n".join(lines)


def get_batch(sender: str) -> Optional[PendingNudgeBatch]:
    """当前待确认批次（卡片重绘与回调用）。没有就返回 None。"""
    return _PENDING.get(sender)


def batch_by_id(batch_id: str) -> Optional[PendingNudgeBatch]:
    """按 batch_id 反查批次。卡片回调必须走这个——只按 sender 会让旧卡片
    的点击改到当前草稿。"""
    if not batch_id:
        return None
    for batch in _PENDING.values():
        if batch.batch_id == batch_id:
            return batch
    return None


def attach_message_id(sender: str, message_id: str) -> None:
    """记下确认卡片的 message_id，供回调时原地更新。"""
    batch = _PENDING.get(sender)
    if batch is not None and message_id:
        batch.message_id = message_id


def set_editing(batch: PendingNudgeBatch, index: int) -> None:
    """标记「正在改第 N 条」。只影响卡片文案，权威状态仍是 items[index-1].body。"""
    batch.editing = index if 0 <= index <= len(batch.items) else 0


def send_one(batch: PendingNudgeBatch, index: int, deliver=None) -> str:
    """只发第 index 条（1 起），其余留在批次里。返回结果说明。

    为什么不复用 `commit`：commit 是「整批一次发完并清空暂存」，
    单条发送之后批次还要留着——用户可能接着发下一条。

    Args:
        deliver: 可选投递函数 `(body) -> bool`。传了就不走通讯录+飞书，
                 直接把正文交出去——测试用它避免真的以 PM 名义给人发消息。
    """
    if not (1 <= index <= len(batch.items)):
        return f"只有 {len(batch.items)} 条，没有第 {index} 条。"
    item = batch.items[index - 1]
    if deliver is not None:
        ok = bool(deliver(item.body))
        if not ok:
            return f"一条都没发出去：\n✗ {item.nudge.audience}：投递失败"
        batch.items.pop(index - 1)
        if not batch.items:
            _PENDING.pop(batch.sender, None)
        return f"发了 1/1 条：\n✓ {item.nudge.audience}"
    result = _send_items([item])
    if result["ok"]:
        batch.items.pop(index - 1)
        if not batch.items:
            _PENDING.pop(batch.sender, None)
    return result["text"]


def drop_one(batch: PendingNudgeBatch, index: int) -> str:
    """不发第 index 条（1 起），从批次里去掉。"""
    if not (1 <= index <= len(batch.items)):
        return f"只有 {len(batch.items)} 条，没有第 {index} 条。"
    n = batch.items.pop(index - 1).nudge
    if not batch.items:
        _PENDING.pop(batch.sender, None)
    return f"已跳过 {n.audience} 那条，剩余 {len(batch.items)} 条。"


def drop_batch(sender: str) -> str:
    """整批放弃。"""
    _PENDING.pop(sender, None)
    return "已放弃这次催办草稿，什么都没发。"


def _apply_revise(batch: PendingNudgeBatch, text: str) -> Optional[str]:
    """处理「第2条改成：xxx」/「改成：xxx」（整体）。返回提示语，未命中返回 None。"""
    m = _REVISE_ITEM.match(text)
    if m:
        idx = int(m.group(1)) - 1
        if not (0 <= idx < len(batch.items)):
            return f"只有 {len(batch.items)} 条，没有第 {idx + 1} 条。"
        batch.items[idx].body = text[m.end():].strip()
        batch.items[idx].edited = True
        return f"第 {idx + 1} 条已替换为：\n\n{batch.items[idx].body}"

    m = _REVISE_PREFIX.match(text)
    if m:
        rest = text[m.end():].strip()
        # 单条时直接替换；多条时全部替换容易冲掉个性化内容，明确拒绝
        if len(batch.items) != 1:
            return (
                "现在有多条草稿，整体替换会冲掉各自的称呼和依据。\n"
                "请指明改哪条：比如「第1条改成：……」。"
            )
        batch.items[0].body = rest
        batch.items[0].edited = True
        return f"已替换为：\n\n{rest}"

    return None


def handle_pending(sender: str, text: str) -> Optional[str]:
    """有待确认的催办时，接住用户的下一句话。未命中返回 None 交给上层。"""
    batch = _PENDING.get(sender)
    if not batch:
        return None

    key = text.strip().lower().strip("。.!！")
    if key in CANCEL_WORDS:
        _PENDING.pop(sender, None)
        return "已放弃这次催办草稿，什么都没发。"

    if key in CONFIRM_WORDS:
        return commit(sender)

    revised = _apply_revise(batch, text.strip())
    if revised is not None:
        return revised + "\n\n改完说「确认」我就发。"

    return None


def commit(sender: str, send_fn=None) -> str:
    """定稿并发出。send_fn(open_id, body) -> message_id，不传则真发。

    发出去就收不回来，所以逐条独立处理：一个人发失败不影响其他人，
    最终把每个人成没成如实报回来，不含糊说「已发送」。
    发成功的登记进跟进表 —— 否则过几天没人知道该回读哪些需求。
    """
    batch = _PENDING.pop(sender, None)
    if not batch:
        return "没有待发送的草稿。"
    return _send_items(batch.items, send_fn)["text"]


def _send_items(items: List[PendingNudge], send_fn=None) -> Dict[str, Any]:
    """逐条发出并登记跟进表。返回 {"ok": 成功条数, "text": 给人看的结果}。

    从 `commit` 里抽出来是为了让「整批发」和「单条发」共用同一段发送逻辑——
    两份实现最容易分化的地方是「失败怎么报」和「跟进表登不登」，
    分开写迟早一边漏。
    """
    send = send_fn or (lambda oid, body: pm_send.send_text(oid, body)["message_id"])

    results: List[str] = []
    sent_meta: List[Dict[str, Any]] = []

    for item in items:
        n = item.nudge
        open_id = pm_people.open_id_of(n.audience)
        if not open_id:
            results.append(f"✗ {n.audience}：通讯录里没找到唯一账号，没发")
            continue
        try:
            msg_id = send(open_id, item.body)
        except Exception as e:
            logger.exception("催办发送失败 audience=%s", n.audience)
            results.append(f"✗ {n.audience}：{type(e).__name__}: {str(e)[:80]}")
            continue

        results.append(f"✓ {n.audience}（{msg_id}）")
        sent_meta.append({
            "story_id": getattr(n.story, "item_id", 0),
            "story_name": getattr(n.story, "name", "") or getattr(n.story, "title", ""),
            "audience": n.audience,
            "open_id": open_id,
            "role": n.role,
            "stage": getattr(n.story, "stage", ""),
            "operator": getattr(n.story, "operator", ""),
            "message_id": msg_id,
        })

    tracked = pm_followup.record(sent_meta) if sent_meta else 0

    ok = len(sent_meta)
    head = f"发了 {ok}/{len(results)} 条：" if ok else "一条都没发出去："
    body = head + "\n" + "\n".join(results)
    if tracked:
        body += f"\n\n这 {tracked} 条我盯着，有进展或者三天没动静会告诉你。"
    return {"ok": ok, "text": body}