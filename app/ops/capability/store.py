"""能力档案存储 — 每次接入一条 CapabilityRecord，目录即状态。

落盘布局（bot/inbox/ 整体 gitignore，凭据红线：原文不上库）：

    bot/inbox/pending/cap-.../     原始包 + 解包目录 + manifest.json
    bot/inbox/rejected/cap-.../
    bot/inbox/failed/cap-.../
    bot/inbox/archive/cap-.../     approved 后归档（含被覆盖的旧版本）

manifest.json 记录全生命周期字段（提交人/扫描/审批/部署结果），
即设计稿 3.4 节 CAPABILITY 数据模型的持久化形态。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("capability.store")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # app/ops/capability/ -> feishu-agent-platform/
INBOX_DIR = PROJECT_DIR / "bot" / "inbox"

# 状态机（设计稿 4.2）
STATUS_RECEIVED = "received"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_ACTIVE = "active"


@dataclass
class CapabilityRecord:
    """一次能力接入的全生命周期档案。"""

    capability_id: str  # cap-YYYYMMDD-xxxx
    type: str = ""  # skill / agent / mcp
    status: str = STATUS_RECEIVED
    name: str = ""
    risk_level: int = 0
    scan_findings: list[dict] = field(default_factory=list)
    parse_ok: bool = True
    parse_error: str = ""
    source_sender_id: str = ""
    source_sender_name: str = ""
    source_message_id: str = ""
    decided_by: str = ""  # 审批人 open_id
    decided_by_name: str = ""  # 审批人飞书显示名（解析失败留空，卡片退回 open_id）
    decided_at: str = ""
    deploy_result: str = ""  # ok / error:...
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class CapabilityStore:
    """能力档案的落盘与状态迁移。目录迁移即状态迁移。"""

    def __init__(self, inbox_dir: Path = INBOX_DIR):
        self._inbox = inbox_dir

    # ── 路径约定 ──

    def _dir_for(self, status: str, capability_id: str) -> Path:
        return self._inbox / status / capability_id

    def record_dir(self, rec: CapabilityRecord) -> Path:
        return self._dir_for(rec.status, rec.capability_id)

    # ── 创建 ──

    def new_record(
        self,
        capability_type: str,
        name: str,
        sender_id: str,
        sender_name: str,
        message_id: str,
    ) -> CapabilityRecord:
        """生成新档案并落盘到 pending/。capability_id 时间戳派生保证唯一。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        cap_id = f"cap-{ts}-{int(time.time() * 1000) % 10000:04d}"
        rec = CapabilityRecord(
            capability_id=cap_id,
            type=capability_type,
            status=STATUS_PENDING,
            name=name,
            source_sender_id=sender_id,
            source_sender_name=sender_name,
            source_message_id=message_id,
        )
        d = self.record_dir(rec)
        d.mkdir(parents=True, exist_ok=True)
        self._write_manifest(rec)
        return rec

    # ── 查询 ──

    def get(self, capability_id: str) -> CapabilityRecord | None:
        """按 id 在各状态目录（含 archive）中查找并读取 manifest。"""
        for status in (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
                       STATUS_FAILED, STATUS_ACTIVE):
            manifest = self._dir_for(status, capability_id) / "manifest.json"
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    return CapabilityRecord(**data)
                except Exception as e:
                    logger.warning("[capability] manifest read failed %s: %s", manifest, e)
                    return None
        # archive/ 下有正式归档与备份目录两种形态
        archive_root = self._inbox / "archive"
        if archive_root.is_dir():
            for d in sorted(archive_root.glob(f"{capability_id}*")):
                manifest = d / "manifest.json"
                if not manifest.exists():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    return CapabilityRecord(**data)
                except Exception:
                    continue
        return None

    def find_by_message(self, message_id: str) -> CapabilityRecord | None:
        """按来源消息 id 查（同一条消息重复提交时复用）。"""
        pending_root = self._inbox / STATUS_PENDING
        if not pending_root.is_dir():
            return None
        for d in sorted(pending_root.iterdir()):
            manifest = d / "manifest.json"
            if not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("source_message_id") == message_id:
                try:
                    return CapabilityRecord(**data)
                except Exception:
                    return None
        return None

    def active_capabilities(self) -> list[CapabilityRecord]:
        """列出当前生效的热插拔能力（按 name 去重，取最新一次部署）。

        archive/ 存放审批通过后的正式归档（status=active 且 deploy_result=ok），
        active/ 存放运行中档案。备份目录（cap-*-backup-*）跳过。
        供 /help 分组与控制台「来源」标识使用；读取失败返回空表（旁路数据，
        绝不阻断主流程）。
        """
        seen: dict[str, CapabilityRecord] = {}
        roots = [self._inbox / "archive", self._inbox / STATUS_ACTIVE]
        for root in roots:
            if not root.is_dir():
                continue
            for d in root.iterdir():
                if not d.is_dir() or "-backup-" in d.name:
                    continue
                manifest = d / "manifest.json"
                if not manifest.exists():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    rec = CapabilityRecord(**data)
                except Exception:
                    continue
                if rec.status != STATUS_ACTIVE or rec.deploy_result != "ok":
                    continue
                old = seen.get(rec.name)
                if old is None or rec.created_at >= old.created_at:
                    seen[rec.name] = rec
        return list(seen.values())

    # ── 状态迁移 ──

    def update(self, rec: CapabilityRecord, new_status: str | None = None) -> None:
        """更新字段并写 manifest；new_status 非空时迁移目录。"""
        old_dir = self.record_dir(rec)
        if new_status:
            rec.status = new_status
        new_dir = self.record_dir(rec)
        if new_dir != old_dir and old_dir.exists():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_dir.exists():
                shutil.rmtree(new_dir)
            shutil.move(str(old_dir), str(new_dir))
        self._write_manifest(rec)

    def archive(self, rec: CapabilityRecord) -> Path:
        """归档到 archive/（审计留档），返回归档目录。"""
        self.update(rec, STATUS_ACTIVE)
        d = self._inbox / "archive" / rec.capability_id
        if d.exists():
            shutil.rmtree(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.record_dir(rec)), str(d))
        return d

    # ── 备份（D4：覆盖同名前备份旧版）──

    def backup_root(self) -> Path:
        """备份根目录：archive/（与归档同层，便于审计追溯）。"""
        return self._inbox / "archive"

    def backup_existing(self, target_dir: Path, capability_id: str) -> Path | None:
        """把将被覆盖的能力目录备份进 archive/cap-.../backup-{name}/。

        Returns:
            备份目录；目标不存在时返回 None。
        """
        if not target_dir.exists():
            return None
        backup_root = self.backup_root() / f"{capability_id}-backup-{target_dir.name}"
        backup_root.parent.mkdir(parents=True, exist_ok=True)
        if backup_root.exists():
            shutil.rmtree(backup_root)
        shutil.copytree(target_dir, backup_root)
        logger.info("[capability] backed up existing %s -> %s", target_dir, backup_root)
        return backup_root

    def discard_pkg_file(self, rec: CapabilityRecord) -> None:
        """删除 pending 目录里上传的原始压缩包（解包后不再需要）。"""
        d = self.record_dir(rec)
        for z in d.glob("*.zip"):
            try:
                z.unlink()
            except OSError:
                pass

    # ── 内部 ──

    def _write_manifest(self, rec: CapabilityRecord) -> None:
        d = self.record_dir(rec)
        d.mkdir(parents=True, exist_ok=True)
        manifest = d / "manifest.json"
        tmp = manifest.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(manifest)
