"""主链的内部转发端点 —— 接收 pm_run 抛过来的「不属于 PM」的私聊。

为什么是主链接收而不是主链主动抢：
    两个进程都能收到同一条私聊（pm_run 轮询、主链 WS）。谁答不能靠抢，
    要有一个**唯一判定者**。PM 私聊的判定者是 pm_run（它每轮都在读那条会话，
    知道自己判了什么），所以由它决定「这条我接不了」，再投递过来。

    HTTP 而不是消息队列：两边本来就在同一台机器，加一条可靠通道（重试、死信、
    顺序）不是这次要解决的问题；同步 HTTP 的失败是**显式**的，pm_run 拿到
    非 200 就能退回本地兜底，比异步队列的静默积压安全。

回复不走本端发送：
    主链只返回文本，由 pm_run 发回私聊。同一个会话只有一个说话方，
    才不会出现 pm_run 的推送与转发结果交错。

安全问题：
    只监听 127.0.0.1 且校验 peer。本端点会以「主链完整权限」处理任意文本，
    对外暴露等于把 dsh 的全部工具开放给任何能访问 :9112 的人。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("pm.relay_endpoint")

relay_router = APIRouter(prefix="/api/pm/relay", tags=["pm-relay"])

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class RelayIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    sender_id: str = Field(default="", max_length=128)
    chat_id: str = Field(default="", max_length=128)


class RelayOut(BaseModel):
    reply: str = ""


def _require_loopback(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in _LOOPBACK:
        logger.warning("[pm.relay] 拒绝非本机调用：%s", host)
        raise HTTPException(status_code=403, detail="relay 端点只接受本机调用")


@relay_router.get("/ping")
async def ping(request: Request) -> dict:
    """pm_run 启动时/转发前探活。**不触发任何模型调用**——探活烧 token 是
    之前踩过的坑（健康探测每次发 prompt 把会话撑爆）。"""
    _require_loopback(request)
    return {"ok": True}


@relay_router.post("", response_model=RelayOut)
async def relay(body: RelayIn, request: Request) -> RelayOut:
    """把一条私聊交给 dsh 内核处理，返回答复文本。

    为什么直接调 DshGateway 而不是复用 `_process_batch`：
    后者带聚合器 debounce、进度卡、记忆召回、任务档案一整套链路，都是**面向
    飞书会话**的。转发来的消息已经由 pm_run 收过一次，再走一遍会重复回执、
    而且进度卡会发到这个私聊里跟 pm_run 的回复打架。这里只要「问题 → 答案」。
    """
    _require_loopback(request)

    # 从 runtime 取而不是 `from app.main import ...`：`python -m app.main` 下
    # 主模块叫 `__main__`，import `app.main` 会**再执行一遍** main.py，
    # 拿到全 None 的新副本（端点报「dsh 未启用」而日志说就绪）。
    from app.runtime import get_dsh_gateway

    gateway = get_dsh_gateway()
    if gateway is None:
        raise HTTPException(status_code=503, detail="dsh 未启用，主链无法处理转发")

    chat_id = body.chat_id or "pm-relay"
    logger.info(
        "[pm.relay] 收到转发 sender=%s chat=%s text=%s",
        body.sender_id or "-", chat_id, body.text[:60],
    )
    try:
        reply = await gateway.handle(
            body.text,
            chat_id=chat_id,
            caller={"sender_id": body.sender_id, "sender_name": ""},
        )
    except Exception as e:
        logger.exception("[pm.relay] 主链处理失败")
        raise HTTPException(status_code=502, detail=f"处理失败：{type(e).__name__}") from e

    reply = (reply or "").strip()
    if not reply:
        raise HTTPException(status_code=502, detail="主链未产出答复")
    return RelayOut(reply=reply)
