"""独立工单 Agent 的 MCP 投影 — 代理转发 `order_agent.mcp_server`（stdio）。

负责：spawn 工单 Agent 仓的 MCP server 子进程、拉起一次会话、把工具调用原样转发、
把结果按本侧统一口径整形（JSON 字符串）。
不负责：工单→SQL 的任何引擎逻辑（案例检索/生成/校验/红线全在对方仓内，本文件零业务）。

调用方：dsh 按 `[projects.mcp.order_agent]` 以子进程挂载。

**为什么是代理而不是重新实现**：对方仓的工具契约（`run_ticket` / `ingest_order` /
`list_cases` / `query_production` / `ingest_feishu` / `sync_case` /
`set_case_mirror_ref` / `set_case_status` / `check_cases`）已稳定且带红线，双写必然漂移。
代理只持有「怎么起对方」这一份配置（解释器与工作目录来自 `[projects.mcp.order_agent]`
的 command/cwd），工具清单在启动时从对方 server 的 `tools/list` 动态获取。

**取舍**：每次工具调用起一次子进程 + MCP 握手（约 1~2s），而不是常驻长连接。
工单任务本身是分钟级 LLM 作业，握手开销可忽略；常驻则要处理子进程崩溃重连、
事件循环嵌套（FastMCP 自己的 anyio 循环里再开 stdio client）与孤儿清理，
风险大于收益。真需要常驻时改这里，不动工具语义。

**失败语义**：一律返回 `{"ok": false, "error": "..."}`，不抛异常；子进程起不来时
错误里带解释器与工作目录，便于直接定位配置问题。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP

from app.config import McpServerConfig
from app.mcp._common import app_config, fail, ok

logger = logging.getLogger("mcp.order_agent")

mcp = FastMCP("order_agent")

_TARGET_MODULE = "order_agent.mcp_server"


def _server_params(cfg: McpServerConfig) -> StdioServerParameters:
    """对方 server 的启动参数（解释器/工作目录缺省跟随本进程）。"""
    return StdioServerParameters(
        command=cfg.command or sys.executable,
        args=cfg.args or ["-m", _TARGET_MODULE],
        cwd=cfg.cwd or None,
    )

def _unwrap(result: Any) -> str:
    """MCP `CallToolResult` → 本侧回执。

    对方工具已返回「一个 JSON 字符串」；能解析则原样透出（工具契约一致），
    解析不了（纯文本/空结果）包成 ok 回执，避免调用方拿到两种形态。
    """
    texts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
    raw = "\n".join(t for t in texts if t)
    if getattr(result, "isError", False):
        return fail(f"order-agent 工具执行失败：{raw[:500]}")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ok(result=raw)
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    return ok(result=parsed)


async def call_order_tool(tool: str, arguments: dict[str, Any] | None = None) -> str:
    """在对方 server 上调用一个工具，返回本侧 JSON 回执字符串。"""
    cfg = app_config().mcp.order_agent
    if not cfg.enabled:
        return fail("工单 Agent 投影未启用（[projects.mcp.order_agent].enabled=false）")
    timeout = timedelta(seconds=cfg.timeout_sec) if cfg.timeout_sec > 0 else None
    params = _server_params(cfg)
    try:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                tool, arguments or {}, read_timeout_seconds=timeout)
            return _unwrap(result)
    except (OSError, RuntimeError, TimeoutError) as e:
        logger.warning("[mcp.order_agent] %s failed: %s", tool, e)
        return fail(
            f"工单 Agent 子进程不可用：{type(e).__name__}: {e}"
            f"（command={params.command!r} args={params.args!r} cwd={params.cwd!r}；"
            "请确认 webportal-order-agent 仓已安装依赖，并在 config.toml "
            "[projects.mcp.order_agent] 配好 command/cwd）"
        )


@mcp.tool()
async def run_ticket(text: str) -> str:
    """处理工单文本，依据已验证案例生成 SQL 供人工审核（不执行 SQL）。

    返回对方引擎的原始结果：命中案例=高可信 SQL；未命中=advisory 低可信参考建议。
    """
    return await call_order_tool("run_ticket", {"text": text})


@mcp.tool()
async def ingest_order(order_id: str) -> str:
    """从 CMDB 工单只读采集（仅 GET）生成 draft 案例；人工确认后改 verified 才参与检索。"""
    return await call_order_tool("ingest_order", {"order_id": order_id})


@mcp.tool()
async def list_cases(status: str = "verified") -> str:
    """列出知识库案例（默认 verified；status=draft 看待确认案例）。"""
    return await call_order_tool("list_cases", {"status": status})


@mcp.tool()
async def query_production(sql: str, db_name: str = "cfhkgwmcusrmydata") -> str:
    """生产只读查询（SELECT/SHOW/EXPLAIN/DESC），用于核对数据与回填参数。

    只读白名单在对方引擎内校验；结果供建议，不作为执行依据。
    """
    return await call_order_tool("query_production", {"sql": sql, "db_name": db_name})


@mcp.tool()
async def ingest_feishu(ref: str, body: str) -> str:
    """由调用方取好飞书文档正文后生成 draft 案例（引擎不碰飞书 API）。"""
    return await call_order_tool("ingest_feishu", {"ref": ref, "body": body})


@mcp.tool()
async def sync_case(case_id: str) -> str:
    """按 id 取现有案例内容与飞书镜像 markdown（本地文件是唯一事实源）。"""
    return await call_order_tool("sync_case", {"case_id": case_id})


@mcp.tool()
async def set_case_mirror_ref(case_id: str, url: str) -> str:
    """写回案例 ↔ 飞书镜像链接（缺这条映射案例不许改 verified）。"""
    return await call_order_tool("set_case_mirror_ref", {"case_id": case_id, "url": url})


@mcp.tool()
async def set_case_status(case_id: str, status: str) -> str:
    """切换案例 draft/verified（人工确认后的定稿入口；verified 需 mirror_ref 已写回）。"""
    return await call_order_tool("set_case_status", {"case_id": case_id, "status": status})


@mcp.tool()
async def check_cases() -> str:
    """校验案例一致性（占位符与 front-matter parameters 的对应关系等）。"""
    return await call_order_tool("check_cases", {})


if __name__ == "__main__":
    mcp.run()
