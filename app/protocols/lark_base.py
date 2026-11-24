"""多维表格（base 域）最小读写层 — 需求知识台账与 easyprd CR 表共用。

为什么单独一层：两条能力都要读同一批表，而 CLI 的输出形态**不止一种**——
- `+record-list --format json` 返回列矩阵（`data.data` 行数组 + `field_id_list`）；
- `+record-list --format ndjson` 返回**按字段名的行对象**，`record_id` 也在行里。

后者才是本层要的形态（矩阵还得自己点表头）。CLI 的 ndjson 默认**写文件**到 cwd，
所以这里统一「给 --output 到临时目录 → 读回 → 随临时目录删除」，避免仓库根堆出
`<table_id>_<时间戳>.ndjson`（实测默认输出落在 cwd）。

身份一律 `user`：bot 身份在 wiki/base 上常是 `lacks permission`，而 base 的读写
scope 本来就挂在用户授权下，不需要把应用加成协作者。
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from app.protocols.lark_cli import _run

logger = logging.getLogger("protocols.lark_base")

_READ_LIMIT_MAX = 2000


async def list_records(
    base_token: str, table_id: str, *,
    identity: str = "user", limit: int = _READ_LIMIT_MAX,
) -> list[dict[str, Any]]:
    """整表读成「按字段名的行对象」列表（每行含 `record_id`）。

    读失败返回空列表并记 warning：调用方都在降级路径上有既定行为
    （绑定回落台账、状态面板显示 unavailable），不该因为一张表读不到就抛。
    """
    if not base_token or not table_id:
        return []
    with tempfile.TemporaryDirectory(prefix="larkbase-") as tmp:
        out = Path(tmp) / "records.ndjson"
        try:
            manifest = await _run([
                "base", "+record-list",
                "--base-token", base_token, "--table-id", table_id,
                "--as", identity if identity in ("user", "bot") else "user",
                "--format", "ndjson",
                "--limit", str(max(1, min(_READ_LIMIT_MAX, limit))),
                "--output", str(out), "--overwrite",
            ])
        except Exception as e:
            logger.warning("base 读表失败(%s/%s): %s",
                           base_token[:8], table_id, str(e)[:160])
            return []
        path = Path(str(manifest.get("record_file") or out))
        if not path.is_file():
            logger.warning("base 读表未产出 ndjson(%s/%s)", base_token[:8], table_id)
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows


async def upsert_record(
    base_token: str, table_id: str, fields: dict[str, Any], *,
    record_id: str = "", identity: str = "user",
) -> str:
    """按 `record_id` 更新、留空则新建；返回 record_id（拿不到为空串）。

    **不做「按业务键自动 upsert」**：CLI 的 `+record-upsert` 也不做（帮助里明说
    "does not auto-upsert by business key"）。所以「先查再写」的责任明确留在调用方，
    避免这里被误当成幂等写。
    """
    if not base_token or not table_id:
        return ""
    args = ["base", "+record-upsert",
            "--base-token", base_token, "--table-id", table_id,
            "--json", json.dumps(fields, ensure_ascii=False),
            "--as", identity if identity in ("user", "bot") else "user",
            "--format", "json"]
    if record_id:
        args += ["--record-id", record_id]
    payload = await _run(args)
    data = payload.get("data") or {}
    rec = data.get("record") or {}
    return str(rec.get("record_id") or data.get("record_id") or record_id or "")


def cell_text(value: Any) -> str:
    """单元格 → 纯文本。

    base 的 text 字段可能被写成 markdown 链接（`[文字](url)`）、select 是数组、
    link 是 `[{"id": "rec_x"}]`。三者都不能直接 `str()` 给人看。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return " ".join(t for t in (cell_text(v) for v in value) if t).strip()
    if isinstance(value, dict):
        # link 值只有 id，不是给人看的文本 → 空串（要 id 请直接用 _link_ids）
        if "id" in value and "text" not in value:
            return ""
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def first_url(value: Any) -> str:
    """单元格里的**第一个** URL（`[文字](链接)` / 裸链接 / 纯 token 都认）。"""
    text = cell_text(value)
    if not text:
        return ""
    start = text.find("](")
    if start >= 0:
        inner = text[start + 2:].split(")", 1)[0].strip()
        if inner.startswith("http"):
            return inner
    for token in text.replace("(", " ").replace(")", " ").split():
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return text if text.startswith("http") else ""
