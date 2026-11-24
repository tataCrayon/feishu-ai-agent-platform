"""Dashboard HTTP 服务启动器 — 在 bot 主 asyncio 事件循环内拉起 FastAPI。

用法（在 app/main.py 中）：
    from app.dashboard import start_dashboard_server
    await asyncio.create_task(start_dashboard_server(port=9112))
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.gateway.api.dashboard import router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# archify 产出的架构/结构图 HTML（自包含交互页），控制台「架构视图」tab 内嵌展示
DIAGRAMS_DIR = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
_ARCH_DIAGRAMS = {
    "runtime": DIAGRAMS_DIR / "feishu-agent-platform-runtime.html",
    "lifecycle": DIAGRAMS_DIR / "feishu-agent-platform-request-lifecycle.html",
}

dashboard_app = FastAPI(title="AgentMate Agent Dashboard", version="1.0.0")
dashboard_app.include_router(router)

# PM 能力接口（`/api/pm/*`）：给外部调用方的薄封装，与运维台 `/api/stats/*` 互不重叠。
# 未启用时每条路由自己返回 503，不在挂载期判断——挂载期判断会让「配置改了没重启」
# 表现为路由 404，比 503 更难查。
from app.api.pm_capabilities import pm_router  # noqa: E402

dashboard_app.include_router(pm_router)

# PM 私聊转发的接收端：pm_run 判「不归 PM」时投到这里走完整 dsh 路由。
# 只监听回环、只在本机可达——详见 app/api/pm_relay.py 头部。
from app.api.pm_relay import relay_router  # noqa: E402

dashboard_app.include_router(relay_router)


@dashboard_app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """返回 Dashboard 单页入口 index.html。"""
    return FileResponse(INDEX_HTML)


@dashboard_app.get("/arch/{name}.html", include_in_schema=False)
async def arch_diagram(name: str) -> FileResponse:
    """返回 archify 架构图 HTML（白名单寻址，不接受任意路径）。"""
    path = _ARCH_DIAGRAMS.get(name)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"架构图不存在：{name}")
    return FileResponse(path, media_type="text/html")


async def start_dashboard_server(port: int = 9112, host: str = "0.0.0.0") -> None:
    """在当前 asyncio 事件循环内启动 Dashboard HTTP 服务。

    阻塞调用 — 必须作为 asyncio 后台任务运行（直接 await 会卡死主流程）。
    启动与退出均打日志：端口被占等异常是常见排障点。
    """
    import uvicorn

    logger.info("[dashboard] starting on http://%s:%s", host, port)
    config = uvicorn.Config(
        dashboard_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # 屏蔽 uvicorn 自带的信号处理器，避免作为后台任务时与主进程抢占 SIGINT
    server.install_signal_handlers = lambda: None
    try:
        await server.serve()
    except Exception:
        logger.exception("[dashboard] failed to serve on %s:%s", host, port)
        raise
    logger.info("[dashboard] server stopped on %s:%s", host, port)