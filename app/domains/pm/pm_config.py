"""PM 绑定配置的唯一读取口。

为什么单独一个文件而不是各处 `load_config().pm`：
1. `load_config()` 每次都要读盘解析 TOML，`pm_watch` / `pm_thread` 这类模块在
   循环里被反复 import，调用点分散后会变成每次都解析一遍。
2. 模块级常量（`pm_hub_engine.SPREADSHEET_TOKEN`）在 import 期求值，必须能在
   不依赖任何上层模块的前提下拿到值——本文件只依赖 `app.config`。
3. 读不到配置时**不抛异常**：PM 这一段默认关闭，缺配置应表现为「能力不可用」，
   而不是把整个进程拖死。缺哪个值由调用方按需检查（空串即未配）。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import PmConfig, load_config

logger = logging.getLogger("pm_hub.config")


@lru_cache(maxsize=1)
def pm_config() -> PmConfig:
    """本进程的 PM 绑定配置（只读一次，缓存）。"""
    try:
        return load_config().pm
    except Exception as e:
        logger.warning("[pm] 配置读取失败，PM 能力按未配置处理：%s", e)
        return PmConfig()


def reload_pm_config() -> PmConfig:
    """丢弃缓存重读——只给测试与「改完配置不想重启」的场景用。"""
    pm_config.cache_clear()
    return pm_config()


def require_identity() -> PmConfig:
    """要求身份已配置。

    Raises:
        RuntimeError: 缺 open_id 时。调用方据此拒绝执行，而不是往空 open_id 发消息。
    """
    cfg = pm_config()
    if not cfg.identity.open_id:
        raise RuntimeError("PM 身份未配置：请在 config.toml [projects.pm.identity] 填 open_id")
    return cfg


def owner_name() -> str:
    """写进「负责人」列 / 卡片署名的名字。

    取 `names[0]`（名字的规范写法）。未配置时返回空串——调用方据此跳过写表，
    而不是往表格里填一个字面量占位符。
    """
    names = pm_config().identity.names
    return names[0] if names else ""
