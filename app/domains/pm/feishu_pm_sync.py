"""PM Hub Bitable Sync - 自动同步每日闸门计划到飞书多维表格
目标多维表格:
Base Token: bascnExampleToken0000000000
Table ID: tblExampleTable001 (日计划)

字段映射:
- 任务 (fldlFWhunk): text, e.g. "[10.29] 锁定版本提测准入清单并确认自测记录"
- 计划日期 (fldDLcvoUI): datetime (timestamp ms)
- 质量闸门 (fldKB9ijQo): text, e.g. "G5 提测准入"
- 状态 (fldtZa5sGE): select, options: "未开始", "进行中", "已完成", "阻塞"
- 闸门结论 (fldzz1Gggu): select, options: "待检查", "通过", "未通过"
- 负责人 (fldu55NYaY): text, e.g. "李四 / 赵六"
- 备注 (fldTJb92xV): text
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.domains.pm.pm_config import owner_name, pm_config
from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)


_cfg = pm_config()
BASE_TOKEN = _cfg.outputs.base_token
TABLE_ID = _cfg.outputs.table_id


def _run_lark_cli(
    cmd: List[str], input_data: Optional[str] = None, timeout: int = 60,
    open_id: str = "",
) -> Dict[str, Any]:
    """调 lark-cli。

    **身份默认取 `[projects.pm].identity.open_id`**，走该用户独立的配置目录
    （见 `pm_identity`）。这样 20 个调用点一个都不用改，也不会因为别人登录过
    lark-cli 就变成「读别人的会话」。显式传 `open_id` 可覆盖（批量给多人跑时用）。

    timeout 不是可选项。2026-11-02 17:46 有一次调用没有返回，把整个感知线程
    挂死 35 分钟 —— 期间 18:00 的过审根本没触发，他那边看起来就是「机器人卡了」。
    node 启动 + 网络抖动叠加时，一次调用几十秒很正常；但绝不能无限等。
    """
    from app.domains.pm.pm_config import pm_config
    from app.domains.pm.pm_identity import run_cli

    who = (open_id or "").strip() or pm_config().identity.open_id
    return run_cli(cmd, open_id=who, input_data=input_data, timeout=timeout)


def fetch_existing_records() -> List[Dict[str, Any]]:
    """拉取多维表格当前已有记录"""
    cmd = [
        "api", "GET",
        f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
        "--params", json.dumps({"page_size": 100}),
        "--as", "user"
    ]
    res = _run_lark_cli(cmd)
    if not res.get("ok", False):
        raise RuntimeError(f"获取记录失败: {res}")
    return res.get("data", {}).get("items", [])


def generate_today_tasks(radar: Dict[str, Any], today_dt: date) -> List[Dict[str, Any]]:
    """基于今日雷达与各版本节拍生成今日日计划条目"""
    ts_ms = int(datetime(today_dt.year, today_dt.month, today_dt.day, 9, 0, 0).timestamp() * 1000)
    tasks: List[Dict[str, Any]] = []

    for v in radar.get("active_versions", []):
        ver = v["version"]
        days = v["workdays_left"]

        # 根据倒排工作日生成针对性闸门动作
        if days == 8 or days == 7 or days == 6:
            tasks.append({
                "fields": {
                    "任务": f"[{ver}] 联调与技术方案跟进，确认提测范围与风险",
                    "计划日期": ts_ms,
                    "质量闸门": "G4 方案/联调预备",
                    "状态": "进行中",
                    "闸门结论": "待检查",
                    "负责人": owner_name(),
                    "备注": f"距发版剩 {days} 个工作日，确认各端开发联调进展与外部依赖"
                }
            })
        elif days <= 5 and days > 1:
            tasks.append({
                "fields": {
                    "任务": f"[{ver}] 执行 G5 提测准入检查（自测+冒烟）",
                    "计划日期": ts_ms,
                    "质量闸门": "G5 提测准入",
                    "状态": "进行中",
                    "闸门结论": "待检查",
                    "负责人": owner_name(),
                    "备注": f"距发版剩 {days} 个工作日，进入测试主干"
                }
            })
        elif days == 1:
            tasks.append({
                "fields": {
                    "任务": f"[{ver}] G6 代码冻结与上线清单封版",
                    "计划日期": ts_ms,
                    "质量闸门": "G6 代码冻结",
                    "状态": "未开始",
                    "闸门结论": "待检查",
                    "负责人": owner_name(),
                    "备注": "SIT Bug清零，UAT验收通过，准备发版工单"
                }
            })

    # 针对缺失责任人的跟进任务
    if radar.get("follow_ups"):
        missing_titles = "、".join([f["title"] for f in radar["follow_ups"][:3]])
        tasks.append({
            "fields": {
                "任务": f"[排期维护] 补齐需求责任人（{missing_titles}等）",
                "计划日期": ts_ms,
                "质量闸门": "G1/G2 治理",
                "状态": "未开始",
                "闸门结论": "待检查",
                "负责人": owner_name(),
                "备注": "多条需求缺少开发/测试责任人，需向技术负责人确认"
            }
        })

    return tasks


def sync_tasks_to_bitable(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量向飞书多维表格写入或更新记录"""
    existing = fetch_existing_records()
    # 提取已有任务标题，避免单日重复插入
    existing_titles = set()
    empty_record_ids = []
    for item in existing:
        fields = item.get("fields", {})
        task_name = fields.get("任务")
        if not task_name:
            empty_record_ids.append(item.get("record_id"))
        else:
            if isinstance(task_name, list):
                # 富文本可能为 list
                t_str = "".join([seg.get("text", "") for seg in task_name])
                existing_titles.add(t_str)
            else:
                existing_titles.add(str(task_name))

    # 过滤出未创建的任务
    to_create = [t for t in tasks if t["fields"]["任务"] not in existing_titles]

    created_count = 0
    updated_count = 0

    for task_data in to_create:
        # 如果有原来留空的占位行，优先复用更新，否则新建
        if empty_record_ids:
            rec_id = empty_record_ids.pop(0)
            cmd = [
                "api", "PUT",
                f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/{rec_id}",
                "--data", "-",
                "--as", "user"
            ]
            body = {"fields": task_data["fields"]}
            _run_lark_cli(cmd, input_data=json.dumps(body, ensure_ascii=False))
            updated_count += 1
        else:
            cmd = [
                "api", "POST",
                f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
                "--data", "-",
                "--as", "user"
            ]
            body = {"fields": task_data["fields"]}
            _run_lark_cli(cmd, input_data=json.dumps(body, ensure_ascii=False))
            created_count += 1

    return {
        "total_tasks": len(tasks),
        "created": created_count,
        "updated": updated_count,
        "skipped": len(tasks) - len(to_create),
    }


if __name__ == "__main__":
    print("=== 开始执行多维表格自动同步 ===")
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    stories = parse_stories(vals)
    radar = evaluate_daily_radar(stories, today=date(2026, 10, 21))
    tasks = generate_today_tasks(radar, today_dt=date(2026, 10, 21))
    print(f"根据 SOP 计算出今日日计划任务: {len(tasks)} 条")
    for t in tasks:
        print(f" -> {t['fields']['任务']} | 闸门: {t['fields']['质量闸门']} | 负责人: {t['fields']['负责人']}")

    result = sync_tasks_to_bitable(tasks)
    print(f"同步完成: 新建 {result['created']} 条, 填充空行 {result['updated']} 条, 已存在跳过 {result['skipped']} 条")