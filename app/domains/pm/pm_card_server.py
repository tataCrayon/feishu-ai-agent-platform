"""PM 卡片回调的 HTTP 接收端 —— 让按钮真的能点。

为什么要单独开一个端口
----------------------
飞书「卡片回调」的事件通道是 `card.action.trigger`，走的是应用的长连接（WS）。
主链 `app.main` 已经吃掉了那个 WS 的唯一配额，`pm_run` 再连会干扰（同 app 双长连接
是竞争消费，2026-09-14 出过事故：cc-connect 抢走全部消息）。

所以这条通道改成 **HTTP 回调**：在飞书开放平台把「卡片回调」配成
「请求地址」模式（`https://<你的地址>/pm/card`），飞书 POST 过来，本服务处理。
但内网机器没有公网地址，因此实际用的是**主链代收**：

    飞书 ──WS──> app.main（唯一长连接）
                    │ 识别出是 PM 卡片的回调（value 里有 pn 键）
                    └─ POST http://127.0.0.1:9113/pm/card ──> pm_run

本模块就是那个 9113 端点。

为什么不让主链直接处理
----------------------
草稿状态在 `pm_run` 的进程内存里（`pm_nudge._PENDING`），主链拿不到、也不该拿。
主链只做「搬运」，权威状态留在它出生的进程。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

logger = logging.getLogger("pm_hub.card_server")

# 回调处理函数签名：接收飞书事件的 dict，返回要回给点按者的话（空串=不回复）
Handler = Callable[[dict], str]

_server: Optional[HTTPServer] = None
_thread: Optional[threading.Thread] = None


def _make_handler(callback: Handler) -> type:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/pm/card"):
                self._reply(404, {"detail": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                event = json.loads(raw.decode("utf-8"))
            except Exception as e:
                logger.warning("卡片回调体解析失败: %s", e)
                self._reply(400, {"detail": "bad json"})
                return

            try:
                text = callback(event if isinstance(event, dict) else {})
            except Exception as e:
                # 回调失败不能让飞书侧看到 5xx：飞书会把 5xx 当成「服务不可用」
                # 并重试，而状态可能已经落盘了，重试会造成二次执行。
                logger.exception("卡片回调处理失败")
                self._reply(200, {"toast": f"处理出错：{type(e).__name__}"})
                return
            self._reply(200, {"toast": text} if text else {"toast": "已处理"})

        def do_GET(self) -> None:  # noqa: N802
            # 主链启动时探活
            self._reply(200, {"ok": True})

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # 默认实现往 stderr 打每条请求，daemon 日志会被刷满
            logger.debug("[card] " + fmt, *args)

    return _Handler


def start(callback: Handler, port: int = 9113, host: str = "127.0.0.1") -> bool:
    """在后台线程起 HTTP 服务。返回是否起成功。

    只绑 127.0.0.1：这个端点能以 PM 的 lark-cli 身份发消息（真发给同事），
    对网络开放等于把「以他名义说话」的能力交出去。
    """
    global _server, _thread
    if _server is not None:
        return True
    try:
        server = HTTPServer((host, port), _make_handler(callback))
    except OSError as e:
        logger.error(
            "卡片回调端口 %s 起不来（被占用？）：%s —— 按钮点下去不会有反应，"
            "但文字口令（「确认」/「第2条改成：…」）仍然可用", port, e,
        )
        return False
    _server = server
    _thread = threading.Thread(target=server.serve_forever, name="pm-card-server", daemon=True)
    _thread.start()
    logger.info("卡片回调服务已启动: http://%s:%s/pm/card", host, port)
    return True


def stop() -> None:
    global _server, _thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            logger.debug("card server shutdown failed", exc_info=True)
        _server = None
    _thread = None


def url(port: int = 9113, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/pm/card"
