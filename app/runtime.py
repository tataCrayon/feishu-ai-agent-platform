"""运行期单例登记处 —— 给「进程内其它模块要拿到 main 里的对象」用。

为什么需要这一层：
    `python -m app.main` 启动时，主模块在 `sys.modules` 里叫 **`__main__`**，
    不叫 `app.main`。任何 `from app.main import X` 都会**再执行一遍 main.py**，
    拿到一份全新的模块级变量（`_dsh_gateway` 之类全是初始值 None）。
    表现是「日志明明写着就绪，端点却说未启用」——查起来极费时间。

所以进程级单例统一放这里，`main` 启动时登记，其它模块按需取。
这里只存引用，不持有生命周期（谁创建谁负责关）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("runtime")

_dsh_gateway: Optional[Any] = None
_feishu_channel: Optional[Any] = None


def set_dsh_gateway(gateway: Any) -> None:
    global _dsh_gateway
    _dsh_gateway = gateway


def get_dsh_gateway() -> Optional[Any]:
    """当前 dsh 网关；未启用或尚未就绪时为 None。"""
    return _dsh_gateway


def set_feishu_channel(channel: Any) -> None:
    global _feishu_channel
    _feishu_channel = channel


def get_feishu_channel() -> Optional[Any]:
    return _feishu_channel
