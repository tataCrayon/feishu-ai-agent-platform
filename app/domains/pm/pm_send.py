"""外发唯一出口 —— 以李四本人身份往飞书发消息。

为什么以 user 身份而不是 bot
----------------------------
催办话术是「哈喽 进哥，XXX 提测了嘛」，读起来是 PM 在说话。
用 bot 发，对方看到的是一台机器在催，语气立刻变味；而且 bot 与对方
没有会话历史，消息会落在一个陌生会话里。所以固定 `--as user`。

    实测：POST /open-apis/im/v1/messages，receive_id_type=open_id，
    identity=user 可发。返回 message_id，说明真的投出去了。

职责边界
--------
只做「发」：拿到 open_id 和正文，发出去，返回结果。
**不懂该发给谁、该说什么** —— 那是 pm_nudge 的事。
也不做「发不发」的判断 —— 确认动作在调用方，本模块被调用即发。

所以这里没有草稿、没有暂存、没有确认词。那些都在上层。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.domains.pm.feishu_pm_sync import _run_lark_cli

logger = logging.getLogger("pm_hub.send")


class SendError(RuntimeError):
    """发送失败。调用方必须把它当成「没发出去」处理，不能当成功。"""


def send_text(open_id: str, text: str) -> Dict[str, Any]:
    """发一条纯文本私聊。返回 {"message_id":..., "chat_id":...}。

    失败抛 SendError，不吞异常 —— 吞了之后上层会以为发出去了，
    然后在「跟进对方回了什么」这一步永远等不到回复，很难查。
    """
    open_id = (open_id or "").strip()
    body = (text or "").strip()
    if not open_id.startswith("ou_"):
        raise SendError(f"接收人不是 open_id：{open_id!r}")
    if not body:
        raise SendError("正文为空，不发")

    try:
        res = _run_lark_cli([
            "im", "+messages-send",
            "--as", "user",
            "--user-id", open_id,
            "--text", body,
            "--format", "json",
        ])
    except Exception as e:
        raise SendError(f"lark-cli 调用失败：{type(e).__name__}: {e}") from e

    data = res.get("data") or {}
    message_id = data.get("message_id") or ""
    if not res.get("ok") or not message_id:
        raise SendError(f"发送未成功：{str(res)[:300]}")

    logger.info("已发送 open_id=%s message_id=%s", open_id, message_id)
    return {
        "message_id": message_id,
        "chat_id": data.get("chat_id") or "",
        "text": body,
    }


def send_many(items) -> list:
    """批量发。逐条独立成败，一条炸了不影响后面的。

    items: [(open_id, text), ...]
    返回 [{"open_id","ok","message_id"|"error"}, ...]
    """
    out = []
    for open_id, text in items:
        try:
            res = send_text(open_id, text)
            out.append({"open_id": open_id, "ok": True, "message_id": res["message_id"]})
        except SendError as e:
            logger.warning("发送失败 open_id=%s: %s", open_id, e)
            out.append({"open_id": open_id, "ok": False, "error": str(e)[:200]})
    return out