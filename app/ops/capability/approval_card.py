"""审批交互卡片 — 构建发送 + card_action 回调处理（L2 校验 + 幂等）。

卡片流程（设计稿 4.1 时序）：
ingest 扫描完发 pending 卡片（摘要/风险/按钮）→ 管理员点「接入/拒绝」
→ on_card_action 校验操作者 L2 + manifest 状态幂等 → 部署 → update_card 回执。

按钮 value 透传 {cap: capability_id, act: approve|reject}，
事件侧从 CardActionEvent.operator.open_id + action.value 取。
"""

from __future__ import annotations

import logging
from typing import Any

from app.ops.capability.scanner import RISK_HIGH, RISK_LABEL_FALLBACK
from app.ops.capability.store import (
    STATUS_PENDING,
    CapabilityRecord,
    CapabilityStore,
)
from app.orchestrator import get_user_permission
from app.orchestrator.router import tool_name_for

logger = logging.getLogger("capability.approval")

_APPROVE = "approve"
_REJECT = "reject"


def build_pending_card(rec: CapabilityRecord, summary_lines: list[str], description: str = "") -> dict[str, Any]:
    """构建 pending 审批卡片（CardBuilder 产出的 dict）。

    description：包内 description 原文 — 以 code_block 展示给审批人，
    因为它是唯一能被包内容直接影响的 LLM 决策面（RouterLLM 工具表）。
    """
    from lark_channel import CardBuilder

    risk_label = {0: "low", 1: "medium", 2: "high"}.get(rec.risk_level, RISK_LABEL_FALLBACK)
    header_template = "green" if rec.risk_level == 0 else ("orange" if rec.risk_level == 1 else "red")
    header_title = f"能力接入审批 · {risk_label} 风险"

    b = CardBuilder()
    b.header(header_title, template=header_template)
    b.markdown(
        f"**{rec.name}**（{rec.type}）\n"
        f"提交人：{rec.source_sender_name or rec.source_sender_id}\n"
        + "\n".join(f"- {line}" for line in summary_lines)
    )
    if rec.risk_level < RISK_HIGH and rec.parse_ok:
        # 路由注入面强制可见：工具名 + description 原文将进 RouterLLM 工具表，
        # 审批人审的就是这段将被注入的文本本身
        b.markdown(f"**路由注入面**（接入后注册路由工具 `{tool_name_for(rec.name)}`，描述原文如下）")
        b.code_block(description or "（未提取到描述）", language="text")
    if rec.risk_level >= RISK_HIGH or not rec.parse_ok:
        # D3：high 自动拒绝，无审批按钮
        b.markdown("**🚨 高风险包，已按安全策略自动拒绝，无审批入口。**")
    else:
        # CardBuilder.button 的 action 即按钮 value（回调透传的 KV），
        # 不要再包一层 {"tag": "button", "value": ...} —— 包了回调里取不到 cap/act
        b.button("✅ 接入", action={"cap": rec.capability_id, "act": _APPROVE}, style="primary")
        b.button("❌ 拒绝", action={"cap": rec.capability_id, "act": _REJECT}, style="danger")
    b.note("仅 L2 管理员可审批 · 全程留痕（manifest + trace）")
    payload = b.build()
    return dict(payload.data)


def build_result_card(rec: CapabilityRecord, extra_lines: list[str]) -> dict[str, Any]:
    """构建处理结果卡片（用于 update_card 回执）。"""
    from lark_channel import CardBuilder

    ok = rec.status == "active"
    b = CardBuilder()
    b.header(
        f"能力接入{'成功' if ok else '未通过'} · {rec.name}",
        template="green" if ok else "grey",
    )
    approver = rec.decided_by_name or rec.decided_by or "-"
    lines = [f"状态：{rec.status}", f"审批人：{approver}"]
    if rec.deploy_result and rec.deploy_result != "ok":
        lines.append(f"部署结果：{rec.deploy_result}")
    lines.extend(extra_lines)
    b.markdown("\n".join(f"- {line}" for line in lines))
    payload = b.build()
    return dict(payload.data)


def _extract_action_value(event: Any, key: str) -> dict | None:
    """从卡片回调事件提取指定 key 的按钮透传 KV（如 cap / task）。

    飞书 2.0 卡片的自定义数据在 behaviors[].value，回调包装形态有两种：
    action.value 直取（SDK 已解包）或整体嵌套一层；两者都取不到时回退
    读原始 payload 里的 behaviors。没有目标 key（非本模块的卡片）返回 None，
    调用方静默忽略——多 handler 聚合分发时互不误认领。
    """
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict):
        value = {}
    for candidate in (value, value.get("value") if isinstance(value.get("value"), dict) else None):
        if candidate and key in candidate:
            return dict(candidate)
    raw = getattr(event, "raw", None) or {}
    try:
        behaviors = (
            raw.get("event", {})
            .get("action", {})
            .get("behaviors", [])
        )
        for b in behaviors:
            if isinstance(b, dict) and isinstance(b.get("value"), dict) and key in b["value"]:
                return dict(b["value"])
    except AttributeError:
        pass
    return None


class ApprovalCardHandler:
    """card_action 回调处理：身份校验 → 幂等 → 分派部署/拒绝。"""

    def __init__(
        self,
        store: CapabilityStore,
        deploy_callback,
        update_card_callback,
        min_level: int = 2,
        resolve_name_callback=None,
    ):
        """deploy_callback(rec) -> DeployResult（同步，跑在线程池）。
        update_card_callback(message_id, card_dict) -> None（异步）。
        resolve_name_callback(open_id, chat_id) -> str（异步，审批人显示名）。"""
        self._store = store
        self._deploy = deploy_callback
        self._update_card = update_card_callback
        self._min_level = min_level
        self._resolve_name = resolve_name_callback

    async def handle(self, event: Any, reply_callback=None) -> None:
        """处理卡片按钮点击。reply_callback(chat_id, text) 用于错误回执。"""
        value = _extract_action_value(event, key="cap")
        if value is None:
            return
        cap_id = str(value.get("cap"))
        act = str(value.get("act", ""))
        operator = getattr(event, "operator", None)
        operator_id = getattr(operator, "open_id", "") or ""

        rec = self._store.get(cap_id)
        if rec is None:
            await self._note(event, reply_callback, f"未找到能力档案 {cap_id}")
            return

        # ── 身份校验（D1：审批人须 L2；与提交人可同一人）──
        perm = get_user_permission(operator_id)
        if perm.level < self._min_level:
            logger.warning(
                "[capability] approval denied: operator=%s level=%s cap=%s",
                operator_id, perm.level, cap_id,
            )
            await self._note(event, reply_callback,
                             f"⛔ 仅 L2 管理员可审批（当前 L{perm.level}）")
            return

        # ── 幂等：只有 pending 可处理 ──
        if rec.status != STATUS_PENDING:
            await self._note(event, reply_callback,
                             f"该能力已处理过（{rec.status}），本次点击忽略")
            return

        rec.decided_by = operator_id
        rec.decided_by_name = await self._operator_name(operator_id, event)
        from datetime import datetime

        rec.decided_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if act == _REJECT:
            self._store.update(rec, "rejected")
            await self._update_card(event.message_id,
                                    build_result_card(rec, ["已拒绝接入，原始包留档 rejected/"]))
            return

        if act != _APPROVE:
            return

        # ── 部署（同步操作放线程池，避免阻塞事件循环）──
        import asyncio

        pkg_dir = self._store.record_dir(rec) / "pkg"
        loop = asyncio.get_running_loop()
        deploy_result = await loop.run_in_executor(
            None, lambda: self._deploy(rec, pkg_dir)
        )

        if deploy_result.ok:
            rec.deploy_result = "ok"
            extra = [deploy_result.effective_scope]
            extra.extend(f"⚠️ {d}" for d in deploy_result.degraded)
            self._store.update(rec)
            self._store.archive(rec)
        else:
            rec.deploy_result = f"error:{deploy_result.error[:200]}"
            extra = [f"❌ {deploy_result.error}", "旧图继续服务，原始包留档 failed/"]
            self._store.update(rec, "failed")
        await self._update_card(event.message_id, build_result_card(rec, extra))

    async def _note(self, event: Any, reply_callback, text: str) -> None:
        if reply_callback is not None:
            try:
                await reply_callback(event.chat_id, text)
            except Exception:
                logger.debug("capability note reply failed", exc_info=True)

    async def _operator_name(self, operator_id: str, event: Any) -> str:
        """审批人显示名：卡片回调自带 operator.name（SDK 已解析）优先，
        免一次成员名单 API；为空再走注入的解析回调（飞书成员名单），
        仍失败留空（卡片退回 open_id）——p2p 会话拉群名单会失败，
        此前只走该路径导致审批卡片长期显示 open_id。"""
        operator = getattr(event, "operator", None)
        inline = str(getattr(operator, "name", "") or "").strip()
        if inline:
            return inline
        fn = self._resolve_name
        if fn is None or not operator_id:
            return ""
        try:
            return (await fn(operator_id, getattr(event, "chat_id", "") or "")) or ""
        except Exception:
            logger.debug("capability approver name resolve failed", exc_info=True)
            return ""
