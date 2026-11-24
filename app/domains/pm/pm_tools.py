"""PM Tools - 智能体可调用的能力注册表

定位
----
本模块是「机器人会做什么」的**唯一清单**。循环层（pm_agent）不认识任何业务，
只认识这里的工具名；入口层（pm_daemon）也不认识业务，只调循环层。

    入口层  pm_daemon        收消息、分派元指令（确认/取消）
       │
    循环层  pm_agent         带历史的 tool-calling 循环
       │
    工具层  pm_tools  ◄──── 本文件：能力清单 + 执行器
       │
    领域层  pm_hub_engine / pm_nudge / pm_weekly_* / pm_knowledge_hub ...

与 pm_hub_handler 的关系
------------------------
pm_hub_handler 里那 11 个工具定义是好的，但它绑死在 `AgentHandler` 基类上，
而那个基类属于 app/main.py 那套跑不起来的框架。所以这里**重写执行器、
保留工具语义**，去掉框架依赖。pm_hub_handler 后续可整体废弃。

两种返回形态
------------
工具跑完有两种可能：

    content  —— 结构化结果，交给模型组织语言        （查人物、查知识库、估工时）
    render   —— 已经排好版的成品，直接回用户，别让模型改  （日计划、催办草稿、写表预览）

后者必须短路。让模型复述一遍排好版的清单，只会把格式弄坏。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    evaluate_daily_radar,
    fetch_sheet_values,
    parse_stories,
)
from app.domains.pm.pm_intent_router import TodoItem
from app.domains.pm.pm_todo_intake import intake_structured_todos
from app.domains.pm.pm_knowledge_hub import query_people_map, search_knowledge_base, search_notes
from app.domains.pm import pm_kb_index
from app.domains.pm.pm_estimator import estimate_prd_effort
from app.domains.pm import pm_awareness, pm_nudge, pm_weekly_plan, pm_weekly_write
from app.domains.pm import pm_project_mcp, pm_scope, pm_story_source
from app.domains.pm import pm_followup
from app.domains.pm import pm_schedule_risk, pm_kb_gaps, pm_thread
from app.domains.pm.pm_daily_brief import build_and_render as build_daily_brief
from app.domains.pm.pm_session import Session

logger = logging.getLogger("pm_hub.tools")


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ToolContext:
    """工具执行时需要知道的一切现场。不读全局变量。"""
    sender: str
    session: Optional[Session] = None


@dataclass
class ToolOutcome:
    content: str                       # 给模型看
    render: Optional[str] = None       # 非空则直接作为最终回复
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    run: Callable[[Dict[str, Any], ToolContext], ToolOutcome]


# ══════════════════════════════════════════════════════════════════════
# 内部工具函数
# ══════════════════════════════════════════════════════════════════════

def _load_radar(today: Optional[date] = None) -> Dict[str, Any]:
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N60")
    return evaluate_daily_radar(parse_stories(vals), today=today or date.today())


def _match_version(versions: List[dict], q: str) -> Optional[dict]:
    """从提问里认出用户指的是哪个版本。容忍 10.29 / 10月29 / 10/29。"""
    q = (q or "").strip()
    if not q:
        return None
    direct = next((v for v in versions if v["version"] in q), None)
    if direct:
        return direct
    m = re.search(r"(\d{1,2})\s*[.\-/月]?\s*(\d{1,2})", q)
    if not m:
        return None
    key = f"{int(m.group(1))}.{int(m.group(2))}"
    return next((v for v in versions if v["version"] == key), None)


# ── 各工具的执行体 ────────────────────────────────────────────────────

def _run_daily_brief(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    text = build_daily_brief(date.today())
    return ToolOutcome(content=text, render=text)


def _run_radar(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    radar = _load_radar()
    versions = radar.get("active_versions") or []

    # 只要卡点清单：回答「谁卡着」「有哪些风险」，不起草任何东西
    if args.get("blockers"):
        try:
            stuck = pm_nudge.pick_nudges()
        except Exception as e:
            logger.exception("读取卡点失败")
            return ToolOutcome(content=f"读取失败：{e}", is_error=True)
        if not stuck:
            return ToolOutcome(
                content="无卡点",
                render="目前没有卡在开发/测试环节、且临近上线的需求。",
            )
        lines = ["现在卡着的有这些：", ""]
        for n in stuck:
            lines.append(f"· {n.story.title[:26]}")
            lines.append(f"  {pm_nudge.ROLE_LABEL[n.role]} {n.audience} — {n.reason}")
        return ToolOutcome(content="\n".join(lines), render="\n".join(lines))

    if not versions:
        return ToolOutcome(
            content="版本计划表里没读到有效版本行，可能是表格结构变了。",
            render="📅 版本计划表里没读到有效版本行，我核对下表格结构再回你。",
        )

    hit = _match_version(versions, args.get("version") or "")
    if hit:
        left = hit["workdays_left"]
        timing = (
            f"距上线还有 {left} 个工作日" if left > 0
            else "今天就是上线日" if left == 0
            else f"已过上线日 {abs(left)} 个工作日"
        )
        render = (
            f"📅 {hit['version']} 版本\n\n"
            f"· 目标上线：{hit['target_date']}\n"
            f"· {timing}\n"
            f"· 需求数：{hit['story_count']} 条\n"
            f"· 当前闸门：{hit['current_gate'] or '尚未进入闸门区间'}"
        )
        return ToolOutcome(content=render, render=render)

    # 没指定版本 → 给全量快照，让模型自己组织
    snapshot = [
        {
            "版本": v["version"],
            "上线日": v["target_date"],
            "剩余工作日": v["workdays_left"],
            "需求数": v["story_count"],
            "当前闸门": v["current_gate"],
        }
        for v in sorted(versions, key=lambda x: x["target_date"])
    ]
    return ToolOutcome(content=json.dumps(snapshot, ensure_ascii=False))


def _stage_nudges(items: List[Any], ctx: ToolContext) -> ToolOutcome:
    """把挑好的人放进待确认队列，并把清单登记进会话。

    登记的目的是让下一句「第2条改成…」指代得上 —— 没有这份清单，
    改稿指令就只能靠模型猜编号。
    """
    if not items:
        text = pm_nudge.stage(ctx.sender, [])
        return ToolOutcome(content=text, render=text)

    text = pm_nudge.stage(ctx.sender, items)

    if ctx.session:
        ctx.session.remember(nudges=[
            {
                "no": i,
                "who": n.audience,
                "what": (getattr(n.story, "title", None) or getattr(n.story, "name", ""))[:24],
            }
            for i, n in enumerate(items, 1)
        ])
    return ToolOutcome(content=text, render=text)


def _run_draft_nudges(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    scope = (args.get("scope") or "").strip()

    # 给了版本关键字先走飞书项目：那里的「当前负责人」是系统里真正卡住的人，
    # 工作簿那一列是人手抄的，可能早就过时了。
    if scope:
        try:
            from_story = pm_nudge.nudges_from_iteration(scope)
        except Exception as e:
            logger.warning("从 story 挑催办对象失败，回落工作簿：%s", e)
            from_story = []
        if from_story:
            return _stage_nudges(from_story, ctx)

    try:
        items = pm_nudge.pick_nudges()
    except Exception as e:
        logger.exception("挑催办对象失败")
        return ToolOutcome(
            content=f"读取版本计划失败：{type(e).__name__}",
            render=f"读版本计划失败（{type(e).__name__}），稍后再试一次。",
            is_error=True,
        )

    if scope and items:
        m = re.search(r"(\d{1,2})\s*[.\-/月]\s*(\d{1,2})", scope)
        if m:
            mm, dd = int(m.group(1)), int(m.group(2))
            items = [
                n for n in items
                if n.story.version.split(".")[:2] == [str(mm), f"{dd:02d}"] or n.story.version == f"{mm}.{dd}"
            ]

    return _stage_nudges(items, ctx)


def _run_log_plan(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    raw = args.get("todos") or []
    todos: List[TodoItem] = []
    for x in raw:
        if isinstance(x, str):
            todos.append(TodoItem(title=x))
        elif isinstance(x, dict) and x.get("title"):
            todos.append(TodoItem(
                title=str(x["title"]),
                date_hint=str(x.get("date_hint") or "today"),
                gate=x.get("gate"),
            ))

    if not todos:
        msg = "没识别出具体工作项，换个说法再发一次？\n例：今日：1、私募周报；2、story排期"
        return ToolOutcome(content="todos 为空", render=msg)

    res = intake_structured_todos(todos, auto_sync_bitable=True)
    if not res.get("success"):
        return ToolOutcome(content=str(res), render=res.get("message") or "录入失败。")

    got = res["todos"]
    sync = res.get("sync_result") or {}
    created, total = sync.get("created", 0), sync.get("total", len(got))
    tail = (
        f"\n\n🔗 已写入多维表格「日计划」：{created}/{total}"
        if created == total
        else f"\n\n🔗 多维表格「日计划」写入 {created}/{total}，失败项需人工补录"
    )

    lines = [f"✅ 今日计划已同步（{len(got)} 项）", ""]
    for i, t in enumerate(got, 1):
        lines.append(f"{i}. {t.title}")
        if t.gate:
            lines.append(f"    └ {t.gate}")

    flat: List[str] = []
    for item in res.get("blindspots") or []:
        flat.extend(item.get("alerts") or [])
        flat.extend(item.get("blindspots") or [])
    if flat:
        lines.append("")
        lines.append("⚠️ 需要提前收口")
        lines.extend(f"  · {x}" for x in flat[:3])
        if len(flat) > 3:
            lines.append(f"  · 另有 {len(flat) - 3} 条未展开")

    text = "\n".join(lines) + tail
    return ToolOutcome(content=text, render=text)


def _run_update_weekly(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    target = str(args.get("target") or "").strip()
    status = str(args.get("status") or "").strip()
    if not target or not status:
        return ToolOutcome(content="缺 target 或 status", is_error=True)

    try:
        plan = pm_weekly_plan.load_active_plan(date.today())
    except Exception as e:
        logger.exception("读取周计划失败")
        return ToolOutcome(
            content=f"读取周计划失败：{e}",
            render=f"读工作簿失败（{type(e).__name__}），稍后重试一次。",
            is_error=True,
        )

    block = plan.block
    if not block:
        return ToolOutcome(
            content="no active week block",
            render="工作簿里没读到「每周计划跟踪」的当周块，暂时没法回写。",
        )

    # 逐个定位。任何一条定位不到就整批不动 —— 半截写入比不写更难查。
    resolved, problems = pm_weekly_plan.resolve_targets(block, target)

    if problems or not resolved:
        listing = "\n".join(f"{it.no}. {it.task}  [{it.status}]" for it in block.items)
        detail = "\n".join(f"· {p}" for p in problems) if problems else "编号没对上。"
        msg = f"{detail}\n\n本周（{block.label}）的事项：\n{listing}"
        return ToolOutcome(content=msg, render=msg)

    text = pm_weekly_write.stage_batch(
        sender=ctx.sender,
        pairs=[(it, status) for it in resolved],
        week_label=block.label,
    )
    if ctx.session:
        ctx.session.remember(weekly_items=[
            {"no": it.no, "task": it.task, "status": it.status} for it in block.items
        ])
    return ToolOutcome(content=text, render=text)


def _run_query_people(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    q = str(args.get("query") or "").strip()
    if not q:
        return ToolOutcome(content="query 为空", is_error=True)
    res = query_people_map(q)
    return ToolOutcome(content=json.dumps(res, ensure_ascii=False))


def _run_search_knowledge(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    q = str(args.get("query") or "").strip()
    if not q:
        return ToolOutcome(content="query 为空", is_error=True)
    hits = pm_kb_index.search(q, k=3)
    if not hits:
        return ToolOutcome(content="[]（知识库无匹配）")
    payload = [
        {
            "source": h.chunk.source,
            "heading": h.chunk.heading,
            "score": round(h.score, 2),
            "text": h.chunk.text[:400],
        }
        for h in hits
    ]
    return ToolOutcome(content=json.dumps(payload, ensure_ascii=False))


def _run_estimate(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """估人天。传 story_id 最省事 —— 子任务清单本身就是现成的技术要点。"""
    title = str(args.get("title") or "").strip()
    desc = str(args.get("description") or "").strip()
    story_id = str(args.get("story_id") or "").strip()
    meta: Dict[str, Any] = {
        "business_line": args.get("business_line") or "",
        "priority": args.get("priority") or "",
        "work_item_status": args.get("status") or "",
    }

    if story_id.isdigit():
        try:
            plan = pm_schedule_risk.plan_of(int(story_id))
        except pm_project_mcp.McpError as e:
            return ToolOutcome(content=f"飞书项目拉取失败：{e}", is_error=True)
        title = title or plan.name
        meta.update({
            "work_item_id": story_id,
            "name": plan.name,
            "work_item_status": plan.stage,
        })
        try:
            snap = pm_story_source.stories_by_ids([int(story_id)]).get(int(story_id))
            if snap:
                meta["business_line"] = meta["business_line"] or snap.line
                meta["priority"] = meta["priority"] or snap.priority
        except Exception:
            pass
        if not desc:
            # 子任务清单就是这条需求的技术要点，比让人临时编描述准
            desc = "\n".join(
                f"{i}. {t}" for i, t in enumerate(plan.subtasks, 1)
            )

    if not title:
        return ToolOutcome(content="title 为空", is_error=True)
    if not desc:
        return ToolOutcome(
            content="没有可估的内容",
            render=(
                "只有标题估不了 —— 模型看的是需求内容（业务规则、范围、跨系统依赖），"
                "光一个标题它会给出一个没意义的数。给我 PRD 链接或者需求要点。"
            ),
            is_error=True,
        )

    claimed = args.get("claimed_days")
    try:
        claimed = float(claimed) if claimed is not None else None
    except (TypeError, ValueError):
        claimed = None

    res = estimate_prd_effort(
        title=title[:60], description=desc, claimed_days=claimed, meta=meta
    )
    if not res.get("success"):
        return ToolOutcome(
            content=json.dumps(res, ensure_ascii=False),
            render=res.get("message") or "估不出来。",
            is_error=True,
        )
    advice = res.get("pmo_advice") or ""
    detail = "\n".join(
        f"· {k} = {v}" for k, v in (res.get("features") or {}).items()
    )
    return ToolOutcome(content=json.dumps(res, ensure_ascii=False), render=f"{advice}\n\n{detail}")


def _run_capture_note(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    content = str(args.get("content") or "").strip()
    if not content:
        return ToolOutcome(content="content 为空", is_error=True)

    project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
    log_dir = os.path.join(project_dir, "daily-log")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{date.today().isoformat()}.md")
    stamp = datetime.now().strftime("%H:%M")

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- **[{stamp}]**: {content}\n")
    except Exception as e:
        logger.exception("写日志失败")
        return ToolOutcome(content=f"写日志失败：{e}", is_error=True)

    return ToolOutcome(content=f"已记到 {path}")


def _run_recall(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """查以前记过的东西 —— 流水、快存、知识库一起搜。

    为什么要有这个工具
    -----------------
    记和查必须成对。只做 `pm_capture_note` 不做这个，
    等于给他一个只能往里扔、扔进去就再也摸不到的口袋。
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolOutcome(content="query 为空", is_error=True)

    try:
        days = int(args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30

    lines: List[str] = []

    try:
        notes = search_notes(query, days=days)
    except Exception:
        logger.exception("检索笔记失败")
        notes = []
    if notes:
        lines.append(f"以前记过的（近 {days} 天）：")
        for n in notes:
            lines.append(f"· {n['when']}  {n['file']}")
            for s in n["snippets"]:
                lines.append(f"    {s}")

    try:
        docs = search_knowledge_base(query)
    except Exception:
        logger.exception("检索知识库失败")
        docs = []
    if docs:
        if lines:
            lines.append("")
        lines.append("知识库里的相关条目：")
        for d in docs:
            lines.append(f"· {d['file_name']}  ({d['rel_path']})")
            for s in d["snippets"][:2]:
                lines.append(f"    {s}")

    if not lines:
        return ToolOutcome(
            content=f"没找到与「{query}」相关的记录或文档。",
            render=f"没找到跟「{query}」有关的记录。要不要现在说一遍，我记下来？",
        )

    body = "\n".join(lines)
    return ToolOutcome(content=body, render=body[:2600])


def _run_whats_new(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """最近的感知事件。

    与 pm_recall 的区别：recall 查的是「我记过的东西」，
    这个查的是「机器人自己盯到的东西」。他问「今天群里有什么」用这个。
    """
    try:
        days = int(args.get("days") or 1)
    except (TypeError, ValueError):
        days = 1

    only_waiting = bool(args.get("only_waiting"))
    scope = str(args.get("scope") or "").strip().lower()

    events = pm_awareness.recent_events(days=days, only_waiting=only_waiting)
    if scope in ("p2p", "group"):
        events = [e for e in events if e.chat_mode == scope]

    if not events:
        meta = pm_awareness.snapshot_meta()
        window = "今天" if days <= 1 else f"最近 {days} 天"
        if not meta["swept_at"]:
            msg = "我还没做过全量扫描，现在什么都不知道。发「/过审」让我先扫一遍。"
        else:
            msg = f"{window}没盯到跟你相关的消息（上次全量扫：{meta['swept_at']}）。"
        return ToolOutcome(content=msg, render=msg)

    head = "今天盯到的" if days <= 1 else f"最近 {days} 天盯到的"
    if only_waiting:
        head += "（还没回的）"
    lines = [f"{head} {len(events)} 条："]

    for i, e in enumerate(events, 1):
        who = e.sender if e.chat_mode == "p2p" else f"{e.chat_name} · {e.sender}"
        lines.append(f"{i}. {e.when} {who}")
        lines.append(f"   {pm_awareness.clip_text(e.text, 100)}")
        tail = e.status
        if e.reason:
            tail += f"｜{e.reason}"
        lines.append(f"   〔{tail}〕")

    # 登记现场，下一句「第 3 条」「那个王五的」才指代得上
    if ctx.session:
        ctx.session.remember(events=[
            {
                "no": i,
                "chat": e.chat_name,
                "who": e.sender,
                "digest": pm_awareness.clip_text(e.text, 40),
            }
            for i, e in enumerate(events, 1)
        ])

    text = "\n".join(lines)
    return ToolOutcome(content=text, render=text)


def _run_my_todos(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8

    text = pm_awareness.render_todos(limit=limit)
    if not text:
        return ToolOutcome(
            content="待办池是空的。",
            render="待办池是空的，没欠着的事。",
        )

    if ctx.session:
        ctx.session.remember(todos=[
            {"no": i, "chat": t.get("chat_name"), "text": str(t.get("text"))[:40]}
            for i, t in enumerate(pm_awareness.open_todos()[:limit], 1)
        ])
    return ToolOutcome(content=text, render=text)


def _run_close_todos(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    raw = args.get("indices")
    if raw is None:
        return ToolOutcome(content="indices 为空", is_error=True)
    if isinstance(raw, (str, int)):
        raw = [raw]

    try:
        wanted = [int(x) for x in raw]
    except (TypeError, ValueError):
        return ToolOutcome(content=f"indices 不是数字：{raw}", is_error=True)

    res = pm_awareness.close_todos(wanted)
    if res.get("error"):
        return ToolOutcome(content=res["error"], render=res["error"], is_error=True)

    closed = res.get("closed") or []
    lines = [f"划掉了 {len(closed)} 条："]
    lines.extend(f"· {c}" for c in closed)
    rest = pm_awareness.render_todos()
    if rest:
        lines.append("")
        lines.append(rest)
    else:
        lines.append("")
        lines.append("待办池清空了。")
    text = "\n".join(lines)
    if ctx.session:
        ctx.session.forget("todos")
    return ToolOutcome(content=text, render=text)


def _run_story_pull(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """按指令拉飞书项目里的需求版本信息。

    他说「11.26 的需求」「拉一下票据那版」时走这里。
    数据源是 story，不是工作簿 —— 工作簿只做视图，不再当事实来源。
    """
    keyword = (args.get("keyword") or "").strip()
    if not keyword:
        return ToolOutcome(
            content="需要迭代关键字",
            render="给我个版本关键字，比如「11.26」「票据 v3.4.1」，我去飞书项目拉。",
            is_error=True,
        )

    only_scope = args.get("only_scope", True)
    try:
        iters = pm_story_source.resolve(keyword)
        if not iters:
            return ToolOutcome(
                content=f"没找到名字里带 {keyword} 的迭代",
                render=f"飞书项目里没有名字带「{keyword}」的迭代，是不是版本号写法不一样？",
            )
        stories = pm_story_source.stories_of([i.name for i in iters])
    except pm_project_mcp.McpError as e:
        logger.warning("拉 story 失败: %s", e)
        return ToolOutcome(
            content=f"飞书项目拉取失败：{e}",
            render=f"飞书项目这边没拉通：{e}\n稍后再试，或者确认下 MCP 通道。",
            is_error=True,
        )
    except Exception as e:
        logger.exception("拉 story 异常")
        return ToolOutcome(content=f"拉取异常：{e}", is_error=True)

    total = len(stories)
    if only_scope:
        stories = [s for s in stories if pm_scope.in_scope(f"{s.name} {s.line}")]
    dropped = total - len(stories)

    if not stories:
        return ToolOutcome(
            content=f"{keyword} 下 {total} 条需求，没有落在我负责范围的",
            render=f"「{keyword}」这版飞书项目里 {total} 条需求，没有公募/私募/ESOP/票据相关的。",
        )

    pending = [s for s in stories if not s.handed_over]
    done = [s for s in stories if s.handed_over]
    show_done = bool(args.get("include_done"))

    names = "、".join(i.name for i in iters)
    if len(iters) > 2:
        names = f"{len(iters)} 个迭代"

    def _clip(title: str) -> str:
        return title if len(title) <= 40 else title[:39] + "…"

    lines = [f"「{keyword}」→ {names}，你负责的 {len(stories)} 条", ""]
    if pending:
        lines.append(f"还没交的 {len(pending)} 条")
        for s in sorted(pending, key=lambda x: x.stage):
            who = f"　@{s.operator}" if s.operator else "　（无负责人，要指人）"
            lines.append(f"· {_clip(s.name)}　{s.stage}{who}")
        # 卡在他自己手上的单独点出来：这些不会有人来催，得他自己动
        mine = [s for s in pending if pm_nudge.ME in (s.operator or "")]
        if mine:
            lines.append(f"　（其中 {len(mine)} 条当前负责人是你，等你这边的动作）")
        lines.append("")
    else:
        lines.append("这批全都交付了。")
        lines.append("")

    if done and show_done:
        lines.append(f"已经交了的 {len(done)} 条")
        for s in done:
            lines.append(f"· {_clip(s.name)}　{s.stage}" + (f"　@{s.operator}" if s.operator else ""))
        lines.append("")
    elif done:
        lines.append(f"另有 {len(done)} 条已交付，要看得说一声。")

    if dropped:
        lines.append(f"不在你范围的 {dropped} 条没列。")

    text = "\n".join(lines).rstrip()
    return ToolOutcome(content=text, render=text)


def _run_followup_status(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    try:
        items = pm_followup.poll()
    except Exception as e:
        logger.exception("回读跟进状态失败")
        return ToolOutcome(
            content=f"回读失败：{type(e).__name__}: {e}",
            render="回读飞书项目失败，稍后再看。",
            is_error=True,
        )

    if not items:
        return ToolOutcome(
            content="没有在跟的问询，或者都还没到该说话的时候",
            render="暂时没有新动静 —— 问出去的都还在等人回，没到该追的时候。",
        )

    text = pm_followup.render(items)
    return ToolOutcome(content=text, render=text)


def _run_schedule_risk(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """排期体检：给结论、风险和判断，不是列字段。

    和 pm_story_pull 的分工：那个回答「这版有哪些需求」，这个回答
    「这版能不能按期发、断在哪、该动什么」。他要的是后者时别调错。
    """
    target = (args.get("target") or "").strip()
    if not target:
        return ToolOutcome(
            content="需要 target（迭代关键字 / 需求名 / story id）",
            render="给我个版本关键字或者需求名，我去看排期。比如「V2.43.8」「11.26」「私募数据审核平台」。",
            is_error=True,
        )
    try:
        if args.get("rules_only"):
            text = pm_schedule_risk.report(target)
        else:
            text = pm_schedule_risk.analyse(target)
    except pm_project_mcp.McpError as e:
        logger.warning("排期分析拉取失败: %s", e)
        return ToolOutcome(
            content=f"飞书项目拉取失败：{e}",
            render=f"飞书项目这边没拉通：{e}\n稍后再试，或者确认下 MCP 通道。",
            is_error=True,
        )
    except Exception as e:
        logger.exception("排期分析异常")
        return ToolOutcome(content=f"排期分析异常：{type(e).__name__}: {e}", is_error=True)
    return ToolOutcome(content=text, render=text)


def _run_kb_gaps(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """知识库缺口：该补但还没补的东西。"""

    action = (args.get("action") or "list").strip()

    if action == "close":
        topic = (args.get("topic") or "").strip()
        if not topic:
            return ToolOutcome(
                content="close 需要 topic",
                render="要关哪一块？说个名字，比如「人力地图」。",
                is_error=True,
            )
        ok = pm_kb_gaps.close(topic)
        text = f"「{topic}」标成补完了。" if ok else f"台账里没有未关闭的「{topic}」。"
        return ToolOutcome(content=text, render=text)

    if action == "add":
        topic = (args.get("topic") or "").strip()
        why = (args.get("why") or "").strip()
        if not topic:
            return ToolOutcome(content="add 需要 topic", render="要记哪一块？", is_error=True)
        gap = pm_kb_gaps.add(topic, why or "（他要求记下，理由没细说）")
        text = f"记下了：{gap.topic}。不急，等你给材料。"
        return ToolOutcome(content=text, render=text)

    text = pm_kb_gaps.render()
    return ToolOutcome(content=text, render=text)


_SETTLE_WORD = {
    "done": "办完了",
    "ignore": "设成不管了",
    "snooze": "先放放",
    "reopen": "重新捡回来",
}


def _one_line_title(text: str, limit: int = 22) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _settle_thread(args: Dict[str, Any], target: str) -> ToolOutcome:
    """他表个态：办完了 / 别管了 / 先放放 / 捡回来。

    这是「跨天记得住」的写入端。写完之后 _build() 每次都会把它折进判断，
    所以第二天、下周、重启 daemon，都不会再冒出来 —— 除非是 snooze 到期。
    """
    if not target:
        return ToolOutcome(
            content="需要 target",
            render="哪件事？给个版本号或需求名，比如「1126」「私募数据审核平台」。",
            is_error=True,
        )

    action = (args.get("settle") or "").strip()
    if action not in _SETTLE_WORD:
        return ToolOutcome(
            content=f"未知处置 {action}",
            render="你想怎么处理？办完了 / 别管了 / 先放放 / 捡回来。",
            is_error=True,
        )

    t = pm_thread.resolve(target)
    if not t:
        return ToolOutcome(
            content=f"没认出来：{target}",
            render=f"「{target}」没对上哪件事。试试版本号（11.26）或需求全名。",
            is_error=True,
        )

    # snooze 可以指定放到哪天。不指定就用 pm_thread 的默认天数。
    until = None
    raw_until = (args.get("until") or "").strip()
    if raw_until:
        try:
            d = datetime.strptime(raw_until, "%Y-%m-%d").date()
        except ValueError:
            return ToolOutcome(
                content=f"日期格式不对：{raw_until}",
                render="日期写成 2026-11-10 这样。",
                is_error=True,
            )
        until = datetime.combine(d, datetime.min.time()).timestamp() * 1000

    if not pm_thread.settle(t.key, action, until=until):
        return ToolOutcome(
            content="写入失败",
            render="没记上，再说一次？",
            is_error=True,
        )

    name = _one_line_title(t.title)
    if action == "snooze":
        when = (
            datetime.fromtimestamp(until / 1000).strftime("%m-%d")
            if until else f"{pm_thread.SNOOZE_DEFAULT_DAYS} 天后"
        )
        render = f"「{name}」先放着，{when} 再提。"
    elif action == "reopen":
        render = f"「{name}」捡回来了，明天的清单里会重新出现。"
    else:
        render = f"「{name}」{_SETTLE_WORD[action]}，以后不再提醒这件事。"

    return ToolOutcome(content=render, render=render)


def _run_thread(args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    """「这件事怎么样了」—— 跨来源的那条时间线。

    和 pm_schedule_risk 的分工：那个只看排期能不能按期；这个把群聊、会议、
    节点、待办摊在同一条线上，回答「来龙去脉」。
    """
    action = (args.get("action") or "timeline").strip()
    target = (args.get("target") or "").strip()

    if action == "queue":
        text = pm_thread.queue()
        return ToolOutcome(content=text or "（没有事）", render=text or "今天没攒下什么事。")

    if action == "settle":
        return _settle_thread(args, target)

    if not target:
        return ToolOutcome(
            content="需要 target",
            render="问哪件事？给个版本号或需求名，比如「1126」「私募数据审核平台」。",
            is_error=True,
        )

    t = pm_thread.resolve(target)
    if not t:
        return ToolOutcome(
            content=f"没认出来：{target}",
            render=f"「{target}」没对上哪件事。试试版本号（11.26）或需求全名。",
            is_error=True,
        )
    text = pm_thread.timeline(t)
    return ToolOutcome(content=text, render=text)


# ══════════════════════════════════════════════════════════════════════
# 工具清单
# ══════════════════════════════════════════════════════════════════════

TOOLS: List[Tool] = [
    Tool(
        name="pm_daily_brief",
        description=(
            "生成今天的完整工作计划视图，合并研发轨（版本计划里的需求）与运营轨"
            "（每周计划跟踪里的事项）。用户问「今天干什么」「今日计划」「有什么活」时调用。"
        ),
        parameters={"type": "object", "properties": {}},
        run=_run_daily_brief,
    ),
    Tool(
        name="pm_query_radar",
        description=(
            "查询版本排期、距上线还剩几个工作日、当前落在 SOP 哪道闸门。\n"
            "传 version 查单个版本（如 10.29）；不传则返回所有在跟版本的快照。\n"
            "用户问「谁卡着」「有什么风险」「哪些需求要提测了」时，传 blockers=true，"
            "只返回卡点清单，不会起草任何话术。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "version": {"type": "string", "description": "版本号，如 10.29、11.05。可省略。"},
                "blockers": {
                    "type": "boolean",
                    "description": "true 表示只要卡点清单（谁、卡在什么状态、为什么），不要排期详情",
                },
            },
        },
        run=_run_radar,
    ),
    Tool(
        name="pm_draft_nudges",
        description=(
            "起草给责任人的催办话术，会进入待确认状态，用户改完说「确认」才算定稿。\n"
            "**只在用户明确要求催办时用**：说了「催一下」「催办」「帮我问问谁提测没」「写个话术」。\n"
            "用户只是问「谁卡着」「有什么风险」时，不要用这个工具，改用 pm_query_radar 的 blockers 模式。\n"
            "scope 传版本/迭代关键字（11.26、票据）时，会按飞书项目里每条需求的「当前负责人」挑人，\n"
            "比工作簿那列更准 —— 那种场景优先传 scope。\n"
            "注意：本工具只生成草稿，无法真正发送给对方。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "限定范围：版本号/迭代关键字（11.26、票据）或人名。可省略。",
                },
            },
        },
        run=_run_draft_nudges,
    ),
    Tool(
        name="pm_log_plan",
        description=(
            "把用户今天/明天要做的工作项录入系统并同步到多维表格「日计划」。"
            "todos 必须由你从用户原话里拆成原子工作项，不要编造、不要保留「今日计划」这类指令壳。"
            "与 SOP 闸门相关的填 gate（如提测→G5 提测准入），无关的留空。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "工作项名称，保留用户原话"},
                            "date_hint": {"type": "string", "description": "today / tomorrow / YYYY-MM-DD"},
                            "gate": {
                                "type": "string",
                                "description": "G1 需求冻结 / G2 方案评审 / G3 承接承诺 / G4 联调就绪 / G5 提测准入 / G6 代码冻结 / G7 发版投产，无则留空",
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["todos"],
        },
        run=_run_log_plan,
    ),
    Tool(
        name="pm_update_weekly",
        description=(
            "修改「每周计划跟踪」工作表里事项的状态。状态只能是 未开始/进行中/已完成/阻塞。"
            "会先返回预览，用户说「确认」才真正写入表格。\n"
            "用户说「3 完成了」「私募周报阻塞了」时调用。"
            "**用户一次提多个编号（如「5 7 已完成」）时，把编号用空格拼在 target 里一次传入**，"
            "不要拆成多次调用 —— 本工具会一次生成合并预览、一次确认全部落笔。\n"
            "编号就是 09:00 日计划推送里每行前面的数字（如「5. 公私募1029版本需求上线」），"
            "直接原样传即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "序号或多个序号（如「3」「5 7」），也可直接给事项名（如 私募周报）",
                },
                "status": {
                    "type": "string",
                    "enum": ["未开始", "进行中", "已完成", "阻塞"],
                    "description": "目标状态",
                },
            },
            "required": ["target", "status"],
        },
        run=_run_update_weekly,
    ),
    Tool(
        name="pm_query_people",
        description="在人物协作地图里查模块负责人、AB 角、审批归属，回答「这个模块找谁」。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，如 WebPortal 前端、董泓林、票据业务"},
            },
            "required": ["query"],
        },
        run=_run_query_people,
    ),
    Tool(
        name="pm_search_knowledge",
        description="检索组织级知识库（L1 业务概念 / L2 流程操作 / L3 经验决策）与 SOP 规范。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，如 发版流程、提测准入、踩坑"},
            },
            "required": ["query"],
        },
        run=_run_search_knowledge,
    ),
    Tool(
        name="pm_estimate_effort",
        description=(
            "用组织标准的 effort-efficiency skill 估需求研发人天。\n"
            "算法：PRD 质量分 Q（v2.4）+ 复杂度 C（四维）+ 可信度，"
            "喂给预训练的 12 特征模型（不现场训练），输出 AI 预测人天和可信度。\n"
            "他说「这个需求要开发多久」「评估下工时」「要几个人天」「这个估得准不准」时调用。\n"
            "**优先传 story_id**：会自动拉飞书项目里这条需求的子任务清单当技术要点，"
            "比让他临时口述描述准。子任务清单已经拆得够细时，这就是最好的输入。\n"
            "也可以传 title + description（PRD 正文或要点）。\n"
            "claimed_days 传研发填报的人天，会给出偏差判断（偏高/偏紧/合理）。\n"
            "注意：可信度低（<85%）时必须告诉用户这是参考值，"
            "因为输入不是完整 PRD 时模型只能给个量级。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "story_id": {
                    "type": "string",
                    "description": "飞书项目 story id，优先用它 —— 会自动取子任务清单当描述",
                },
                "title": {"type": "string", "description": "需求标题"},
                "description": {"type": "string", "description": "PRD 正文或技术要点"},
                "claimed_days": {"type": "number", "description": "研发报审的人天，可省略"},
                "business_line": {"type": "string", "description": "业务线，影响可信度事前修正"},
                "priority": {"type": "string", "description": "优先级，影响可信度事前修正"},
            },
        },
        run=_run_estimate,
    ),
    Tool(
        name="pm_capture_note",
        description=(
            "把用户口述的备忘、复盘、临时结论记到当天工作日志里。\n"
            "他说「记一下」「帮我记住」「备忘」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记录的内容，保留用户原话"},
            },
            "required": ["content"],
        },
        run=_run_capture_note,
    ),
    Tool(
        name="pm_recall",
        description=(
            "查以前记过的东西：工作日志流水、快存区、组织知识库（L1/L2/L3）与 SOP。\n"
            "他说「上次说的」「之前记的」「我们以前怎么定的」「有没有相关文档」时调用。\n"
            "时间不确定就把 days 放大（比如 90）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，空格分隔多个词"},
                "days": {"type": "integer", "description": "回溯多少天，默认 30"},
            },
            "required": ["query"],
        },
        run=_run_recall,
    ),
    Tool(
        name="pm_whats_new",
        description=(
            "查机器人自己盯到的事：群聊、私聊里跟他相关的消息，什么时候、谁说的、他回没回。\n"
            "他说「今天群里有什么」「有什么动静」「谁找我了」「有没有我漏掉的」时调用。\n"
            "与 pm_recall 不同：recall 查他自己记过的和知识库，这个查实时感知到的会话。\n"
            "只管展示事实，不要顺手起草催办 —— 他要催会明说。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回溯几天，默认 1（今天）"},
                "only_waiting": {
                    "type": "boolean",
                    "description": "true 只列他还没回的，用于「我漏了什么」",
                },
                "scope": {
                    "type": "string",
                    "enum": ["p2p", "group"],
                    "description": "只看私聊或只看群聊，省略则都要",
                },
            },
        },
        run=_run_whats_new,
    ),
    Tool(
        name="pm_my_todos",
        description=(
            "查待办池：过审时确认要跟进、但还没做完的事。\n"
            "他说「我还欠什么」「有什么没做」「待办」「有没有挂着的」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多列几条，默认 8"},
            },
        },
        run=_run_my_todos,
    ),
    Tool(
        name="pm_close_todos",
        description=(
            "把待办池里的事项标记为做完。indices 是 pm_my_todos 列表里的序号。\n"
            "他说「第 2 条做完了」「都清掉」「这个不用跟了」时调用。\n"
            "编号对不上时工具会列出当前清单，不要自己猜编号。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "要划掉的序号，如 [2] 或 [1,3]",
                },
            },
            "required": ["indices"],
        },
        run=_run_close_todos,
    ),
    Tool(
        name="pm_story_pull",
        description=(
            "从飞书项目（story）拉某个版本/迭代的需求清单：需求名、状态、当前卡在谁手上。\n"
            "这是他维护需求的唯一源 —— 工作簿里的需求行不再手工抄，一律以这里为准。\n"
            "他说「11.26 的需求出来了」「拉一下票据那版」「这版有哪些」「谁还没提测」时调用。\n"
            "keyword 用版本片段即可（11.26 / 票据 / v3.4.1），会自动匹配到真实迭代名。\n"
            "默认按他的职责范围（公募/私募/ESOP/票据）过滤，其余不列。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "迭代/版本关键字，如 11.26、票据、v3.4.1",
                },
                "only_scope": {
                    "type": "boolean",
                    "description": "true（默认）只列他负责的业务线；false 列全部",
                },
                "include_done": {
                    "type": "boolean",
                    "description": "true 连已交付的也逐条列；默认 false，已交付只报个数",
                },
            },
            "required": ["keyword"],
        },
        run=_run_story_pull,
    ),
    Tool(
        name="pm_followup_status",
        description=(
            "查「问出去之后有没有下文」：谁已提测、谁换了负责人、谁问了几天没动静。\n"
            "数据来自飞书项目 story 的状态回读，不是猜聊天内容。\n"
            "他说「上次问的有没有回」「谁提测了」「跟进一下」时调用。\n"
            "提测的那条会直接指出现在该问谁（当前负责人），不用再查。"
        ),
        parameters={"type": "object", "properties": {}},
        run=_run_followup_status,
    ),
    Tool(
        name="pm_schedule_risk",
        description=(
            "排期体检：判断某个版本或某条需求能不能按期上线，给出结论、风险和判断，"
            "而不是罗列字段。\n"
            "会看：节点排期是否断链（实现完了但 SIT/UAT/上线没排）、"
            "当前节点是否越过迭代发版日、是否已逾期、节点上有没有挂人力、"
            "子任务的规模与跨系统联调比例、工时口径是否自洽。\n"
            "默认会基于这些事实给一段判断（最可能出问题的地方、为什么、他该做什么）；"
            "分析层连不上时自动退回纯规则结论，并注明。\n"
            "他说「分析下他们的排期」「这版能不能赶上」「这条排到什么时候」"
            "「你的意见呢」「有什么排期风险」时调用。\n"
            "target 可以是迭代关键字（11.26 / V2.43.8 / 票据）、需求名片段、"
            "或 story id。命中多个需求时会列出来让他挑。\n"
            "注意：这个工具只做判断，不起草任何话术，也不发消息。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "迭代关键字、需求名片段，或 story id",
                },
                "rules_only": {
                    "type": "boolean",
                    "description": "true 只给规则算出来的事实与风险，不要判断。"
                                   "他要「核对一下数据」时用；要「你的意见」时留空。",
                },
            },
            "required": ["target"],
        },
        run=_run_schedule_risk,
    ),
    Tool(
        name="pm_kb_gaps",
        description=(
            "知识库缺口台账：该补但现在还没有的东西（流程、地图、口径文档）。\n"
            "他说「还缺什么」「待补充有哪些」「知识库还差什么」时调用（action=list）。\n"
            "他说「XX 先不急，记一下」「这个要补上」时用 action=add 登记。\n"
            "他说「XX 补好了」「人力地图我发你了」时用 action=close 关掉。\n"
            "这些东西没有截止日，别塞进待办池。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "close"],
                    "description": "list 查看（默认）、add 登记、close 关闭",
                },
                "topic": {"type": "string", "description": "缺口的名字，如「人力地图」"},
                "why": {"type": "string", "description": "为什么要补（add 时用）"},
            },
        },
        run=_run_kb_gaps,
    ),
    Tool(
        name="pm_thread",
        description=(
            "「一件事」的跨来源时间线 —— 把群聊、会议纪要、story 节点、日计划待办\n"
            "摊在同一条线上。\n"
            "他说「1126 那个需求怎么样了」「这件事来龙去脉」「私募那条现在什么情况」时用 action=timeline。\n"
            "action=queue 看今天所有事的分档（需要出手 / 等他回 / 知道一下）。\n"
            "action=settle 记他的处置：他说「这件办完了」「这条别管了」「下周再说」「捡回来」时调用，\n"
            "写进去之后这件事就不会再出现在每日清单里（snooze 到期才重新出现）。\n"
            "和 pm_schedule_risk 的区别：那个判「能不能按期发」，这个答「发生了什么」。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["timeline", "queue", "settle"],
                    "description": "timeline 看一件事（默认）、queue 看今天的全景、settle 记处置",
                },
                "target": {
                    "type": "string",
                    "description": "版本号或需求名，如 1126、私募数据审核平台。queue 时可省略",
                },
                "settle": {
                    "type": "string",
                    "enum": ["done", "ignore", "snooze", "reopen"],
                    "description": (
                        "done 办完了 / ignore 别管了以后不再提 / "
                        "snooze 先放放到期再提 / reopen 撤销前面的处置捡回来。action=settle 时必填"
                    ),
                },
                "until": {
                    "type": "string",
                    "description": "snooze 到哪天，格式 2026-11-10。不填默认 3 天后",
                },
            },
        },
        run=_run_thread,
    ),
]

_BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}


def spec() -> List[Dict[str, Any]]:
    """转成 OpenAI tools 格式，给 LLM 用。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS
    ]


def execute(name: str, args: Dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    tool = _BY_NAME.get(name)
    if not tool:
        return ToolOutcome(content=f"未知工具 {name}", is_error=True)
    try:
        return tool.run(args or {}, ctx)
    except Exception as e:
        logger.exception("工具执行异常 name=%s args=%s", name, args)
        return ToolOutcome(content=f"工具 {name} 执行异常：{type(e).__name__}: {e}", is_error=True)