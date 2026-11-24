"""PM 私聊转发客户端 —— 把「不属于 PM」的私聊交给主链（`app.main`）处理。

为什么由 pm_run 转发、而不是主链留白再抢：
    pm_run 每 3 秒**轮询**同一个私聊会话，主链是 WS 推送 —— 两边都能收到同一条
    消息。留白（主链判断「PM 私聊就让路」）意味着主链必须知道 pm_run 的内部状态
    （这条它是接了还是判了不属于自己），那是把两个进程焊死。而 pm_run 天然知道
    自己判了什么，转发是它一句话的事。

转发拓扑：

    pm_run                    app.main（:9112）
      │ POST /api/pm/relay      │
      └───────────────────────► │ 走完整 dsh 路由（7 个 agent）
      ◄───────────────────────┘ │
      回复经 pm_run 的私聊通道发回 │

为什么不让主链直接把回复发到私聊：
    两个进程都会往同一会话发消息。让**一个进程**负责「往这个会话说话」，
    才不会出现顺序错乱（转发结果和 pm_run 自己的推送交错）。所以主链只返回文本，
    发的动作仍在 pm_run。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("pm_hub.relay")

# 主链的内部转发端点。落 dashboard 端口是因为那个 HTTP 服务本来就常驻，
# 不额外开端口（防火墙/端口占用都是额外的部署负担）。
_DEFAULT_URL = "http://127.0.0.1:9112/api/pm/relay"


def relay_url() -> str:
    return os.getenv("PM_RELAY_URL", _DEFAULT_URL)


def available(timeout: float = 2.0) -> bool:
    """主链在不在。探测失败当「不在」——转发不了就由 pm_run 自己答，不能静默丢消息。"""
    try:
        resp = requests.get(relay_url().replace("/relay", "/relay/ping"), timeout=timeout)
        return resp.status_code == 200
    except Exception as e:
        logger.debug("relay 探测失败: %s", e)
        return False


def forward(text: str, sender_id: str, chat_id: str = "",
            timeout: float = 120.0) -> Optional[str]:
    """把一条私聊转给主链，返回它的回复文本。

    Returns:
        回复文本；主链不在 / 处理失败 / 返回空 → None（调用方据此走本地兜底）。

    超时给到 120s：主链要过 debounce 聚合 + dsh 会话 + 可能的工具调用，
    悄悄话式的短超时会把「正在跑工单查询」误判成失败。
    """
    try:
        resp = requests.post(
            relay_url(),
            json={"text": text, "sender_id": sender_id, "chat_id": chat_id},
            timeout=timeout,
        )
    except Exception as e:
        logger.debug("relay 投递失败: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning("relay 返回 %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        body = resp.json()
    except Exception:
        logger.warning("relay 返回非 JSON: %s", resp.text[:200])
        return None
    reply = str(body.get("reply") or "").strip()
    return reply or None
