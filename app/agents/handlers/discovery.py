"""Handler 约定式自动发现 — 扫描 handlers 目录，零手工注册。

约定（新增独立 Handler 只需放一个文件，无需改任何其他代码）：
1. 文件名 `{snake_name}_handler.py`（下划线开头跳过，如 `_private_handler.py`）
2. 模块内定义 `build(config, ctx)` 工厂函数 → 用工厂构造实例；
   未定义 build 时取模块内第一个 AgentHandler 子类，`cls(config)` 构造
3. Handler 类属性 `agent_name` 作为注册键

ctx 为启动期共享资源字典（如 {"skill_catalog": store}），
由 main.py 组装后传入，供需要外部依赖的 build 使用。
"""

from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

from app.agents.handlers import AgentHandler, AgentHandlerRegistry
from app.config import RouterConfig

logger = logging.getLogger("agents.handlers.discovery")


def auto_discover_handlers(config: RouterConfig, ctx: dict[str, Any]) -> AgentHandlerRegistry:
    """扫描本目录下 *_handler.py 并注册全部发现的 Handler。

    Args:
        config: RouterConfig（传入各 Handler 构造器）。
        ctx: 启动期共享资源字典（传给 build 工厂）。

    Returns:
        完成注册的 AgentHandlerRegistry。
    """
    registry = AgentHandlerRegistry()
    for path in sorted(Path(__file__).parent.glob("*_handler.py")):
        if path.name.startswith("test_"):
            continue  # 同目录单测不实现 Handler；扫进来只会刷一条「装配失败」噪音
        module_name = f"app.agents.handlers.{path.stem}"
        try:
            module = importlib.import_module(module_name)
            handler = _instantiate(module, config, ctx)
            if handler is None:
                continue
            registry.register(handler.agent_name, handler)
            logger.info("[handlers] auto-registered %s ← %s", handler.agent_name, path.name)
        except Exception as e:
            # 单个 Handler 失败不阻断启动 — 该 agent 运行时走 transport 兜底通道
            logger.warning("[handlers] failed to load %s: %s", path.name, e)
    return registry


def _instantiate(
    module: Any, config: RouterConfig, ctx: dict[str, Any]
) -> AgentHandler | None:
    """按约定构造 Handler 实例：build 工厂优先，否则首个子类 cls(config)。"""
    build = getattr(module, "build", None)
    if callable(build):
        return build(config, ctx)

    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, AgentHandler) and obj is not AgentHandler:
            return obj(config)
    logger.warning("[handlers] no build() or AgentHandler subclass in %s", module.__name__)
    return None
