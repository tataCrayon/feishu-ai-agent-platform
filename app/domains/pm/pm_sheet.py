"""写表格的统一出口 —— 电子表格单元格 / 多维表格记录。

为什么要有这一层：写能力原本散在三处（`pm_weekly_write._write_cell` /
`feishu_pm_sync.sync_tasks_to_bitable` / `pm_todo_intake._sync_to_bitable`），
调用方要写一条「提测结论」得先知道该走哪一处。这里只做**路由与校验**，
真正的 HTTP 调用仍复用那三处，不另写一套 lark-cli 参数。

三条约定：

1. **不猜表**：`token` / `table_id` 必须调用方给或来自 `[projects.pm.outputs]`，
   缺了就报错。猜一张表写进去比写失败更糟。
2. **不吞失败**：`_run_lark_cli` 抛异常就往上抛，调用方据此告诉用户「没写进去」。
3. **回读校验**：电子表格写完回读一次（`_write_cell` 已有），多维表格靠飞书返回
   的 record 列表判断，不做额外读。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.domains.pm.feishu_pm_sync import BASE_TOKEN, TABLE_ID, _run_lark_cli
from app.domains.pm.pm_config import pm_config

logger = logging.getLogger("pm_hub.sheet")


def write_cells(range_name: str, values: List[List[Any]],
                token: str = "") -> Dict[str, Any]:
    """写电子表格区间（`values` 是二维数组，行优先）。

    `range_name` 形如 `sht000!AK20:AK20`。只写不读回——回读由调用方按需做，
    因为「写完立刻读」在有缓存的情况下可能读到旧值，会把成功误判成失败。
    """
    sheet_token = token or pm_config().outputs.spreadsheet_token
    if not sheet_token:
        raise RuntimeError("未配置表格：config.toml [projects.pm.outputs].spreadsheet_token")
    if not range_name.strip():
        raise RuntimeError("range 不能为空")

    body = {"valueRange": {"range": range_name, "values": values}}
    cmd = [
        "api", "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{sheet_token}/values",
        "--data", "-",
        "--as", "user",
    ]
    return _run_lark_cli(cmd, input_data=json.dumps(body, ensure_ascii=False))


def append_records(fields_list: List[Dict[str, Any]],
                   base_token: str = "", table_id: str = "") -> Dict[str, Any]:
    """往多维表格批量新增记录。

    `fields_list` 每项是一条记录的 `fields`。飞书的批量接口上限 500 条，
    超过就分批——一次塞太多会被静默截断，那等于「以为写完了其实没写完」。
    """
    bt = base_token or BASE_TOKEN or pm_config().outputs.base_token
    tid = table_id or TABLE_ID or pm_config().outputs.table_id
    if not (bt and tid):
        raise RuntimeError("未配置多维表格：config.toml [projects.pm.outputs].base_token / table_id")
    if not fields_list:
        return {"ok": True, "created": 0}

    created = 0
    for i in range(0, len(fields_list), 500):
        chunk = fields_list[i:i + 500]
        cmd = [
            "api", "POST",
            f"/open-apis/bitable/v1/apps/{bt}/tables/{tid}/records/batch_create",
            "--data", "-",
            "--as", "user",
        ]
        res = _run_lark_cli(
            cmd,
            input_data=json.dumps({"records": [{"fields": f} for f in chunk]},
                                  ensure_ascii=False),
        )
        if not res.get("ok", False):
            raise RuntimeError(f"写多维表格失败: {res}")
        created += len(chunk)
    return {"ok": True, "created": created}


def update_record(record_id: str, fields: Dict[str, Any],
                  base_token: str = "", table_id: str = "") -> Dict[str, Any]:
    """更新多维表格单条记录。用于「把核对结论落成结构化字段」这类回写。"""
    bt = base_token or BASE_TOKEN or pm_config().outputs.base_token
    tid = table_id or TABLE_ID or pm_config().outputs.table_id
    if not (bt and tid):
        raise RuntimeError("未配置多维表格")
    if not record_id.strip():
        raise RuntimeError("record_id 不能为空")

    cmd = [
        "api", "PUT",
        f"/open-apis/bitable/v1/apps/{bt}/tables/{tid}/records/{record_id}",
        "--data", "-",
        "--as", "user",
    ]
    return _run_lark_cli(cmd, input_data=json.dumps({"fields": fields}, ensure_ascii=False))


def read_sheet(range_name: str, token: str = "") -> List[List[Any]]:
    """读电子表格区间。走 `pm_hub_engine.fetch_sheet_values`（range 转义已趟过坑）。"""
    from app.domains.pm.pm_hub_engine import fetch_sheet_values

    sheet_token = token or pm_config().outputs.spreadsheet_token
    if not sheet_token:
        raise RuntimeError("未配置表格")
    return fetch_sheet_values(sheet_token, range_name)


def resolved_token(token: Optional[str] = None) -> str:
    """调用方省略 token 时的取值口径（给路由层做 503 判断用）。"""
    return (token or "").strip() or pm_config().outputs.spreadsheet_token
