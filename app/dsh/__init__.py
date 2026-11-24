"""dsh 执行面 — ACP 客户端、进程托管与事件翻译。

对外只导出**入口**：`DshSupervisor` / `SupervisorConfig`（进程托管）、
`DshGateway` / `GatewayConfig`（飞书会话桥）、`DshAdapter`（直接驱动会话时用）、
错误类型、权限回调类型。
内部事件与翻译函数在 `app.dsh.events`，需要时从该模块直接取。
分别以下划线私有的是：报文分帧、RPC 编号、profile 初始化、进程回收。

失败语义：跨进程调用抛 `DshError` 子类（见 `adapter.py` 模块 docstring）；
事件翻译抛 `ValueError`；`supervisor.start()` 不抛，返回是否可用。
"""

from __future__ import annotations

from app.dsh.adapter import (
    DshAdapter,
    DshError,
    DshProtocolError,
    DshUnavailableError,
    PermissionHandler,
    PermissionOption,
)
from app.dsh.gateway import DshGateway, GatewayConfig
from app.dsh.supervisor import DshSupervisor, SupervisorConfig

__all__ = [
    "DshAdapter",
    "DshError",
    "DshGateway",
    "DshProtocolError",
    "DshSupervisor",
    "DshUnavailableError",
    "GatewayConfig",
    "PermissionHandler",
    "PermissionOption",
    "SupervisorConfig",
]
