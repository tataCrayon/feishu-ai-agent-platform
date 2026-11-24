"""能力包接入入口 — 文件消息识别 / 下载 / 解包 / 扫描 / 建档 / 发卡片。

入口在 main.py 的消息分流：消息带文件附件且是能力包格式（zip/yaml/md）
时转交本服务；非管理员直接拒绝（不入 pending，防垃圾档案堆积）。

包格式约定（与注册发现对齐）：
- zip：内含 SKILL.md（Skill 包）或 agent.yaml（Agent/MCP 包）的目录包，≤2MB
- 单文件 agent.yaml / SKILL.md：直接作为单文件包
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from lark_channel import InboundMessage

from app.ops.capability.approval_card import build_pending_card
from app.ops.capability.scanner import CapabilityScanner, safe_extract_zip
from app.ops.capability.store import CapabilityRecord, CapabilityStore
from app.orchestrator import get_user_permission

logger = logging.getLogger("capability.ingest")

CAPABILITY_PKG_SUFFIXES = {".zip", ".yaml", ".yml", ".md"}
_MAX_PKG_BYTES = 2 * 1024 * 1024  # 设计稿 3.3：≤2MB


class CapabilityIngestService:
    """把管理员发来的能力包文件转成 pending 档案 + 审批卡片。"""

    def __init__(
        self,
        store: CapabilityStore,
        scanner: CapabilityScanner,
        send_card_callback,
        reply_callback,
        min_level: int = 2,
        max_pkg_mb: int = 2,
    ):
        """send_card_callback(chat_id, card_dict) -> message_id（异步）。
        reply_callback(msg, text) -> None（异步）。"""
        self._store = store
        self._scanner = scanner
        self._send_card = send_card_callback
        self._reply = reply_callback
        self._min_level = min_level
        self._max_bytes = max_pkg_mb * 1024 * 1024

    def is_capability_package(self, msg: InboundMessage) -> bool:
        """消息是否是能力包提交：有文件附件且扩展名在白名单内。"""
        resources = getattr(msg, "resources", None) or []
        for res in resources:
            fname = (getattr(res, "file_name", "") or "").lower()
            if any(fname.endswith(s) for s in CAPABILITY_PKG_SUFFIXES):
                return True
        return False

    async def handle_package_message(self, msg: InboundMessage) -> None:
        """处理一条能力包提交消息（main.py 消息分流调用）。"""
        sender_id = msg.sender.open_id
        chat_id = msg.conversation.chat_id

        # ── 权限：仅 L2 可提交 ──
        perm = get_user_permission(sender_id, chat_id)
        if perm.level < self._min_level:
            logger.warning("[capability] ingest denied: sender=%s level=%s", sender_id, perm.level)
            await self._reply(msg, "⛔ 能力包接入仅对 L2 管理员开放。")
            return

        # ── 找到能力包附件并下载 ──
        resources = getattr(msg, "resources", None) or []
        pkg_res = None
        for res in resources:
            fname = (getattr(res, "file_name", "") or "").lower()
            if any(fname.endswith(s) for s in CAPABILITY_PKG_SUFFIXES):
                pkg_res = res
                break
        if pkg_res is None:
            await self._reply(msg, "未识别到能力包文件（支持 zip / agent.yaml / SKILL.md）")
            return

        work_root = self._store.record_dir(
            CapabilityRecord(capability_id="_tmp")
        ).parent  # pending 根
        work_root.mkdir(parents=True, exist_ok=True)
        tmp_dir = work_root / f"_tmp-{msg.id or 'noid'}"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)

        try:
            # 下载由 main.py 注入的 channel 完成（见 wire_channel_downloader）
            pkg_path = await self._download(msg, pkg_res, tmp_dir)
            if pkg_path is None:
                await self._reply(msg, "⚠️ 能力包下载失败，请重试")
                return
            if pkg_path.stat().st_size > min(self._max_bytes, _MAX_PKG_BYTES):
                await self._reply(msg, f"⚠️ 包体超过 {self._max_bytes // (1024*1024)}MB 上限")
                return

            # ── 解包 ──
            unpack_dir = tmp_dir / "pkg"
            unpack_dir.mkdir()
            if pkg_path.suffix.lower() == ".zip":
                try:
                    safe_extract_zip(pkg_path, unpack_dir)
                except ValueError as e:
                    await self._reply(msg, f"🚨 拒绝接入：{e}")
                    return
                # zip 根目录如果只包了一个子目录，下沉一层
                unpack_dir = self._flatten_single_root(unpack_dir)
            else:
                shutil.copy2(pkg_path, unpack_dir / pkg_path.name)

            # ── 扫描 ──
            report = self._scanner.scan(unpack_dir)

            # ── 建档 ──
            rec = self._store.new_record(
                capability_type=report.capability_type,
                name=report.capability_name,
                sender_id=sender_id,
                sender_name=msg.sender.display_name or "",
                message_id=msg.id or "",
            )
            # 把 tmp 目录整体搬进档案目录（pkg/ + 原始包）
            final_dir = self._store.record_dir(rec)
            shutil.rmtree(final_dir, ignore_errors=True)
            shutil.move(str(tmp_dir), str(final_dir))
            # 档案内路径归一：解包结果统一放 pkg/
            pkg_src = final_dir / "pkg"
            if not pkg_src.exists():
                # 单文件包：解包目录就是 final_dir 本身的内容，无需调整
                pass

            rec.parse_ok = report.parse_ok
            rec.parse_error = report.parse_error
            rec.risk_level = report.risk_level
            rec.scan_findings = [
                {"severity": f.severity, "rule": f.rule, "detail": f.detail}
                for f in report.findings
            ]
            self._store.update(rec)

            # ── high 风险自动拒绝（D3）──
            if report.blocked:
                self._store.update(rec, "rejected")
                lines = report.summary_lines()
                await self._reply(
                    msg,
                    "🚨 能力包未通过静态扫描，已自动拒绝：\n"
                    + "\n".join(f"- {item}" for item in lines),
                )
                return

            # ── 发审批卡片（description 原文以 code_block 展示 — 路由注入面可见）──
            card = build_pending_card(rec, report.summary_lines(),
                                      description=report.capability_description)
            # 审批卡发不出去不能连累回执：档案已经落盘，用户至少要知道
            # 「东西收了、卡没发出来」，而不是看到一句异常。
            try:
                await self._send_card(chat_id, card)
            except Exception as e:
                logger.warning("[capability] 审批卡发送失败 %s: %s",
                               rec.capability_id, str(e)[:140])
            logger.info(
                "[capability] pending created: %s (%s/%s, risk=%s) by %s",
                rec.capability_id, rec.type, rec.name, rec.risk_level, sender_id,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def wire_channel_downloader(self, download_fn) -> None:
        """注入 channel 下载协程：async (msg, resource, dest_dir) -> Path|None。"""
        self._download_fn = download_fn

    # ── 内部 ──

    async def _download(self, msg: InboundMessage, resource, dest_dir: Path) -> Path | None:
        """下载附件；下载协程由 main.py 经 wire_channel_downloader 注入。"""
        fn = getattr(self, "_download_fn", None)
        if fn is None:
            return None
        return await fn(msg, resource, dest_dir)

    @staticmethod
    def _flatten_single_root(unpack_dir: Path) -> Path:
        """zip 根只含一个子目录时把能力目录下沉到 pkg 根。"""
        entries = [e for e in unpack_dir.iterdir()]
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        if len(dirs) == 1 and not files:
            inner = dirs[0]
            tmp = unpack_dir.parent / "_flatten"
            shutil.move(str(inner), str(tmp))
            shutil.rmtree(unpack_dir)
            tmp.rename(unpack_dir)
        return unpack_dir
