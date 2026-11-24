"""webportal-agent handler — 工单 → SQL（A2A 客户端，对接 webportal-order-agent）。

能力承载在独立仓库 webportal-order-agent（A2A Server，127.0.0.1:8080，
独立迭代独立发布）；本 Handler 只是编排壳，不再是引擎宿主：
1. Function Calling 选工具：process_order（工单→SQL）/ ingest_order（案例沉淀）/
   list_cases（案例库概览）
2. 工具执行 = A2A message:send（HTTP+JSON 绑定，非流式阻塞至任务终态）：
   - process_order：聚合文本 + 附件文件 Part（raw base64，pdf/图片/.msg
     解析全部在服务端 read_ticket_text 完成）
   - ingest_order / list_cases：文本前缀消息（沉淀案例 <id> / 案例列表）
3. A2A 产物 JSON → 用户可读回复（SQL 卡 + 审查表 + advisory 显式降级说明）

多轮聚合保留（10 分钟 TTL）：首条工单描述 + 补充合并后再触发引擎；附件
原样入缓冲（本地只读 bytes 不解析——read_ticket_text 已随引擎迁至上游）。
纯文件消息只回执不触发引擎（事故形态：描述随后而到，TTL 内
合并出单）。缓冲仅在成功出单后清空，失败/阻断保留可续跑。

安全红线继承引擎：SQL 只产出不执行；CMDB 仅只读 GET；draft 案例不参与检索。
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from app.agents.handlers import AgentHandler, ToolResult
from app.domains.webportal import user_auth
from app.domains.webportal.approval_link import (
    build_guidance,
    download_form_attachments,
    extract_applink_instance_ids,
    extract_approval_codes,
    fetch_instance_form,
    format_approval_content,
    list_my_approvals,
    locate_instance,
    probe_short_link,
    resolve_by_instance_ids,
)

logger = logging.getLogger("agents.handlers.webportal")

# ── A2A 服务定位 ─────────────────────────────────────────────────

# webportal-order-agent A2A Server（独立进程，python -m order_agent.a2a_server）
_A2A_BASE = os.environ.get("WEBPORTAL_A2A_URL", "http://127.0.0.1:8080").rstrip("/")

# 引擎单次调用上限（服务端含多次 LLM + dry-run；网关慢时可跑到 170s+，
# 卡在这个量级的超时会被误报成「服务不可达」）
_A2A_TIMEOUT_SEC = 300

# 多轮聚合 TTL（秒）：首条工单描述 + 后续补充合并的窗口
_AGGREGATE_TTL = 600

# 单附件大小上限（超限跳过并回文本说明；channel 下载层另有更小的上限）
_MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024

# 本轮聚合附件 → process_order 工具执行体（contextvar 隔离并发会话，
# run_with_message 进入 self.run 前 set、结束后 reset；默认 None 非可变）
_pending_attachments: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "webportal_pending_attachments", default=None)

# 当前会话 key → fetch_approval 的 pending 选单落到对应聚合缓冲
# （execute_tool 无 session 入参，contextvar 隔离并发会话）
_current_session_key: ContextVar[str] = ContextVar(
    "webportal_current_session", default="")

# 审批候选选单 pending 有效期（秒）：歧义列选后用户回序号的窗口
_APPROVAL_PICK_TTL = 300

# ── per-sender 授权提示（直返文案：URL 须逐字触达，不经 LLM 改写）──
_AUTH_REQUEST_TMPL = (
    "🔐 这条审批不在我的可见范围，需要你授权我以你的身份读取"
    "（仅申请审批只读权限）：\n"
    "{url}\n"
    "10 分钟内有效。用你的飞书账号完成授权后，重发刚才的消息，我立即继续处理。"
)
_AUTH_PENDING_TMPL = (
    "⏳ 授权还没完成——请先点击授权链接完成确认，再重发消息：\n{url}"
)
_AUTH_WRONG_TMPL = (
    "⚠️ 刚才的授权用的不是发消息的这个账号。请用你自己的飞书账号重新授权：\n"
    "{url}\n10 分钟内有效，完成后重发消息。"
)


# ── 工具定义 ─────────────────────────────────────────────────────

ORDER_AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "process_order",
            "description": "处理工单：将工单/审批描述匹配 verified 案例库生成 SQL 供人工审核。"
                "命中案例 → 高可信 SQL；未命中 → advisory 参考建议（低可信）",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_text": {
                        "type": "string",
                        "description": "聚合后的完整工单描述文本（含多轮补充，"
                            "附件由系统自动携带，无需也不应写进本参数）",
                    },
                },
                "required": ["ticket_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_order",
            "description": "案例沉淀：把已验证的工单处理过程收进知识库（本地案例库 + 飞书案例库镜像）。"
                "素材二选一：CMDB 工单号（系统只读采集），或飞书文档链接（操作手册/工单记录，"
                "系统自动取正文）。落 draft 草稿，人工确认后用 confirm_case 定稿才参与检索",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "CMDB 工单号（纯数字），如 5107。与 feishu_url 二选一",
                    },
                    "feishu_url": {
                        "type": "string",
                        "description": "飞书文档链接（wiki/docx，操作手册或工单记录）。与 order_id 二选一",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_case",
            "description": "同步案例到飞书案例库镜像：案例定稿（status 改 verified）后重同步，"
                "或镜像缺失/过期时补同步。不改本地案例内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "案例 id（本地案例文件名去掉 .md）",
                    },
                },
                "required": ["case_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_case",
            "description": "案例定稿：人工核对无误后把 draft 改成 verified（此后才参与检索），"
                "并重推飞书案例库镜像。**仅在用户明确说确认/没问题时调用**——"
                "采集物不得直接生效，人工确认是定稿的唯一入口",
            "parameters": {
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "案例 id（本地案例文件名去掉 .md）",
                    },
                },
                "required": ["case_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_approval",
            "description": "读取飞书审批单（按短链）。系统已对本轮消息自动预读过链接；"
                "仅当需要换一个链接再读、或上文缺【审批单】块时调用。"
                "返回表单全文，附件自动随工单携带",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "go.feishu.cn/approval/s/ 审批短链（原样传）",
                    },
                    "context": {
                        "type": "string",
                        "description": "用户消息原文（含流程编号等线索，用于定位审批单）",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_cases",
            "description": "列出 verified 案例库概览（ID + 标题），供用户了解已覆盖场景",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class WebportalHandler(AgentHandler):
    """工单 → SQL 处理器（webportal-order-agent A2A 客户端壳）。"""

    agent_name = "webportal-agent"
    tools = ORDER_AGENT_TOOLS

    def __init__(self, config, case_mirror: Any = None):
        super().__init__(config)
        # 引擎本轮是否成功出单（run_with_message 据此决定清不清聚合缓冲）
        self._last_engine_ok = False
        # 多轮聚合缓冲：{session_key: {"combined": str, "attachments": [...], "ts": float}}
        self._buffer: dict[str, dict[str, Any]] = {}
        # 案例库飞书镜像写目标（config.toml [projects.case_mirror]，见 CaseMirrorConfig）
        self._case_mirror = case_mirror

    # ── 多轮聚合 ─────────────────────────────────────────────────

    def _aggregate(self, query: str, session_key: str, attachments: list[dict]) -> dict:
        """本轮消息并入会话缓冲（10 分钟 TTL），返回聚合上下文。

        纯文件消息的 query 是 lark_channel 生成的 `<file key=... name=.../>`
        占位符，对引擎是噪音——剥掉，附件以文件 Part 原样随 process_order 携带。
        """
        import re as _re
        query = _re.sub(r'<file key="[^"]*"(?:\s+name="[^"]*")?/>\s*', "", query).strip()
        now = time.time()
        entry = self._buffer.get(session_key)
        if entry and now - entry["ts"] <= _AGGREGATE_TTL:
            entry["combined"] += "\n" + query
            entry["ts"] = now
        else:
            entry = {"combined": query, "attachments": [], "ts": now}
        seen = {a.get("file_path") for a in entry["attachments"]}
        for a in attachments:
            if a.get("file_path") and a["file_path"] not in seen:
                entry["attachments"].append(a)
        self._buffer[session_key] = entry
        return entry

    def _clear_buffer(self, session_key: str) -> None:
        self._buffer.pop(session_key, None)

    # ── A2A 调用 ────────────────────────────────────────────────

    @staticmethod
    def _attachment_parts(attachments: list[dict]) -> list[dict]:
        """附件 → A2A 文件 Part（raw base64 + filename + mediaType）。

        读取失败/超限降级为文本块（服务端/用户都能看到发生了什么），
        不让单个坏附件拖垮整单——与旧版附件转文本的降级语义一致。
        """
        parts: list[dict] = []
        for a in attachments:
            fp = str(a.get("file_path") or "")
            name = a.get("filename") or (Path(fp).name if fp else "attachment")
            try:
                path = Path(fp)
                size = path.stat().st_size
                if size > _MAX_ATTACHMENT_BYTES:
                    parts.append({"text": f"【附件 {name} 跳过：超过 "
                                          f"{_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB】"})
                    continue
                data = path.read_bytes()
            except OSError as e:
                parts.append({"text": f"【附件 {name} 读取失败: {e}】"})
                continue
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            parts.append({
                "raw": base64.b64encode(data).decode(),
                "filename": name,
                "mediaType": mime,
            })
        return parts

    async def _a2a_send(self, parts: list[dict]) -> dict:
        """A2A message:send（阻塞至任务终态）→ 产物 JSON。

        抛 RuntimeError 表达协议/服务层失败（连接拒绝、任务 FAILED、
        产物缺失/非法 JSON），由 execute_tool 统一转 ToolResult error。
        """
        body = {"message": {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": parts,
        }}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_A2A_TIMEOUT_SEC, connect=5.0),
            ) as client:
                resp = await client.post(
                    f"{_A2A_BASE}/message:send", json=body,
                    headers={"A2A-Version": "1.0"},
                )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"工单服务不可达（{_A2A_BASE}，需运行 python -m order_agent.a2a_server）：{e}"
            ) from e
        if resp.status_code != 200:
            raise RuntimeError(f"A2A HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

        task = data.get("task")
        if task is None:
            message = data.get("message")
            if message is None:
                raise RuntimeError(f"A2A 响应缺少 task/message: {str(data)[:200]}")
            text = next(
                (p.get("text", "") for p in (message.get("parts") or [])
                 if p.get("text")), "")
            return self._parse_payload(text)
        state = str((task.get("status") or {}).get("state", ""))
        if state == "TASK_STATE_COMPLETED":
            for artifact in task.get("artifacts") or []:
                for part in artifact.get("parts") or []:
                    if part.get("text"):
                        return self._parse_payload(part["text"])
            raise RuntimeError("A2A 任务完成但无文本产物")
        status_msg = (task.get("status") or {}).get("message") or {}
        text = next(
            (p.get("text", "") for p in (status_msg.get("parts") or [])
             if p.get("text")), "")
        raise RuntimeError(f"A2A 任务 {state or '未知状态'}: {text or '无错误详情'}"[:300])

    @staticmethod
    def _parse_payload(text: str) -> dict:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"A2A 产物不是 JSON（{e}）: {text[:120]}") from e
        if not isinstance(parsed, dict):
            raise RuntimeError(f"A2A 产物类型异常: {text[:120]}")
        return parsed

    # ── 工具执行（全部走 A2A，无进程内引擎）────────────────────

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        try:
            if tool_name == "process_order":
                text = str(args.get("ticket_text", ""))
                attachments = list(_pending_attachments.get() or [])
                if not text.strip() and not attachments:
                    return ToolResult(error="处理被阻断：工单内容为空（文本与附件均为空）")
                parts: list[dict] = []
                if text.strip():
                    parts.append({"text": text})
                parts.extend(self._attachment_parts(attachments))
                payload = await self._a2a_send(parts)
                if payload.get("blocked"):
                    return ToolResult(error=(
                        f"处理被阻断：{payload.get('message') or '引擎阻断'}。"
                        "请补全工单信息后重试，或联系管理员检查工单服务。"))
                # 引擎成功出单 → run_with_message 据此清聚合缓冲
                self._last_engine_ok = True
                reply = self._format_sql_payload(payload)
                return ToolResult(data={"reply": reply, "done": True})

            if tool_name == "ingest_order":
                return await self._run_ingest(args)

            if tool_name == "sync_case":
                return await self._run_sync_case(str(args.get("case_id", "") or "").strip())

            if tool_name == "confirm_case":
                return await self._run_confirm_case(str(args.get("case_id", "") or "").strip())

            if tool_name == "fetch_approval":
                return await self._run_fetch_approval(
                    str(args.get("url", "")), str(args.get("context", "")))

            if tool_name == "list_cases":
                payload = await self._a2a_send([{"text": "案例列表"}])
                cases = payload.get("cases") or []
                if not cases:
                    return ToolResult(data={"reply":
                        "⚠️ 案例库为空（无 verified 案例），当前所有工单只能产出 advisory 参考建议",
                        "done": True})
                total = int(payload.get("total") or len(cases))
                lines = [f"📚 **已验证案例（{total} 条）**", ""]
                for c in cases[:50]:
                    lines.append(f"  • `{c.get('id', '?')}` {c.get('title', '')}")
                if total > 50:
                    lines.append(f"  … 共 {total} 条")
                return ToolResult(data={"reply": "\n".join(lines), "done": True})

            return ToolResult(error=f"未知工具: {tool_name}")
        except Exception as e:
            # 引擎 BlockedError / A2A 协议失败语义 = 硬失败如实告知，不降级产出
            msg = str(e)[:300]
            logger.warning("webportal tool '%s' failed: %s", tool_name, msg)
            return ToolResult(
                error=f"处理被阻断：{msg}。请补全工单信息后重试，或联系管理员检查工单服务。"
            )

    # ── 案例沉淀 / 飞书案例库镜像同步 ─────────────────────────────

    async def _run_ingest(self, args: dict) -> ToolResult:
        """案例沉淀：CMDB 工单号 或 飞书文档链接（二选一）→ 本地 draft + 飞书镜像。"""
        from app.protocols.lark_cli import LarkCli

        order_id = str(args.get("order_id", "") or "").strip()
        feishu_url = str(args.get("feishu_url", "") or "").strip()
        if bool(order_id) == bool(feishu_url):
            return ToolResult(error="order_id 与 feishu_url 必须二选一："
                                    "给 CMDB 工单号（纯数字）或飞书文档链接")
        if order_id:
            if not order_id.isdigit():
                return ToolResult(error="order_id 必须是纯数字的 CMDB 工单号")
            source_desc = f"CMDB 工单 {order_id}"
            parts = [{"text": f"沉淀案例 {order_id}"}]
        else:
            # 飞书正文在本侧取（引擎不访问飞书），随「沉淀案例 飞书 <链接>」带过去
            doc = await LarkCli().get_doc_content(feishu_url, max_chars=20000)
            body = str(doc.get("content") or "").strip()
            if not body:
                return ToolResult(error=f"飞书文档取不到正文：{feishu_url}")
            source_desc = f"飞书文档 {feishu_url}"
            parts = [{"text": f"沉淀案例 飞书 {feishu_url}\n{body}"}]

        payload = await self._a2a_send(parts)
        if not payload.get("ingested"):
            return ToolResult(error=(
                f"处理被阻断：案例采集失败: "
                f"{payload.get('error') or '未知错误'}"))

        mirror_line = await self._sync_case_mirror(payload)
        reply = (
            f"📥 **案例草稿已沉淀**（draft，待人工确认后生效）\n"
            f"来源: {source_desc}\n"
            f"文件: `{payload.get('draft_path', '')}`\n"
            f"{mirror_line}\n\n"
            f"请人工核对场景与 SQL 模板；确认无误后说「确认案例 {payload.get('case_id', '')}」"
            f"定稿（改 verified 并重推飞书镜像），之后才会参与检索。"
        )
        return ToolResult(data={"reply": reply, "done": True})

    async def _run_sync_case(self, case_id: str) -> ToolResult:
        """案例镜像重同步（定稿改 verified 后 / 镜像缺失补同步），不改本地内容。"""
        if not case_id:
            return ToolResult(error="case_id 为空：请给案例 id")
        payload = await self._a2a_send([{"text": f"案例同步 {case_id}"}])
        if not payload.get("found"):
            return ToolResult(error=(
                f"处理被阻断：{payload.get('error') or f'案例不存在：{case_id}'}"))
        mirror_line = await self._sync_case_mirror(payload)
        return ToolResult(data={"reply": (
            f"🔄 **案例镜像同步** `{payload.get('case_id', case_id)}`"
            f"（status: {payload.get('status', '?')}）\n{mirror_line}"
        ), "done": True})

    async def _run_confirm_case(self, case_id: str) -> ToolResult:
        """人工确认定稿：改 verified + 重推飞书镜像（内容里的「状态：」随之更新）。"""
        if not case_id:
            return ToolResult(error="case_id 为空：请给案例 id")
        payload = await self._a2a_send([{"text": f"案例状态 {case_id} verified"}])
        if not payload.get("updated"):
            return ToolResult(error=f"定稿失败：{payload.get('error') or '未知错误'}")
        mirror_line = await self._sync_case_mirror(payload)
        ref = str((payload.get("case_ref") or {}).get("url") or "")
        return ToolResult(data={"reply": (
            f"✅ **案例已定稿** `{payload.get('case_id', case_id)}`（status: verified，"
            f"已参与检索）\n{mirror_line}\n\n"
            f"案例文档：{ref or '（缺 mirror_ref，请补同步）'}"
        ), "done": True})

    async def _sync_case_mirror(self, payload: dict) -> str:
        """把产物里的案例镜像推到飞书案例库；失败只如实报告，不影响本地落盘。

        成功后必须把 node_token 写回本地案例的 mirror_ref——那是本地知识库 ↔
        飞书镜像的唯一映射（产物「案例文档」链接取它）。不写回两边就漂移，
        且案例改 verified 会被引擎拒（缺 mirror_ref 不许定稿）。
        """
        from app.domains.webportal.feishu_case_mirror import CaseMirrorSync
        from app.protocols.lark_cli import LarkCliError

        sync = CaseMirrorSync(cfg=self._case_mirror)
        error = sync.config_error()
        if error:
            return f"⚠️ 飞书镜像未同步：{error}"
        markdown = str(payload.get("mirror_markdown") or "")
        if not markdown:
            return "⚠️ 飞书镜像未同步：产物缺镜像内容"
        try:
            result = await sync.sync(
                title=str(payload.get("title") or payload.get("case_id") or ""),
                status=str(payload.get("status") or "draft"),
                source_type=str(payload.get("source_type") or "cmdb"),
                mirror_markdown=markdown)
        except LarkCliError as exc:
            logger.warning("case mirror sync failed: %s", exc)
            return f"⚠️ 飞书镜像同步失败：{exc}（本地草稿不受影响）"
        if not result.ok:
            return f"⚠️ 飞书镜像同步失败：{result.error}（本地草稿不受影响）"
        verb = "已新建子文档" if result.created else "已更新子文档"
        check = "回读校验通过" if result.verified else "回读校验未确认，请人工核对目录索引"
        head = f"🪞 飞书案例库镜像已同步：{verb} {result.node_url}（{check}）"
        return f"{head}\n{await self._write_mirror_ref(str(payload.get('case_id') or ''), result.node_url)}"

    async def _write_mirror_ref(self, case_id: str, node_url: str) -> str:
        """把飞书子文档链接写回本地案例 front-matter（映射维护，引擎侧落盘）。

        引擎零飞书写依赖：建/更子文档归本侧，写映射回归引擎，各管一段。
        """
        if not case_id or not node_url:
            return "⚠️ 映射未写回：缺案例 id 或子文档链接"
        try:
            res = await self._a2a_send([{"text": f"案例映射 {case_id} {node_url}"}])
        except Exception as exc:
            logger.warning("mirror_ref writeback failed: %s", exc)
            return f"⚠️ 映射未写回：{exc}（可稍后说「同步案例 {case_id}」补同步）"
        if not res.get("updated"):
            return f"⚠️ 映射未写回：{res.get('error') or '未知错误'}"
        return "🔗 本地↔飞书映射已写回（mirror_ref）"

    # ── 审批单读取（fetch_approval）────────────────────────────

    @staticmethod
    def _format_picker(cands: list[dict[str, Any]]) -> str:
        """歧义候选 → 编号列表（用户回序号续接）。"""
        lines = ["【请原样转达给用户，不要改写】", "找到几条可能的审批单，回复序号我继续："]
        topic_zh = {"1": "待办", "17": "未读知会", "18": "已读知会",
                    "2": "已办", "3": "已发起"}
        for i, c in enumerate(cands, 1):
            sums = " / ".join(
                f"{s.get('key', '')}={str(s.get('value', ''))[:24]}"
                for s in (c.get("summaries") or [])[:3])
            lines.append(
                f"{i}. **{c.get('definition_name', '审批单')}**"
                + (f"（{c.get('initiator_name', '')} · {topic_zh.get(c.get('topic', ''), '')}）" if c.get("initiator_name") else "")
                + (f"\n   {sums}" if sums else ""))
        return "\n".join(lines)

    async def _run_fetch_approval(self, url: str, context: str) -> ToolResult:
        """短链/applink → 探测 → 关联（含 instanceId 兜底）→ 表单 + 附件。"""
        query_text = " ".join(x for x in (url, context) if x)
        codes = extract_approval_codes(query_text)
        applink_ids = extract_applink_instance_ids(query_text)
        if not codes and not applink_ids:
            return ToolResult(data={"reply":
                "【请原样转达给用户】" + build_guidance(
                    link_kind="未识别到 go.feishu.cn 审批短链或 applink 深链",
                ), "done": True})
        sender, _ = user_auth.session_ids(_current_session_key.get())
        env = user_auth.token_env_sync(sender) if sender else None
        probe = await probe_short_link(codes[0]) if codes else {}
        instance_ids = list(dict.fromkeys(
            x for x in (applink_ids + [probe.get("instance_id", "")]) if x))
        cands = await list_my_approvals(env_extra=env)
        loc = locate_instance(probe, query_text, cands)
        if loc.status != "matched" and instance_ids:
            # 兜底：applink/短链里的数字 instanceId ↔ 候选任务创建毫秒对齐
            fb = await resolve_by_instance_ids(instance_ids, cands, env_extra=env)
            if fb is not None:
                loc = fb
        fixed = await user_auth.fixed_open_id()
        trusted = (env is not None or loc.level in ("L0", "L1")
                   or not sender or sender == fixed)

        # 非可信命中 + 链接真实存在 → 走 per-sender 授权（与 prefetch 同规则）
        if not trusted and instance_ids and sender:
            block = await self._auth_block(sender)
            if block:
                return ToolResult(data={
                    "reply": "【请原样转达给用户，不要改写】\n" + block,
                    "done": True})

        if loc.status == "ambiguous":
            # 落选单 pending：用户回序号/「候选」由 run_with_message 续接
            session = _current_session_key.get()
            entry = self._buffer.get(session)
            if entry is not None:
                entry["pending_approval"] = {
                    "candidates": loc.candidates, "ts": time.time(),
                    "original_query": query_text,
                }
            return ToolResult(data={"reply": self._format_picker(loc.candidates),
                                    "done": True})

        if loc.status != "matched" or not loc.candidate:
            return ToolResult(data={"reply":
                "【请原样转达给用户】" + build_guidance(
                    link_kind="审批短链" if codes else "applink 深链",
                    instance_id=instance_ids[0] if instance_ids else "",
                    candidates=len(cands),
                ), "done": True})

        cand = loc.candidate
        detail = await fetch_instance_form(cand["instance_code"], env_extra=env)
        if detail is None:
            if (sender and env is None
                    and (fixed is None or sender != fixed)):
                block = await self._auth_block(sender)
                if block:
                    return ToolResult(data={
                        "reply": "【请原样转达给用户，不要改写】\n" + block,
                        "done": True})
            return ToolResult(data={"reply":
                "【请原样转达给用户】⚠️ 审批单定位到了，但详情读取失败（权限或已删除）。\n"
                f"定位结果：{cand.get('definition_name')}（{loc.reason}），请稍后重试或补充流程编号。",
                "done": True})

        # 附件下载并入聚合缓冲（与消息附件同形状 → process_order 自动携带）
        files = await download_form_attachments(detail["attachments"])
        atts = _pending_attachments.get()
        if files and atts is not None:
            seen = {a.get("file_path") for a in atts}
            atts.extend(f for f in files if f["file_path"] not in seen)

        meta = detail["meta"]
        content = format_approval_content(
            detail["text"], meta, files,
            located_by=f"{loc.reason}：{meta.get('definition_name')}")
        # data 里同时给 LLM：定位回执（须转达）+ 表单全文（供继续处理/问答）
        return ToolResult(data={
            "reply": content,
            "approval_form": detail["text"],
            "located": loc.reason,
            "done": True,
        })

    # ── 结果格式化 ───────────────────────────────────────────────

    @staticmethod
    def _format_sql_payload(r: dict) -> str:
        """A2A 产物 JSON → 用户可读回复（SQL 卡 + 审查表 + advisory 降级说明）。"""
        parts: list[str] = []
        if r.get("advisory"):
            parts.append(
                "⚠️ **无案例背书 — 以下为参考建议（低可信、仅供参考）**\n"
                "知识库中无匹配的 verified 案例，建议沉淀案例后再出正式 SQL。\n"
                + (f"分析思路: {str(r.get('thinking') or '')[:300]}\n"
                   if r.get("thinking") else "")
            )
        else:
            emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                str(r.get("risk", "low")), "⚪")
            parts.append(
                f"✅ **命中案例** `{r.get('case_id', '')}`「{r.get('case_title', '')}」"
                f"match={r.get('match', '')} {emoji} 风险: {r.get('risk', '')}"
            )
        params_missing = r.get("params_missing") or []
        if params_missing:
            parts.append("❓ **缺参数**: " + "、".join(f"`{x}`" for x in params_missing)
                         + "（SQL 里已用 :占位符，回填即可执行）")
        sql = str(r.get("sql") or "")
        # SQL 完整交付不截断：一次给全，缺参处用占位符
        parts.append("```sql\n" + sql + "\n```")
        # 命中案例 → 案例知识库**条目**链接（case_ref.url = 飞书子文档，即 mirror_ref）。
        # 只给条目、不给源头依据：依据已渲染在条目「## 来源」段里，两份材料会让审核侧
        # 分不清该看哪个（此前误把溯源 sources 的飞书手册当「案例文档」渲染过）。
        ref = r.get("case_ref") or {}
        url = str(ref.get("url") or "")
        if url:
            parts.append(f"📄 **案例文档**：{ref.get('title') or r.get('case_id', '')} {url}")
        elif r.get("case_id"):
            parts.append(f"📄 **案例文档**：（缺 mirror_ref，请说「同步案例 {r.get('case_id')}」补同步）")
        review = r.get("review") or []
        if review:
            review_lines = [
                f"  • {item.get('item', '?')}: {item.get('result', '?')}"
                + (f"（{str(item.get('note', ''))[:50]}）" if item.get("note") else "")
                for item in review[:6]
            ]
            parts.append("**审查要点**\n" + "\n".join(review_lines))
        parts.append("💡 SQL 仅供人工审核，请勿直接执行。")
        return "\n\n".join(parts)

    # ── AgentHandler 接口 ────────────────────────────────────────

    def _tool_narrative(self, tool_name: str, args: dict, done: bool) -> str:
        """工单域工具事件 → 叙事化文案（不暴露内部工具名，用户读得懂）。"""
        if tool_name == "process_order":
            return ("正在比对案例库、生成 SQL 方案" if not done
                    else "SQL 方案已生成")
        if tool_name == "ingest_order":
            return ("正在从 CMDB 采集工单案例" if not done
                    else "案例草稿已沉淀")
        if tool_name == "fetch_approval":
            return ("正在读取审批单" if not done else "审批单已读取")
        if tool_name == "list_cases":
            return "正在整理已覆盖的场景" if not done else "案例库概览已就绪"
        return super()._tool_narrative(tool_name, args, done)

    async def _auth_block(self, sender: str) -> str:
        """发起 per-sender 设备授权 → 用户可见提示块；发起失败返回空串。"""
        url, _ = await user_auth.start_auth(sender)
        if not url:
            return ""
        logger.info("approval auth requested sender=%s", sender[:12])
        return _AUTH_REQUEST_TMPL.format(url=url)

    async def _prefetch_approval(
            self, entry: dict, session_key: str) -> tuple[str, bool]:
        """审批短链确定性预读：命中链接就先拿表单。

        返回 (文本, direct)：direct=True 时文本**原样**作为本轮回复直返
        （授权提示含 URL，须逐字触达，不进 LLM 改写）；direct=False 时文本
        并入上下文走 LLM。空串 = 无审批短链（常见情形，零开销直通）。
        """
        combined = entry.get("combined", "")
        codes = extract_approval_codes(combined)
        applink_ids = extract_applink_instance_ids(combined)
        if not codes and not applink_ids:
            return "", False
        sender, _ = user_auth.session_ids(session_key)

        # 发送者身份解析：有 token → 以发送者身份读；有进行中的设备授权 →
        # 按需轮询一次（点完授权重发消息即续上，无后台任务）。
        env: dict[str, str] | None = None
        state = "no_session"
        if sender:
            env, state = await user_auth.resolve(sender)
            if state == "pending":
                return _AUTH_PENDING_TMPL.format(
                    url=user_auth.session_url(sender)), True
            if state == "wrong_account":
                url, _ = await user_auth.start_auth(sender)
                if url:
                    return _AUTH_WRONG_TMPL.format(url=url), True
                state = "error"  # 发起失败 → 退回固定身份
            if state in ("expired", "error"):
                env = None

        probe = await probe_short_link(codes[0]) if codes else {}
        instance_ids = list(dict.fromkeys(
            x for x in (applink_ids + [probe.get("instance_id", "")]) if x))
        cands = await list_my_approvals(env_extra=env)
        loc = locate_instance(probe, combined, cands)
        if loc.status != "matched" and instance_ids:
            # 兜底：applink/短链里的数字 instanceId ↔ 候选任务创建毫秒对齐
            fb = await resolve_by_instance_ids(instance_ids, cands, env_extra=env)
            if fb is not None:
                loc = fb
        fixed = await user_auth.fixed_open_id()
        # 可信 = 发送者自己的池（env）/ 精确键 L0·L1 命中 / 发送者未知或
        # 就是固定登录身份（维持现状）。非可信的 L2·L3 命中不采用——多半
        # 是固定身份池里别人（或无关）的单，改走 per-sender 授权。
        trusted = (env is not None or loc.level in ("L0", "L1")
                   or not sender or sender == fixed)

        if loc.status == "matched" and loc.candidate and trusted:
            detail = await fetch_instance_form(
                loc.candidate["instance_code"], env_extra=env)
            if detail is None:
                # 定位到了却读不到详情：非固定身份且无 token → 走授权
                if (sender and env is None
                        and (fixed is None or sender != fixed)):
                    block = await self._auth_block(sender)
                    if block:
                        return block, True
                return ("【审批单定位失败】详情读取失败（权限或已删除），"
                        "请如实告知用户并请其补流程编号或发附件。"), False
            files = await download_form_attachments(detail["attachments"])
            if files:
                seen = {a.get("file_path") for a in entry["attachments"]}
                entry["attachments"].extend(
                    f for f in files if f["file_path"] not in seen)
            return format_approval_content(
                detail["text"], detail["meta"], files,
                located_by=loc.reason), False

        # 链接真实存在但没在可信池里精确定位：需要发送者自己的身份
        if not trusted and instance_ids and sender:
            block = await self._auth_block(sender)
            if block:
                return block, True

        if loc.status == "ambiguous":
            entry["pending_approval"] = {
                "candidates": loc.candidates, "ts": time.time(),
                "original_query": combined[:300],
            }
            return ("【审批单定位歧义，以下候选请转达用户回复序号】\n"
                    + self._format_picker(loc.candidates)), False
        return ("【审批单未定位到】" + build_guidance(
            link_kind="审批短链" if codes else "applink 深链",
            instance_id=instance_ids[0] if instance_ids else "",
            candidates=len(cands),
        ) + "\n（请如实转达用户，不要编造审批内容）"), False

    async def _try_pick_pending(self, entry: dict, query: str,
                                session_key: str) -> str | None:
        """审批候选选单续接：pending 期内「N」= 按序号拉单继续；「候选」= 重发列表。

        非这两类输入放行（pending 保留至 TTL，用户先说需求再回序号也通）。
        """
        pending = entry.get("pending_approval")
        if not pending or time.time() - pending.get("ts", 0) > _APPROVAL_PICK_TTL:
            entry.pop("pending_approval", None)
            return None
        text = query.strip()
        if text in ("候选", "候选列表"):
            return self._format_picker(pending.get("candidates") or [])
        if not re.fullmatch(r"\d{1,2}", text):
            return None
        n = int(text)
        cands = pending.get("candidates") or []
        if not 1 <= n <= len(cands):
            return f"序号超出范围（1-{len(cands)}），回复「候选」重新查看。"
        entry.pop("pending_approval", None)  # 取即删
        cand = cands[n - 1]
        sender, _ = user_auth.session_ids(session_key)
        env = user_auth.token_env_sync(sender) if sender else None
        detail = await fetch_instance_form(cand.get("instance_code", ""),
                                           env_extra=env)
        if detail is None:
            return "⚠️ 该候选详情读取失败（权限或已删除），回复「候选」重选。"
        files = await download_form_attachments(detail["attachments"])
        if files:
            entry["attachments"].extend(files)
        form_text = detail["text"]
        original = pending.get("original_query", "")
        synthesized = (
            f"（系统）用户已选定候选 {n}：{detail['meta'].get('definition_name')}。"
            f"审批单表单内容：\n{form_text}\n"
            f"用户原始诉求：{original[:300]}"
        )
        self._last_engine_ok = False
        token = _pending_attachments.set(entry["attachments"])
        session_token = _current_session_key.set(session_key)
        try:
            return await self.run(synthesized)
        finally:
            _pending_attachments.reset(token)
            _current_session_key.reset(session_token)

    async def run_with_message(self, query: str, session_key: str,
                               attachments: list[dict] | None = None,
                               history: list[dict] | None = None) -> str:
        """带聚合上下文的入口（crew 分派时优先调用）。

        将本轮文本+附件并入会话缓冲；附件**原样携带**到 process_order 的
        A2A 消息（文件 Part，服务端解析——本地不再转文本）。缓冲仅在引擎
        成功出单后清空——失败/阻断时保留（TTL 10 分钟），用户补充文本或
        重发消息可续跑（事故：文件消息与「处理这个工单」文字
        消息分两条到达，失败即清缓冲导致后一条丢了全部附件上下文）。
        """
        entry = self._aggregate(query, session_key, attachments or [])
        logger.info(
            "[webportal] session=%s aggregated %d chars, %d attachments",
            session_key[:20], len(entry["combined"]), len(entry["attachments"]),
        )

        # 审批短链确定性预读（不赌 LLM 会调工具）：链接 → 探测 → 关联 → 表单，
        # 【审批单】块直接并入上下文；附件并入聚合缓冲随工单携带。
        # direct=True（授权提示）→ 原样直返，URL 不经 LLM 改写。
        pre, direct = await self._prefetch_approval(entry, session_key)
        if direct and pre:
            logger.info("[webportal] approval auth direct-reply session=%s",
                        session_key[:20])
            return pre
        if pre:
            entry["combined"] = (entry["combined"] + "\n\n" + pre).strip()

        # 审批候选选单续接：pending 期内纯数字 → 按序号定位审批单继续；
        # 「候选」→ 重发候选列表。取即删（同澄清闭环语义）。
        picked = await self._try_pick_pending(entry, query, session_key)
        if picked is not None:
            return picked

        # 纯文件消息（占位符剥离后缓冲里无任何文字）→ 只登记附件回执，不触发引擎：
        # 工单描述通常随后一条文字消息到达，逐文件跑
        # 引擎既烧钱又丢聚合（成功即清缓冲）。TTL 10 分钟内补充文字即合并出单。
        if not entry["combined"].strip() and entry["attachments"]:
            names = "、".join(
                a.get("filename") or Path(str(a.get("file_path", ""))).name
                for a in entry["attachments"]
            )
            return (
                f"📎 已收到附件（{names}），暂存待合并。\n"
                "请补充一句工单说明（如「处理这个工单，流程编号 xxx」），"
                "我会合并全部附件与描述一次性生成 SQL。"
            )

        self._last_engine_ok = False
        token = _pending_attachments.set(entry["attachments"])
        session_token = _current_session_key.set(session_key)
        try:
            reply = await self.run(entry["combined"], history=history)
        except Exception:
            # 异常路径同样保留缓冲，让「稍后重试」有上下文可续
            raise
        finally:
            _pending_attachments.reset(token)
            _current_session_key.reset(session_token)
        if self._last_engine_ok:
            self._clear_buffer(session_key)
        return reply


def build(config, ctx: dict) -> WebportalHandler:
    """自动发现约定工厂。"""
    return WebportalHandler(config, case_mirror=ctx.get("case_mirror"))
