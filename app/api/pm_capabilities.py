"""PM 能力层 HTTP 接口 — `/api/pm/*`，薄封装，不含业务判断。

**定位**：能力层不认识任何人，身份与表位置全部是入参或来自 `[projects.pm]` 配置。
这里只做「把 Python 函数变成 HTTP」，不做聚合、不排序、不渲染文案——那些属于调用方。

三条出口纪律（PRD 4.1 的硬约束）：

1. **不聚合**：不截断、不拼字符串、不按「今天要办」排序，返回原始结构。
2. **不 omit 空值**：`""` / `null` / `0.0` / `[]` 都显式出现。前端靠字段在不在判断能力。
3. **不重命名**：`text` 不叫 `content`，`ts` 不叫 `time`，`who` 不叫 `sender`——
   `.threads.jsonl` 里 1400+ 条历史数据就是这个名字。

单位统一毫秒；日期字符串一律 `YYYY-MM-DD`。

挂载方式：`app/dashboard.py` 里 `include_router(pm_router)`。本文件自带
`prefix="/api/pm"`，不要再外套一层前缀。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.domains.pm import pm_thread, pm_watch
from app.domains.pm.pm_config import pm_config
from app.domains.pm.pm_hub_engine import evaluate_daily_radar, fetch_sheet_values, parse_stories
from app.domains.pm.pm_people import name_of, open_id_of, resolve_many

logger = logging.getLogger("api.pm")

pm_router = APIRouter(prefix="/api/pm", tags=["pm"])


def _ensure_enabled() -> None:
    """未启用直接 503，而不是回一份空数据。

    空数组会被调用方当成「今天真的没事」，那是错误信息；503 至少说明能力没开。
    """
    if not pm_config().enabled:
        raise HTTPException(status_code=503,
                            detail="PM 能力未启用：config.toml [projects.pm].enabled = false")


def _parse_day(day: str) -> date:
    try:
        return date.fromisoformat(day) if day else date.today()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"day 需为 YYYY-MM-DD：{day!r}") from e


# ══════════════════════════════════════════════════════════════════════
# A. 消息事件
# ══════════════════════════════════════════════════════════════════════


@pm_router.get("/events")
def get_events(
    since: int = Query(0, description="毫秒时间戳，返回 ts 大于它的消息"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """拉会话增量（`pm_watch.poll` 的薄封装）。

    读会话用 user token（`lark-cli --as user`）——bot 只能读被拉进的群，
    而这里要的是全部。轮询有副作用（推进游标、更新快照），所以每次调用都会
    让下一次少拿到重复数据；`next_since` 是游标，调用方存下来即可。
    """
    _ensure_enabled()
    state = pm_watch.WatchState.load()
    events = pm_watch.poll(state, limit=limit, since_ms=since)
    state.save()
    rows = [asdict(e) for e in events]
    next_since = max((e.ts for e in events), default=since)
    return {"since": since, "next_since": next_since, "events": rows}


@pm_router.post("/scan")
def post_scan() -> dict[str, Any]:
    """全量扫描（`pm_watch.full_sweep`）——每天一次，5-7 分钟，别在请求里高频调。"""
    _ensure_enabled()
    snap = pm_watch.full_sweep()
    return {
        "date": snap.date,
        "events": snap.events,
        "hot": snap.hot,
        "full_swept_at": snap.full_swept_at,
        "stats": snap.stats,
    }


# ══════════════════════════════════════════════════════════════════════
# B. 事项
# ══════════════════════════════════════════════════════════════════════


def _thread_out(t: pm_thread.Thread) -> dict[str, Any]:
    """Thread → dict。

    `last_at` 是 `@property`，`asdict()` 拿不到——必须手动塞。
    漏了它调用方就没法按「最近动静」排序，而这是最常用的排序键。
    """
    row = asdict(t)
    row["last_at"] = t.last_at
    return row


@pm_router.get("/threads")
def get_threads(
    day: str = Query("", description="YYYY-MM-DD，缺省今天"),
    days_back: int = Query(1, ge=0, le=30),
) -> dict[str, Any]:
    """事项列表（`threads_of` 的结构化结果）。

    返回的是 `List[Thread]`，不是 `queue()` 渲染好的字符串——调用方要在它之上
    再做总结与归类，拿到拼接文本就没法做了。
    """
    _ensure_enabled()
    d = _parse_day(day)
    return {
        "day": d.isoformat(),
        "threads": [_thread_out(t) for t in pm_thread.threads_of(d, days_back)],
    }


@pm_router.get("/threads/{key}")
def get_thread(key: str, day: str = Query(""), days_back: int = Query(1, ge=0, le=30)) -> dict[str, Any]:
    """单条事项的时间线。`events` 原样返回，`settle` 类事件**不过滤**。"""
    _ensure_enabled()
    d = _parse_day(day)
    for t in pm_thread.threads_of(d, days_back):
        if t.key == key:
            return _thread_out(t)
    raise HTTPException(status_code=404, detail=f"没有这个事项：{key}")


class SettleIn(BaseModel):
    """处置载荷。action 语义见 `pm_thread.settle`：done / ignore / snooze / reopen。"""

    action: str = Field(..., description="done | ignore | snooze | reopen")
    until: Optional[float] = Field(None, description="毫秒时间戳，只对 snooze 有效")
    note: str = ""


# 白名单在这里再列一次是**有意重复**：`settle()` 遇到非法 action 只打 warning
# 返回 False，HTTP 层照抄就会变成「200 却什么都没写」——调用方以为处置成功了。
_SETTLE_ACTIONS = frozenset({"done", "ignore", "snooze", "reopen"})


@pm_router.post("/threads/{key}/settle")
def post_settle(key: str, body: SettleIn) -> dict[str, Any]:
    """处置一件事。

    `settle` 只校验 action 合法与 key 非空，**不校验 key 是否存在**——
    传错 key 会写一条指向不存在事项的孤儿记录，fold 时被静默丢弃。
    这个保证由调用方负，所以这里把 `written` 显式返回。
    """
    _ensure_enabled()
    if body.action not in _SETTLE_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"action 必须是 {'/'.join(sorted(_SETTLE_ACTIONS))}，收到 {body.action!r}")
    written = pm_thread.settle(key, body.action, until=body.until, note=body.note)
    return {"key": key, "action": body.action, "written": written}


@pm_router.get("/backlog")
def get_backlog(day: str = Query(""), days_back: int = Query(1, ge=0, le=30)) -> dict[str, Any]:
    """待确认池（`backlog_items`，已是 `List[dict]`，原样返回）。"""
    _ensure_enabled()
    return {"items": pm_thread.backlog_items(_parse_day(day), days_back)}


# ══════════════════════════════════════════════════════════════════════
# C. 表格 / 雷达 / 人名
# ══════════════════════════════════════════════════════════════════════


@pm_router.get("/sheet")
def get_sheet(range: str = Query(..., description="形如 sht000!A1:N25"),
              token: str = Query("")) -> dict[str, Any]:
    """读表格区间。`token` 缺省取配置里的版本排期表。"""
    _ensure_enabled()
    sheet_token = token or pm_config().outputs.spreadsheet_token
    if not sheet_token:
        raise HTTPException(status_code=503,
                            detail="未配置表格：config.toml [projects.pm.outputs].spreadsheet_token")
    return {"token": sheet_token, "range": range,
            "values": fetch_sheet_values(sheet_token, range)}


@pm_router.get("/radar")
def get_radar() -> dict[str, Any]:
    """G1-G7 闸门雷达（`evaluate_daily_radar` 的原始 report）。

    发不发、发给谁、什么时候发由调用方决定——这里只给数据。
    `warnings[].workdays_left` 是独立字段：文案里虽然也拼了「剩N工作日」，
    但下游要按天数排序/过滤时不该对句子再正则一次。
    """
    _ensure_enabled()
    cfg = pm_config()
    if not (cfg.outputs.spreadsheet_token and cfg.outputs.sheet_id_versions):
        raise HTTPException(status_code=503,
                            detail="未配置版本排期表：config.toml [projects.pm.outputs]")
    vals = fetch_sheet_values(cfg.outputs.spreadsheet_token,
                              f"{cfg.outputs.sheet_id_versions}!A1:N60")
    return evaluate_daily_radar(parse_stories(vals), today=date.today())


class SheetWriteIn(BaseModel):
    """写电子表格。`values` 是二维数组（行优先），`range` 形如 `sht000!AK20:AK20`。"""

    range: str = Field(..., description="表格区间")
    values: list[list[Any]] = Field(..., description="二维数组，行优先")
    token: str = ""


@pm_router.post("/sheet")
def post_sheet(body: SheetWriteIn) -> dict[str, Any]:
    """写电子表格单元格 / 追加行。

    写能力原本散在三处（`pm_weekly_write._write_cell` / `feishu_pm_sync` /
    `pm_todo_intake`），这里收敛成一个出口：调用方不必知道该调哪个模块。
    """
    _ensure_enabled()
    from app.domains.pm.pm_sheet import write_cells

    try:
        res = write_cells(body.range, body.values, body.token)
    except Exception as e:
        logger.warning("[api.pm] 写表失败: %s", e)
        raise HTTPException(status_code=502, detail=f"写表失败：{e}") from e
    return {"range": body.range, "ok": bool(res.get("ok", True))}


class RecordsIn(BaseModel):
    """写多维表格。`records` 每项是该条记录的 `fields`。"""

    records: list[dict[str, Any]] = Field(..., description="每条记录的 fields 列表")
    base_token: str = ""
    table_id: str = ""


@pm_router.post("/records")
def post_records(body: RecordsIn) -> dict[str, Any]:
    """多维表格批量新增记录。

    给 ⑩「上线前检查单」与 ①「能不能提测」这类结论一个落点——核对完的结论
    只留在聊天记录里，三处对同一件事各说各话，就是这个口子要解决的问题。
    """
    _ensure_enabled()
    from app.domains.pm.pm_sheet import append_records

    try:
        res = append_records(body.records, body.base_token, body.table_id)
    except Exception as e:
        logger.warning("[api.pm] 写多维表格失败: %s", e)
        raise HTTPException(status_code=502, detail=f"写多维表格失败：{e}") from e
    return {"created": res.get("created", 0)}


class RecordUpdateIn(BaseModel):
    fields: dict[str, Any] = Field(..., description="要更新的字段")
    base_token: str = ""
    table_id: str = ""


@pm_router.put("/records/{record_id}")
def put_record(record_id: str, body: RecordUpdateIn) -> dict[str, Any]:
    """更新多维表格单条记录（按 `record_id`）。"""
    _ensure_enabled()
    from app.domains.pm.pm_sheet import update_record

    try:
        res = update_record(record_id, body.fields, body.base_token, body.table_id)
    except Exception as e:
        logger.warning("[api.pm] 更新记录失败: %s", e)
        raise HTTPException(status_code=502, detail=f"更新记录失败：{e}") from e
    return {"record_id": record_id, "ok": bool(res.get("ok", True))}


@pm_router.get("/doc")
async def get_doc(doc_url: str = Query(..., description="飞书文档/wiki 链接或 token"),
                  max_chars: int = Query(4000, ge=200, le=20000)) -> dict[str, Any]:
    """读飞书文档正文（走 lark-cli 的 user 身份，需本机已授权）。

    **必须是 async**：`LarkCli.get_doc_content` 是协程，同步路由里调它拿到的
    是未 await 的协程对象，返回体会变成空字典而不是正文。
    """
    _ensure_enabled()
    from app.domains.pm.pm_docs import read_doc_async

    try:
        return await read_doc_async(doc_url, max_chars=max_chars)
    except Exception as e:
        logger.warning("[api.pm] 读文档失败: %s", e)
        raise HTTPException(status_code=502, detail=f"读文档失败：{e}") from e


def _minutes_module():
    from app.domains.pm import pm_meeting_sensor

    return pm_meeting_sensor


@pm_router.get("/minutes")
def get_minutes(days: int = Query(1, ge=1, le=30),
                limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """列最近 N 天的妙记（只要列表，不抽取行动项——抽取是调用方的事）。"""
    _ensure_enabled()
    try:
        items = _minutes_module().fetch_recent_minutes(days=days, limit=limit)
    except Exception as e:
        logger.warning("[api.pm] 拉妙记列表失败: %s", e)
        raise HTTPException(status_code=502, detail=f"拉妙记列表失败：{e}") from e
    return {"days": days, "count": len(items), "items": items}


@pm_router.get("/minutes/{token}")
def get_minute(token: str) -> dict[str, Any]:
    """读某场妙记的原文详情（`fetch_minute_detail` 原样透出）。"""
    _ensure_enabled()
    try:
        return _minutes_module().fetch_minute_detail(token)
    except Exception as e:
        logger.warning("[api.pm] 拉妙记详情失败: %s", e)
        raise HTTPException(status_code=502, detail=f"拉妙记详情失败：{e}") from e


@pm_router.get("/nudge-plan")
def get_nudge_plan(day: str = Query("")) -> dict[str, Any]:
    """今天该发的提测询问清单，**不发送**。

    给调用方看「节拍今天有没有东西要发」。真正发送仍是 `pm_nudge.stage` →
    用户确认 → `commit` 那条两段式链路，这里不开口子。
    """
    _ensure_enabled()
    from app.domains.pm import pm_nudge

    d = _parse_day(day)
    plan = pm_nudge.pending_ask_plan(d)
    return {
        "day": d.isoformat(),
        "ask_days_window": [x.isoformat() for x in pm_nudge.ask_days(d)],
        "plan": plan,
    }


# ══════════════════════════════════════════════════════════════════════
# E. 人名 / 通讯录
# ══════════════════════════════════════════════════════════════════════


@pm_router.get("/people")
def get_people(
    ids: str = Query("", description="逗号分隔的 open_id"),
    name: str = Query("", description="按名字查 open_id"),
) -> dict[str, Any]:
    """人名/部门/反向查 open_id。

    「发给张三」时手上只有名字、没有 open_id——这个转换不能等到聊天时才现算。
    """
    _ensure_enabled()
    if name.strip():
        oid = open_id_of(name.strip())
        return {"name": name.strip(), "open_id": oid}
    id_list = [x.strip() for x in ids.split(",") if x.strip()]
    if not id_list:
        return {"people": []}
    resolved = resolve_many(id_list)
    return {"people": [
        {
            "open_id": oid,
            "name": name_of(oid),
            # `resolve_many` 每项形如 {"name": ..., "dept": ...}；缺 dept 时回落空串，
            # 不省略字段（前端靠字段在不在判断能力，不靠 undefined）。
            "dept": (resolved.get(oid) or {}).get("dept", ""),
        }
        for oid in id_list
    ]}


# ══════════════════════════════════════════════════════════════════════
# D. 卡片原语 —— 只管发/改，卡片内容由调用方给
# ══════════════════════════════════════════════════════════════════════
#
# PRD 4.2 的三条：send_card / update_card / 回调分发。
# - send_card 早就有（`app/protocols/feishu_send.py::send_card`，`ou_`/`oc_` 都收）；
# - update_card 本次新增（PATCH /im/v1/messages/{id}，就地改不掉记录）；
# - 回调分发在 `app/main.py` 的 `channel.on_card_action`，本层不重复注册。


class CardIn(BaseModel):
    """卡片载荷。**不校验内容**——卡片长什么样是调用方的事，这里只转交。"""

    target: str = Field(..., description="oc_ 群 或 ou_ 用户 open_id")
    card: dict[str, Any] = Field(..., description="飞书交互卡片 JSON")


@pm_router.post("/cards")
def post_card(body: CardIn) -> dict[str, Any]:
    """发卡片给群或私聊。

    `target` 按前缀选 `receive_id_type`，所以私聊卡片这条路是通的
    （老实现 `pm_confirm_card.send_interactive_card` 只接 chat_id）。
    """
    _ensure_enabled()
    from app.protocols.feishu_send import FeishuSendError, send_card

    if not body.target.strip():
        raise HTTPException(status_code=400, detail="target 不能为空")
    try:
        mid = send_card(body.target.strip(), body.card)
    except FeishuSendError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"message_id": mid, "target": body.target.strip()}


@pm_router.patch("/cards/{message_id}")
def patch_card(message_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """就地更新已发出的卡片。请求体直接是卡片 JSON。"""
    _ensure_enabled()
    from app.protocols.feishu_send import FeishuSendError, update_card

    try:
        update_card(message_id, body)
    except FeishuSendError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"message_id": message_id, "updated": True}
