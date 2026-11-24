"""飞书能力 MCP server（stdio）— 发消息 / 回复 / 读文档 / 读会话上下文。

负责：工具参数校验（三红线硬拒）、调用 `app.protocols.feishu_send` 与
`app.protocols.lark_cli`、把结果整形为统一 JSON 回执。
不负责：会话与事件接入（`app/gateway/channel/`）、路由与提示词（dsh 侧）。
调用方：dsh 按 `[projects.mcp.feishu]` 以子进程挂载。

**读会话上下文是这个 server 的正式能力**，不是可选装饰：群里的需求常常是
「接着刚才那段聊」，模型只看到本轮一句话时只能反问，用户体感是「它没在看群」。
读接口用 bot 自己的 tenant token，可见范围天然等于「机器人在哪个会话里」。

拒绝口径（AC-3.2）：目标非法、文本超长一律返回 `{"ok": false, "error": "REJECT: ..."}`，
**不抛异常**——模型需要看到可改参数的拒绝原因，框架异常只会变成不可读的工具错误。
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from app.mcp._common import (
    app_config,
    check_chat_id,
    check_doc_target,
    check_message_id,
    check_text,
    current_chat_id,
    fail,
    ok,
    reject,
)
from app.mcp._common import (
    sender_name as _sender_name,
)
from app.protocols.notify_card import (
    DEFAULT_TEMPLATE,
    build_notify_card,
    check_card_args,
    sender_of,
)
from app.protocols.feishu_send import (
    FeishuSendError,
    format_conversation,
    recent_messages,
    reply_text,
    send_card,
    send_text,
)
from app.protocols.feishu_send import (
    chat_members as _members,
)
from app.protocols.lark_cli import LarkCli, LarkCliError

logger = logging.getLogger("mcp.feishu")

mcp = FastMCP("feishu")

# 读文档上限：与 lark_cli 默认值同口径，长文档分次按需读，不在工具层静默放大
_DOC_MAX_CHARS = 4000


def _text_guard(text: str) -> str:
    """返回拒绝文案，空串=通过。"""
    return check_text(text, app_config().mcp.feishu_max_text_chars)


@mcp.tool()
def send_message(chat_id: str, text: str) -> str:
    """向飞书群（oc_*）或用户（ou_*）发文本消息，返回 message_id。

    目标必须是机器人可寻址的会话 ID；文本单条有长度上限，超长需自行拆分。
    """
    reason = check_chat_id(chat_id) or _text_guard(text)
    if reason:
        return reject(reason)
    try:
        mid = send_text(chat_id.strip(), text)
    except FeishuSendError as e:
        logger.warning("[mcp.feishu] send_message failed: %s", e)
        return fail(f"发送失败：{e}")
    return ok(message_id=mid)


@mcp.tool()
def send_card_message(
    chat_id: str,
    title: str,
    body: str,
    from_name: str = "",
    fields: dict[str, str] | None = None,
    note: str = "",
    template: str = DEFAULT_TEMPLATE,
) -> str:
    """给别人发一张通知卡片（卡片副标题会写明「来自 {发送者} 的消息」）。

    **结构由本工具钉死**，你只填纯文本，不要也不能自己拼卡片 JSON：
    头部标题、正文、若干净字段行、脚注（note + 时间）。要发「几点开会 / 工单
    进度 / 一段通知」这类内容就用它，比纯文本显眼。

    参数说明：
    - chat_id：目标会话，`oc_` 群或 `ou_` 用户 open_id（与 send_message 同口径）。
      不知道对方 open_id 时，先用 chat_members 从共同所在的群查。
    - title：卡片标题，单行，≤60 字。
    - body：正文，markdown，≤2000 字。`<at>` 标签会被剥掉（不允许 @全体）。
    - from_name：署名人；留空则用本次会话的真实发送者，模型不要自己编。
    - fields：`{"字段名": "值"}` 形式的若干行事实，≤12 行，长出就走 body。
    - note：脚注一句话，≤300 字（如「请回我一句确认」）。
    - template：`blue` / `green` / `yellow` / `red` / `grey` 等配色，默认 blue。

    发送结果含 message_id；由你转达的内容请在 note 或 body 里说明来源，
    不要 impersonate 收件人的同事。
    """
    reason = check_chat_id(chat_id)
    if reason:
        return reject(reason)
    reason = check_card_args(title, body, fields=fields, note=note, template=template)
    if reason:
        return reject(reason)
    sender = sender_of(_sender_name(), from_name)
    try:
        card = build_notify_card(
            title=title, body=body, sender=sender,
            fields=fields, note=note, template=template,
        )
    except ValueError as e:
        return reject(str(e))
    except Exception as e:
        logger.warning("[mcp.feishu] 通知卡渲染失败: %s", e)
        return fail(f"卡片渲染失败：{type(e).__name__}: {e}")
    try:
        mid = send_card(chat_id.strip(), card)
    except FeishuSendError as e:
        logger.warning("[mcp.feishu] send_card_message failed: %s", e)
        return fail(f"卡片发送失败：{e}")
    logger.info("[mcp.feishu] 通知卡已发 chat=%s from=%s template=%s",
                chat_id[:12], sender, template)
    return ok(message_id=mid, sender=sender, template=template)


@mcp.tool()
def reply_message(message_id: str, text: str) -> str:
    """回复（引用）指定飞书消息，返回新消息的 message_id。

    只认机器人可寻址的消息 ID（om_*）。
    """
    reason = check_message_id(message_id) or _text_guard(text)
    if reason:
        return reject(reason)
    try:
        mid = reply_text(message_id.strip(), text)
    except FeishuSendError as e:
        logger.warning("[mcp.feishu] reply_message failed: %s", e)
        return fail(f"回复失败：{e}")
    return ok(message_id=mid)


@mcp.tool()
async def read_doc(doc_url: str) -> str:
    """读取飞书文档正文（markdown，走 lark-cli 的用户身份，需本机已授权）。

    接受飞书文档/wiki 链接或文档 token；正文有截断上限，返回里会注明。
    """
    reason = check_doc_target(doc_url)
    if reason:
        return reject(reason)
    try:
        result = await LarkCli().get_doc_content(doc_url.strip(), max_chars=_DOC_MAX_CHARS)
    except LarkCliError as e:
        logger.warning("[mcp.feishu] read_doc failed: %s", e)
        hint = "（缺授权 scope，先跑 lark-cli auth login 补齐）" if e.missing_scopes else ""
        return fail(f"读取文档失败：{e}{hint}")
    return ok(token=result["token"], revision_id=result["revision_id"],
              content=result["content"])


@mcp.tool()
def read_conversation(chat_id: str = "", limit: int = 20) -> str:
    """读取**当前会话**最近的消息，用来理解「刚才在聊什么」。

    用户说「总结一下刚才的讨论」「上面那条链接说的是什么」「按我们刚说的做」
    这类依赖上下文的话时调用。返回按时间正序排列，带时间与说话人。
    chat_id 省略 = 当前会话（推荐）；显式指定只能读机器人所在的会话。

    边界：只能读机器人所在的会话（群里 / 与机器人私聊）；卡片消息正文若是
    图片渲染，只能拿到标题。
    """
    target = chat_id.strip() or current_chat_id()
    if not target:
        return reject("没指定 chat_id，且本会话没有注入上下文——请在飞书会话里直接问我")
    reason = check_chat_id(target)
    if reason:
        return reject(reason)
    try:
        messages = recent_messages(target, limit)
    except FeishuSendError as e:
        logger.warning("[mcp.feishu] read_conversation failed: %s", e)
        return fail(f"读取会话消息失败：{e}")
    if not messages:
        return ok(chat_id=target, count=0,
                  text="（该会话最近没有可读消息，或机器人不在其中）")
    return ok(
        chat_id=target,
        count=len(messages),
        text=format_conversation(messages),
    )


@mcp.tool()
def chat_members(chat_id: str = "") -> str:
    """列出会话成员（open_id → 显示名），用于把 open_id 翻译成人名、确认谁在群里。

    chat_id 省略 = 当前会话。
    """
    target = chat_id.strip() or current_chat_id()
    if not target:
        return reject("没指定 chat_id，且本会话没有注入上下文")
    reason = check_chat_id(target)
    if reason:
        return reject(reason)
    names = _members(target)
    if not names:
        return fail("拿不到成员名单：机器人可能不在该会话，或缺少通讯录权限")
    return ok(count=len(names), members=[
        {"open_id": oid, "name": name} for oid, name in names.items()
    ])


if __name__ == "__main__":
    mcp.run()
