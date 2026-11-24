"""PM Todo Intake - 每日待办入库与防御性盲区扫描

职责边界
--------
本模块**不做语义理解**。切词、意图判断全部由 pm_intent_router 完成，
这里只负责：

    list[TodoItem] ──► 盲区扫描 ──► 写飞书多维表格「日计划」

历史缺陷与修正
--------------
旧版把未命中规则的条目统一打上 "日清待办" 标签并渲染给用户，
于是群里出现过：

    • [日清待办] **同步日计划：**
    • [日清待办] **私募周报**
    • [日清待办] **story排期**

"日清待办" 是一个内部兜底常量，不是业务概念，不该出现在任何面向用户的输出里。
现在 gate 只有两种状态：命中 SOP 闸门（G1~G7）或 None。None 就不渲染任何前缀。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.domains.pm.feishu_pm_sync import (
    BASE_TOKEN,
    TABLE_ID,
    _run_lark_cli,
    fetch_existing_records,
)
from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm.pm_intent_router import TodoItem, normalize_todos
from app.domains.pm.pm_config import owner_name

logger = logging.getLogger("pm_hub.todo_intake")


# ══════════════════════════════════════════════════════════════════════
# 防御性盲区规则库（SOP V2.3 闸门对照）
# ══════════════════════════════════════════════════════════════════════

DEFENSIVE_BLINDSPOT_RULES: List[Dict[str, Any]] = [
    {
        "pattern": r"(提测|测试|自测|转测|送测)",
        "gate": "G5 提测准入",
        "blindspots": [
            "【自测证据】开发是否已在需求卡片/工单中提交核心流程自测截图与冒烟通过记录？",
            "【用例覆盖】测试团队是否已完成用例评审，用例基线是否已锁定？",
            "【环境与配置】测试环境服务、第三方 mock 服务及新增配置项是否已推送到 SIT？",
        ],
    },
    {
        "pattern": r"(联调|接口|前后端|接入)",
        "gate": "G4 联调就绪",
        "blindspots": [
            "【UI走查与资源】前端使用的最终切图与动效资源是否已与 UI/UE 走查定稿？",
            "【跨模块协议】接口协议（Swagger/Postman）是否有未向下游通知的破坏性字段变更？",
        ],
    },
    {
        "pattern": r"(上线|发版|发包|投产|灰度)",
        "gate": "G7 发版投产",
        "blindspots": [
            "【配置清单】生产环境 DB DDL 变更脚本、新增配置项清单是否已提交运维与安全审核？",
            "【回滚预案】发版失败或异常时的版本降级与数据回滚方案是否已与研发负责人确认？",
            "【业务通告】运营与业务端是否已完成发版停机/更新公告的审核与发布？",
        ],
    },
    {
        "pattern": r"(评审|PRD|需求确认)",
        "gate": "G2 方案评审",
        "blindspots": [
            "【研发排期敲定】需求基线锁定后，各端研发（前端、后端、测试）是否均已在工作簿锁定工期？",
            "【依赖排查】是否存在外部系统依赖（银行接口、核心交易接口、三方身份认证）？",
        ],
    },
]


# ══════════════════════════════════════════════════════════════════════
# 兜底切分（LLM 不可用时）
# ══════════════════════════════════════════════════════════════════════

def parse_raw_todos(raw_text: str) -> List[TodoItem]:
    """把口语化文本粗暴切分成待办条目。

    这是 LLM 路由失败时的兜底路径，不做语义理解，只按分隔符切。
    归一化（去序号/去指令壳/去重）统一交给 router.normalize_todos。
    """
    normalized = raw_text.replace("；", "\n").replace(";", "\n")
    normalized = re.sub(r"([，,]?\s*)([0-9]+[、\.])", r"\n\2", normalized)

    lines = [l.strip() for l in normalized.splitlines() if l.strip()]
    return normalize_todos([{"title": l, "date_hint": "today"} for l in lines])


# ══════════════════════════════════════════════════════════════════════
# 盲区扫描
# ══════════════════════════════════════════════════════════════════════

def scan_blindspots(
    todos: List[TodoItem],
    radar: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """对待办逐条做盲区扫描。

    倒计时类提醒（alerts）在全局去重：
    旧实现里 2 条待办命中同一个临近版本，就会输出 2 条一模一样的
    "【发版倒计时】[10.29] 距上线仅剩 1 个工作日"，群里看着像复读机。
    """
    imminent_versions = []
    if radar:
        imminent_versions = [
            v for v in radar.get("active_versions", [])
            if v.get("workdays_left", 999) <= 6
        ]

    results: List[Dict[str, Any]] = []
    emitted_alerts: set[str] = set()   # 全局去重，同一条提醒只说一次

    for todo in todos:
        item_blindspots: List[str] = []
        for rule in DEFENSIVE_BLINDSPOT_RULES:
            if re.search(rule["pattern"], todo.title):
                item_blindspots.extend(rule["blindspots"])

        alerts: List[str] = []
        for iv in imminent_versions:
            ver_name = iv["version"]
            # 只有待办明确提到该版本，或待办本身带闸门时才提醒，
            # 避免"私募周报"这种日常事务也背上发版倒计时
            mentioned = ver_name in todo.title
            if not (mentioned or todo.gate):
                continue
            msg = (
                f"【发版倒计时】[{ver_name}] 距上线仅剩 {iv['workdays_left']} 个工作日"
                f"（目标: {iv['target_date']}），偏差需在今日 17:00 前收口"
            )
            if msg in emitted_alerts:
                continue
            emitted_alerts.add(msg)
            alerts.append(msg)

        results.append({
            "title": todo.title,
            "gate": todo.gate,
            "blindspots": item_blindspots,
            "alerts": alerts,
        })

    return results


def _fetch_radar() -> Optional[Dict[str, Any]]:
    """读取当前版本雷达（失败不影响主流程）。"""
    try:
        vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
        return evaluate_daily_radar(parse_stories(vals), today=date.today())
    except Exception as e:
        logger.warning("获取雷达失败，仅按规则扫描: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════
# 写多维表格
# ══════════════════════════════════════════════════════════════════════

def _plan_ts(todo: TodoItem, today_dt: date) -> int:
    """按 date_hint 算出「计划日期」（当天 09:00 的毫秒值）。

    2026-11-02 17:42 的教训：他说「明天：1、跟进1112需求提测情况…」，
    4 条全被记成了当天（date.today() 硬编码）。第二天晨报按「今天」去读，
    一条都不在窗口里 —— 在他看来就是「我录了，机器没看见」。

    date_hint 由 pm_intent_router 抽取，取值 today / tomorrow / YYYY-MM-DD。
    认不出来就退回今天：宁可日期不准，也不能因为格式怪就丢掉这条待办。
    """
    target = today_dt
    hint = (todo.date_hint or "today").strip()
    if hint == "tomorrow":
        target = today_dt + timedelta(days=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", hint):
        try:
            target = date.fromisoformat(hint)
        except ValueError:
            target = today_dt
    return int(
        datetime(target.year, target.month, target.day, 9, 0, 0).timestamp() * 1000
    )


def _build_record(todo: TodoItem, ts_ms: int) -> Dict[str, Any]:
    """构造单条多维表格记录。

    「质量闸门」是单选字段：gate 为 None 时**不写该字段**，
    而不是塞一个"日清待办"进去——后者会污染看板筛选。
    """
    fields: Dict[str, Any] = {
        "任务": todo.title,
        "计划日期": ts_ms,
        "状态": "进行中",
        "闸门结论": "待检查",
        "负责人": owner_name(),
        "备注": "【自然语言摄入】经 PMO 外骨骼盲区扫描录入",
    }
    if todo.gate:
        fields["质量闸门"] = todo.gate
    return {"fields": fields}


def _sync_to_bitable(todos: List[TodoItem]) -> Dict[str, int]:
    """逐条写入多维表格，返回成功/总数。"""
    today_dt = date.today()

    created = 0
    for todo in todos:
        rec = _build_record(todo, _plan_ts(todo, today_dt))
        cmd = [
            "api", "POST",
            f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
            "--data", "-",
            "--as", "user",
        ]
        try:
            _run_lark_cli(cmd, input_data=json.dumps(rec, ensure_ascii=False))
            created += 1
        except Exception as e:
            logger.error("写入手动待办失败: %s | rec=%s", e, rec)

    return {"created": created, "total": len(todos)}


# 手动录入的记录都带这个标记；自动生成的研发轨任务不带。
# 靠它把「他自己录的」和「机器按 SOP 生成的」分开 ——
# 不分开的话，晨报里同一条研发任务会出现两次（今日计划 + 版本闸门）。
_INTAKE_MARK = "【自然语言摄入】"
_DONE_STATUS = ("已完成", "完成", "已关闭")


def planned_items(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """某一天他自己录的计划。

    晨报原来只有两个来源：sht001 的「本周未完成」和 sht000 的版本闸门。
    「今天打算做什么」这一问没人回答 —— 他 11-02 录的 4 项到 11-03 早上
    一个字都没出现，这条就是补那一块。

    读不到返回空列表：日计划是附加信息，不能因为它挂了拖垮整份晨报。
    """
    today = today or date.today()
    try:
        records = fetch_existing_records()
    except Exception as e:
        logger.warning("读日计划表失败（晨报将不含今日计划）: %s", e)
        return []

    start = datetime(today.year, today.month, today.day).timestamp() * 1000
    end = start + 24 * 3600 * 1000

    out: List[Dict[str, Any]] = []
    for r in records:
        f = r.get("fields") or {}
        ts = f.get("计划日期")
        if not isinstance(ts, (int, float)) or not (start <= ts < end):
            continue
        if _INTAKE_MARK not in (f.get("备注") or ""):
            continue
        if (f.get("状态") or "") in _DONE_STATUS:
            continue
        title = (f.get("任务") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "gate": f.get("质量闸门") or "",
            "status": f.get("状态") or "",
            "record_id": r.get("record_id") or "",
        })
    return out


# 业务说法 → 字段选项。状态是单选，写不存在的值接口会直接拒，
# 所以必须翻译：他只会说「完成」/「阻塞」。
_STATUS_MAP = {
    "完成": "已完成", "已完成": "已完成", "做完": "已完成", "done": "已完成",
    "阻塞": "阻塞", "卡住": "阻塞", "卡了": "阻塞",
    "进行中": "进行中", "在做": "进行中", "开始": "进行中",
}


def normalize_status(status: str) -> Optional[str]:
    """业务说法 → 字段选项；认不出返回 None。

    单独暴露出来，是为了调用方能在写表之前先校验 ——
    否则一批记录会全写失败，只看到「没改成」却不知道为什么。
    """
    s = (status or "").strip()
    return _STATUS_MAP.get(s) or _STATUS_MAP.get(s.lower())


def set_status(record_id: str, status: str) -> bool:
    """回写日计划表某条记录的状态。认不出状态就返回 False，由调用方提示。"""
    value = normalize_status(status)
    if not value or not record_id:
        return False

    cmd = [
        "api", "PUT",
        f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/{record_id}",
        "--data", "-", "--as", "user",
    ]
    try:
        res = _run_lark_cli(
            cmd, input_data=json.dumps({"fields": {"状态": value}}, ensure_ascii=False)
        )
        return bool(res.get("ok"))
    except Exception as e:
        logger.error("回写日计划状态失败 %s: %s", record_id, e)
        return False


# ══════════════════════════════════════════════════════════════════════
# 对外主入口
# ══════════════════════════════════════════════════════════════════════

def intake_structured_todos(
    todos: List[TodoItem],
    auto_sync_bitable: bool = True,
) -> Dict[str, Any]:
    """主入口：已结构化的待办 → 盲区扫描 → 写表。

    这是 pm_daemon 应该调用的函数。语义理解在上游 router 已做完。
    """
    if not todos:
        return {
            "success": False,
            "message": "未识别出有效工作项。",
            "todos": [],
            "blindspots": [],
            "sync_result": None,
        }

    radar = _fetch_radar()
    blindspot_report = scan_blindspots(todos, radar)
    sync_res = _sync_to_bitable(todos) if auto_sync_bitable else None

    return {
        "success": True,
        "todos": todos,
        "blindspots": blindspot_report,
        "sync_result": sync_res,
    }


def intake_and_sync_todos(raw_text: str, auto_sync_bitable: bool = True) -> Dict[str, Any]:
    """兼容入口：直接接受原始文本（LLM 不可用时的兜底路径）。

    新代码应优先使用 intake_structured_todos。
    """
    return intake_structured_todos(parse_raw_todos(raw_text), auto_sync_bitable)