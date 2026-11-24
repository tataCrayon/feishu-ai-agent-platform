"""CMDB 工单提醒卡片渲染（纯函数，无 IO）。

版式对齐「授权操作通知」样式：字段名用业务语言、术语归位、不放口头语。

版式三段：
① header 标题 + 副标题（工单号 · 日期），颜色即事件语义
② 字段行：`**字段名：** 值`，每行一个事实，值里不再夹 emoji 与口语
③ 任务清单用原生 table（多任务时列对齐比逐行堆叠易扫读）；最后一句操作提示 + 工单链接

术语约定（全套卡片统一，改文案时照此翻译）：
| 内部值 | 卡片用语 |
|---|---|
| 执行成功 / 执行失败 / 已取消 | 成功 / 失败 / 已取消 |
| 未执行 | 待执行 |
| 执行中 | 执行中 |
| `PROD-FLP-HK-票据 (PROD-FLP)` | 原样（实例组是运维的定位信息，不做二次包装） |
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.domains.cmdb_watch.client import ENV_LABELS, ENV_ORDER_PAGES, ENV_PROD, ENV_UAT, Order, Task

TZ = timezone(timedelta(hours=8))

KIND_LABEL = {"sql": "SQL 变更", "deploy": "应用发布", "nacos": "配置变更",
              "cron": "定时任务"}

# 「我」在这条任务里的角色：没有配置读者名单时，任务创建人就是执行人
DEFAULT_ACTOR = "任务创建人"

# 每种事件一个头（颜色 + 标题 + 副标题模板），改文案只改这张表。
# 副标题只有「对象 + 编号 + 日期」这些定位信息，不放形容词。
_HEADERS: dict[str, tuple[str, str]] = {
    "order": ("green", "CMDB 生产工单提醒"),
    "started": ("blue", "CMDB 任务执行中"),
    "done": ("green", "CMDB 任务已完成"),
    "failed": ("red", "CMDB 任务执行失败"),
    "test": ("turquoise", "CMDB 工单提醒 · 链路自检"),
    # 指定工单关注：看的是整张单的进度，标题不要和「我的任务」混
    "watch_started": ("blue", "CMDB 工单开始执行"),
    "watch_done": ("green", "CMDB 工单执行完成"),
    "watch_done_failed": ("red", "CMDB 工单执行结束（有失败）"),
}

# CMDB 原始状态 → 卡片用语（长句收短，避免同一行里两种人称）
_STATE_TERMS = {
    "未执行": "待执行",
    "执行成功": "成功",
    "执行失败": "失败",
    "已取消任务": "已取消",
}

# 同一张卡最多列几行任务（再多折叠成计数 + 提示去工单页看）
_MAX_TASK_ROWS = 8


def order_url(order_id: int, base_url: str, env: str = ENV_PROD) -> str:
    """工单详情页链接。base_url 留空则返回空串（宁可没链接也不给个点不开的）。

    UAT 与生产是**两个不同的页面**（`UatOrderDetail` / `OrderDetail`），而且同号
    不代表同一张单——链接必须带对环境，否则点开看到的是另一张单。
    """
    if not base_url:
        return ""
    page = ENV_ORDER_PAGES.get(env or ENV_PROD, ENV_ORDER_PAGES[ENV_PROD])
    return f"{base_url.rstrip('/')}/order/{page}?order_id={order_id}"


def env_label(env: str) -> str:
    """卡片上的环境用语：`生产` / `UAT`。"""
    return ENV_LABELS.get(env or ENV_PROD, ENV_LABELS[ENV_PROD])


def _title(event: str, env: str) -> str:
    """事件标题 + 环境后缀。

    UAT 必须显式标出来：同一个「任务执行失败」在生产与 UAT 里的处置完全不同，
    卡面上不区分就等于让人自己猜。
    """
    template, title = _HEADERS.get(event, _HEADERS["started"])
    return f"{title} · UAT" if (env or ENV_PROD) == ENV_UAT else title


def _card() -> Any:
    """延迟 import：render 要能在没装 lark_channel 的纯单测环境跑（同 prd_watch）。"""
    from lark_channel import CardBuilder

    return CardBuilder()


def _now_text(when: datetime | None = None) -> str:
    now = when or datetime.now(TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S (CST)")


def _day_text(when: datetime | None = None) -> str:
    return (when or datetime.now(TZ)).strftime("%Y-%m-%d")


def state_term(task: Task) -> str:
    """CMDB 状态 → 卡片用语（未执行→待执行 等）。"""
    return _STATE_TERMS.get(task.state_label, task.state_label or "未知")


def task_kind(task: Task) -> str:
    return KIND_LABEL.get(task.kind, task.kind)


def short_order_name(order: Order) -> str:
    """工单标题去掉尾部日期（副标题里已有日期，正文重复一遍只会显得啰嗦）。"""
    name = (order.order_name or "（无标题工单）").strip()
    suffix = order.order_name.rstrip()[-10:] if order.order_name else ""
    if len(name) > 11 and suffix[:1].isdigit() and suffix[4:5] == "-" and suffix[7:8] == "-":
        return name[:-10].rstrip("_- ")
    return name


def _task_rows(tasks: list[Task]) -> list[list[str]]:
    """任务表格行：类型 / 编号 / 名称 / 状态 / 执行环境。"""
    return [[task_kind(t), f"#{t.task_id}", t.name or "—", state_term(t), t.env or "—"]
            for t in tasks]


def _task_table(b: Any, tasks: list[Task], *, page_size: int = 10) -> None:
    """原生 table：多任务时列对齐才扫得下去（逐行 markdown 在窄屏会折成三段）。"""
    b.table(["类型", "任务", "名称", "状态", "执行环境"], _task_rows(tasks[:page_size]),
            page_size=page_size, data_types=["text", "text", "text", "text", "lark_md"])


def _footer(b: Any, text: str, *, url: str = "") -> None:
    """底部一行操作提示 + 工单链接（与截图里「撤销历史授权」同一形态）。"""
    if url:
        b.markdown(f"{text} [在 CMDB 查看工单]({url})" if text
                   else f"[在 CMDB 查看工单]({url})")
    elif text:
        b.markdown(f"<font color='grey'>{text}</font>")


def build_order_card(order: Order, tasks: list[Task], *, base_url: str,
                     when: datetime | None = None,
                     actor: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """新工单卡片：命中任务清单一次性给全（>8 条折叠成计数）。"""
    template, _ = _HEADERS["order"]
    title = _title("order", order.env)
    now = when or datetime.now(TZ)
    b = _card()
    b.config(width_mode="fill")
    b.header(title, subtitle=f"#{order.order_id} · {_day_text(now)}", template=template)

    b.markdown("**工单名称：** " + (short_order_name(order)))
    b.markdown(f"**工单编号：** {order.order_id}")
    if (order.env or ENV_PROD) == ENV_UAT:
        b.markdown(f"**执行环境：** {env_label(order.env)}")
    b.markdown("**命中人员：** " + actor)
    all_pending = all(not t.running and not t.done for t in tasks)
    b.markdown("**命中任务：** " + (f"{len(tasks)} 项（待执行）" if all_pending
                                    else f"{len(tasks)} 项"))
    b.markdown("**任务清单：**")
    shown = tasks[:_MAX_TASK_ROWS]
    _task_table(b, shown)
    if len(tasks) > len(shown):
        b.markdown(f"<font color='grey'>另有 {len(tasks) - len(shown)} 项任务未展开，"
                   "请到工单页查看完整清单。</font>")

    _footer(b, f"提醒时间：{_now_text(now)}　·　任务开始与结束时会各提醒一次。",
            url=order_url(order.order_id, base_url, order.env))
    return dict(b.to_dict())


def build_task_card(task: Task, order: Order, event: str, *, base_url: str,
                    when: datetime | None = None) -> dict[str, Any]:
    """任务状态事件卡片。`event` ∈ {started, done, failed}。"""
    template, _ = _HEADERS.get(event, _HEADERS["started"])
    title = _title(event, order.env)
    now = when or datetime.now(TZ)
    b = _card()
    b.config(width_mode="fill")
    b.header(title, subtitle=f"#{order.order_id} · {task_kind(task)} #{task.task_id}",
             template=template)

    b.markdown("**工单名称：** " + short_order_name(order))
    b.markdown(f"**任务事项：** {task_kind(task)}　#{task.task_id}　{task.name or '—'}")
    b.markdown(f"**执行状态：** {state_term(task)}")
    if (order.env or ENV_PROD) == ENV_UAT:
        b.markdown(f"**工单环境：** {env_label(order.env)}")
    if task.env:
        b.markdown(f"**执行环境：** {task.env}")
    b.markdown(f"**任务创建人：** {task.creator or '未知'}")
    b.markdown(f"**提醒时间：** {_now_text(now)}")

    if event == "started":
        tail = "任务已开始执行，结束后会再次提醒。"
    elif event == "failed":
        tail = "**任务未能成功完成**，请到工单页核对执行日志与失败原因。"
    else:
        tail = "该任务已结束；同一工单中仍有你名下的任务时继续跟踪。"
    _footer(b, tail, url=order_url(order.order_id, base_url, order.env))
    return dict(b.to_dict())


def build_watch_card(order: Order, event: str, *, base_url: str,
                     subscriber: str = "", progress: str = "",
                     when: datetime | None = None) -> dict[str, Any]:
    """指定工单关注卡片：`event` ∈ {watch_started, watch_done, watch_done_failed}。

    与「我的任务」卡片的区别：这张看的是**整张单**——所有人的任务都列出来，
    因为用户关注的是「这张单什么时候发完」，而不是他自己那几条。
    「已开始关注」的确认走对话文字回执，不发卡（避免订阅时一次收到两条消息）。
    """
    template, _ = _HEADERS.get(event, _HEADERS["watch_started"])
    title = _title(event, order.env)
    now = when or datetime.now(TZ)
    b = _card()
    b.config(width_mode="fill")
    b.header(title, subtitle=f"#{order.order_id} · {_day_text(now)}", template=template)

    b.markdown("**工单名称：** " + short_order_name(order))
    b.markdown(f"**工单编号：** {order.order_id}")
    if (order.env or ENV_PROD) == ENV_UAT:
        b.markdown(f"**执行环境：** {env_label(order.env)}")
    if subscriber:
        b.markdown("**关注人：** " + subscriber)
    if progress:
        b.markdown("**执行进度：** " + progress)
    b.markdown("**任务清单：**")
    tasks = order.tasks
    shown = tasks[:_MAX_TASK_ROWS]
    _task_table(b, shown)
    if len(tasks) > len(shown):
        b.markdown(f"<font color='grey'>另有 {len(tasks) - len(shown)} 项任务未展开，"
                   "请到工单页查看完整清单。</font>")

    if event == "watch_started":
        tail = "工单已开始执行，全部任务结束后会再提醒一次。"
    elif event == "watch_done_failed":
        tail = "**工单已结束，但存在失败任务**，请到工单页核对执行日志。"
    else:
        tail = "工单内任务已全部结束。"
    _footer(b, f"提醒时间：{_now_text(now)}　·　{tail}",
            url=order_url(order.order_id, base_url, order.env))
    return dict(b.to_dict())


def build_test_card(*, base_url: str, users: list[str], chat_hint: str,
                    when: datetime | None = None) -> dict[str, Any]:
    """自检卡片：证明「配置解析 → 卡片渲染 → 飞书送达」这条链路是通的。"""
    template, title = _HEADERS["test"]
    now = when or datetime.now(TZ)
    b = _card()
    b.config(width_mode="fill")
    b.header(title, subtitle=_day_text(now), template=template)
    b.markdown(f"**发送时间：** {_now_text(now)}")
    b.markdown(f"**关注人员：** {'、'.join(users) if users else '（未配置）'}")
    b.markdown(f"**推送目标：** {chat_hint or '（未配置）'}")
    b.markdown(f"**CMDB 地址：** {base_url or '（未配置）'}")
    _footer(b, "本卡为链路自检，仅证明提醒通道可用；真实提醒只在有任务命中时发送。")
    return dict(b.to_dict())


def card_text(card: dict[str, Any]) -> str:
    """卡片里给人看的文本（单测断言与日志用，不钉结构）。

    递归取所有 markdown/plain_text 内容：结构一改（get 多一层）测试不该假失败，
    要断言的是**人眼会看到的那几行字**。
    """
    parts: list[str] = []

    def _visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _visit(item)
            return
        if isinstance(node, dict):
            if node.get("tag") in ("markdown", "plain_text") and "content" in node:
                parts.append(str(node["content"]))
            for key in ("elements", "columns", "header", "body", "rows"):
                if key in node:
                    _visit(node[key])
            if "title" in node:
                _visit(node["title"])
            # table 的行是 {"col_0": "值", ...} 这种平铺映射，键不是上面任何一类
            # —— 只有能直接取到 dict.values 时才算行（普通元素也符合 dict，靠 tag 排除）
            if node.get("tag") == "table":
                for row in node.get("rows") or []:
                    if isinstance(row, dict):
                        _visit(list(row.values()))
            return
        if isinstance(node, str):
            # table 的 row 是 {"col_0": "...", ...}，单元格文本也要算「人眼可见」
            parts.append(node)

    _visit(card)
    return "\n".join(parts)
