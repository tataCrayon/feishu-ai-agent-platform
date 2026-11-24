"""能力热插拔（Capability Hot-plug）— 管理员审批接入新 Skill/Agent/MCP。

功能简报：docs/features/capability-hotplug.md

数据流：管理员飞书发能力包文件 → ingest 下载/解包 → scanner 静态扫描
→ store 建 pending 档案 → approval_card 发审批卡片 → 管理员点「接入」
→ reloader 落盘 + 热重建（registry/路由工具/graph 原子替换）→ 卡片回执。

安全红线（评审确认 D1-D4）：
- 提交与审批都须 L2 管理员（可同一人，审计留痕）
- MCP 只允许通过新建 Agent 包承载
- high 风险包自动拒绝，无强制通过入口
- 覆盖同名能力前旧版备份进 bot/inbox/archive/
"""

from __future__ import annotations

from app.ops.capability.ingest import CapabilityIngestService
from app.ops.capability.reloader import HotReloader
from app.ops.capability.scanner import CapabilityScanner, ScanFinding, ScanReport
from app.ops.capability.store import CapabilityRecord, CapabilityStore

__all__ = [
    "CapabilityIngestService",
    "CapabilityRecord",
    "CapabilityScanner",
    "CapabilityStore",
    "HotReloader",
    "ScanFinding",
    "ScanReport",
]
