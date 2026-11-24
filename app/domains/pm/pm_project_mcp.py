"""飞书项目（Meego）MCP 传输层 —— 统一走官方端点，不依赖任何本机 IDE 配置。

职责边界：
    只做协议：握手、发 tools/call、把返回的 content 解成 dict。
    不懂 story、不懂迭代、不懂业务。领域语义全部在 pm_story_source.py。

为什么不再读 `~/.cursor/mcp.json`：
    那是**某个人的 Cursor** 才有的文件，换台机器、换个 IDE 就没了 —— 事实上
    本机就没有，PM 能力层持续报「找不到 mcp.json」，看起来像没权限，其实是
    数据源选错了。真正的通道官方一直开着：`https://project.feishu.cn/mcp_server/v1`
    （`prd_watch/meegle.py` 用的就是它，同一份 `m-` 用户凭证，无需任何本机文件）。

为什么直接复用 `prd_watch.meegle.MeegleClient` 而不是再抄一份：
    两个包都要连同一个端点、解析同一种 SSE 帧、踩同一批坑（token 过期判定、
    raw_text 错误形态）。抄第二份的代价是「修一处忘一处」。传输层与领域层本就
    应该分层：meegle 只管协议，这边只管 story/迭代语义。

项目标识：MQL 里写 `examplecorp.story`（空间名），API 的 `project_key` 参数传说空间名
         或 24 位 project_key 都通（实测），统一用 `examplecorp` 免得维护两套常量。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.domains.prd_watch.meegle import MeegleClient, MeegleError

PROJECT_KEY = "examplecorp"          # 飞书项目空间标识，story/迭代都在这下面


class McpError(RuntimeError):
    """MCP 通道或远端业务报错。调用方据此决定「告诉用户没拉到」而不是瞎猜。"""


_client: Optional[MeegleClient] = None


def connect() -> MeegleClient:
    """进程内复用一条 JSON-RPC 会话。"""
    global _client
    if _client is None:
        _client = MeegleClient(simple_name=PROJECT_KEY)
    return _client


def reset() -> None:
    """丢掉会话（token 换新 / 排障重连用）。"""
    global _client
    _client = None


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """调一个 MCP 工具，返回解码后的 dict。

    异常口径统一成 `McpError`：上游是 `MeegleError`（凭证/网络/业务错），
    所有调用方捕获的都是同一个 `McpError`，不用记住两套类名。
    """
    try:
        return connect().call(name, arguments)
    except MeegleError as e:
        raise McpError(str(e)) from e


def search_mql(statement: str) -> List[Dict[str, Any]]:
    """跑一条 MQL，返回 `data` 里的全部行（已过首页/翻页）。

    单次最多 50 条，`LIMIT n` 只截断不翻页（官方 schema 原文）。要翻页必须
    拿首页的 `session_id` + `list[0].group_infos[].group_id`，按
    `group_pagination_list` 逐页取。不翻页的后果是**静默少数据**：
    139 个迭代只回 50 个，看起来「迭代不存在」而不是「没翻页」。

    结果是 `moql_field_list` 原始行，字段解码在调用方（`pm_story_source`）。
    """
    body = call_tool("search_by_mql", {"project_key": PROJECT_KEY, "mql": statement})
    rows = _rows_of(body)
    sid = str(body.get("session_id") or "")
    meta = (body.get("list") or [{}])[0]
    total = int(meta.get("count") or 0)
    if not sid or total <= len(rows):
        return rows

    for group in meta.get("group_infos") or []:
        gid = str(group.get("group_id") or "")
        if not gid:
            continue
        page = 2  # 第 1 页就是上面那次
        while len(rows) < total:
            try:
                page_body = call_tool("search_by_mql", {
                    "project_key": PROJECT_KEY,
                    "session_id": sid,
                    "group_pagination_list": [
                        {"group_id": gid, "page_size": _PAGE_SIZE, "page_num": page},
                    ],
                })
            except McpError:
                # 翻页失败就带着已拿到的行返回：少几页比整条查询失败对用户更有用，
                # 但调用方必须知道数据不全 —— 用 warning 留痕，不静默。
                _log_partial(len(rows), total, statement)
                return rows
            got = _rows_of(page_body)
            if not got:
                break
            rows.extend(got)
            page += 1
    return rows


_PAGE_SIZE = 50


def _rows_of(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = body.get("data") or {}
    out: List[Dict[str, Any]] = []
    for group in data.values():
        if isinstance(group, list):
            out.extend(group)
    return out


def _log_partial(got: int, total: int, statement: str) -> None:
    import logging

    logging.getLogger("pm_hub.mcp").warning(
        "MQL 翻页中断，只拿到 %d/%d 行：%s", got, total, statement[:80]
    )
