"""以 bot 身份向飞书会话收发消息 / 读取会话内容（测试、运维与 dsh 工具共用）。

命令行用法（项目根）：
    python app/protocols/feishu_send.py <chat_id> <文本> [--reply <message_id>]
    python app/protocols/feishu_send.py --default "要发的内容"   # 发到授权测试私聊
    python app/protocols/feishu_send.py --read <chat_id> [--limit 20]  # 读最近消息

凭据来源：config.toml 的 app_id + .env 的 FEISHU_APP_SECRET（与 bot 同源）。

失败语义：网络/鉴权/业务码失败统一抛 `FeishuSendError`（含飞书返回码与说明），
不 `SystemExit`——本模块同时被 MCP 工具调用，进程退出只能由脚本入口决定。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.protocols.feishu_card import ensure_button_behaviors

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CHAT = "oc_chat0000000000000000000000"  # 授权测试私聊（张三）

_IM_BASE = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuSendError(RuntimeError):
    """发消息失败（凭据缺失 / 鉴权失败 / 飞书业务码非 0 / 网络异常）。"""


def _load_env() -> None:
    env = PROJECT_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _app_id() -> str:
    s = (PROJECT_DIR / "config.toml").read_text(encoding="utf-8")
    m = re.search(r'app_id\s*=\s*"([^"]+)"', s)
    if not m:
        raise FeishuSendError("config.toml 中未找到 app_id")
    return m.group(1)


def _post(url: str, payload: dict, token: str = "") -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except OSError as e:
        raise FeishuSendError(f"请求飞书失败：{type(e).__name__}: {e}") from e


def _tenant_token() -> str:
    _load_env()
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not secret:
        raise FeishuSendError(".env 缺 FEISHU_APP_SECRET")
    tok = _post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": _app_id(), "app_secret": secret},
    )
    if tok.get("code") != 0:
        raise FeishuSendError(f"获取 tenant_access_token 失败: {tok}")
    return str(tok["tenant_access_token"])


def _receive_id_type(receive_id: str) -> str:
    """飞书 im/v1/messages 的 receive_id_type 推导（与 lark_channel outbound 同规则）。

    只认前缀：`ou_` 是人（私聊）、`oc_` 是群，其余按 chat_id 处理（老调用方传的都是
    群 id）。写死 chat_id 会让 `ou_` 直投报 400，这是本工具最早只有群发时踩过的坑。
    """
    return "open_id" if receive_id.startswith("ou_") else "chat_id"


def _body(text: str) -> dict:
    return {"msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False)}


def _send(chat_id: str, msg_type: str, content: dict) -> str:
    """发一条消息（text / interactive 共用一条鉴权路径）。"""
    out = _post(
        f"{_IM_BASE}?receive_id_type={_receive_id_type(chat_id)}",
        {"receive_id": chat_id, "msg_type": msg_type,
         "content": json.dumps(content, ensure_ascii=False)},
        token=_tenant_token(),
    )
    if out.get("code") != 0:
        raise FeishuSendError(f"发送失败: {out}")
    return str(out["data"]["message_id"])


def send_text(chat_id: str, text: str) -> str:
    """发文本消息，返回 message_id。chat_id 为 oc_*（群）或 ou_*（用户 open_id）。"""
    return _send(chat_id, "text", {"text": text})


def send_image(chat_id: str, image_path: str) -> str:
    """发一张本地图片，返回 message_id。`chat_id` 同样支持 `ou_` / `oc_`。

    两步走是飞书的接口要求：先 `im/v1/images` 传文件拿 `image_key`，
    再 `im/v1/messages` 用 `msg_type=image` 投出去。两处都用 tenant token。

    用途：授权二维码、报表截图这类「必须看图」的内容。文字里贴 URL 用户还得复制，
    二维码扫码一步到位。文件不存在时抛 `FeishuSendError`，不静默跳过。
    """
    path = Path(image_path)
    if not path.is_file():
        raise FeishuSendError(f"图片不存在：{image_path}")

    boundary = f"----AgentMate{int(time.time() * 1000)}"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n',
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {_tenant_token()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            up = json.loads(resp.read().decode("utf-8"))
    except OSError as e:
        raise FeishuSendError(f"上传图片失败：{type(e).__name__}: {e}") from e
    if up.get("code") != 0:
        raise FeishuSendError(f"上传图片失败: {up}")

    image_key = str((up.get("data") or {}).get("image_key") or "")
    if not image_key:
        raise FeishuSendError(f"上传图片没有返回 image_key: {up}")
    return _send(chat_id, "image", {"image_key": image_key})


def send_card(chat_id: str, card: dict) -> str:
    """发交互卡片（msg_type=interactive），返回 message_id。

    chat_id 可以是 `oc_` 群/群内私聊，也可以是 `ou_` 用户 open_id——按前缀自动选
    `receive_id_type`（`ou_` 直投对方与机器人的私聊）。

    **按钮必须补 behaviors**：这里与 `BotChannel.send_card` 是两条独立出口，
    dsh 的 MCP 子进程只有这一条（没有 SDK channel 对象）。少了垫片，卡片照样
    发得出去、按钮点下去却没有任何回调——飞书客户端提示「目标回调服务当前未在线」，
    发送端日志一切正常。补在出口而不是各卡片构造函数里，是因为漏一处就是
    「有一类卡片的按钮永远点不动」，而这种事只能靠统一入口保证。
    """
    return _send(chat_id, "interactive", ensure_button_behaviors(dict(card)))


def update_card(message_id: str, card: dict) -> bool:
    """就地更新已发出的卡片（PATCH /im/v1/messages/{message_id}）。

    与 `send_card` 的区别只在「有没有新消息」：重发会在聊天记录里堆一串废卡，
    而按钮点完只该让**原来那张**变样。走 tenant token，与发送同一条鉴权路径。

    只能在发送后一定时间内 patch（飞书对卡片更新有时间窗），超窗会返回错误码；
    调用方按 False 处理时再考虑重发，不要在这里静默重发。
    """
    token = _tenant_token()
    url = f"{_IM_BASE}/{message_id}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": json.dumps(
            ensure_button_behaviors(dict(card)), ensure_ascii=False)}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except OSError as e:
        raise FeishuSendError(f"更新卡片失败：{type(e).__name__}: {e}") from e
    if out.get("code") != 0:
        raise FeishuSendError(f"更新卡片失败: {out}")
    return True


# ── 读会话：最近消息与成员名 ─────────────────────────────────────────────
#
# 为什么放在这里而不是让模型自己拼 API：dsh 会话里的模型只有 web_fetch（打不到
# 内网鉴权接口）和 MCP 工具。要让「读懂群里刚才在聊什么」成为**能力**而不是
# 模型临场发挥，就必须有一个带 tenant_access_token 的工具。
#
# 权限前提（实测）：机器人得在群里 / 是这对私聊的一方。用 user 身份的 search
# 接口读不到别的群，`im/v1/messages?container_id_type=chat` 用的是机器人自己的
# 可见范围，这也是「谁能看到什么」的正确边界。

_IM_CHAT_BASE = "https://open.feishu.cn/open-apis/im/v1"


def _get(path: str, params: dict[str, str] | None = None) -> dict:
    """带 bot 身份的 GET。业务码非 0 抛 FeishuSendError（含飞书原文说明）。"""
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"{path}{query}",
        headers={"Authorization": f"Bearer {_tenant_token()}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 飞书把「机器人不在群里」这类也说成 HTTP 400 + JSON，读 body 才拿得到原因
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            raise FeishuSendError(f"请求飞书失败：HTTP {e.code}") from e
    except OSError as e:
        raise FeishuSendError(f"请求飞书失败：{type(e).__name__}: {e}") from e


def list_chats() -> list[tuple[str, str]]:
    """机器人所在的群列表 [(chat_id, name)]。

    纯 HTTP（tenant token），不依赖 SDK 长连接——dsh 的 MCP 子进程里没有 channel
    对象，但「我在哪些群」这个事实照样查得到，定时任务按群名投递才有解。
    """
    chats: list[tuple[str, str]] = []
    page_token = ""
    for _ in range(10):  # 上限 1000 个群，够任何团队规模
        params = {"page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        out = _get(f"{_IM_CHAT_BASE}/chats", params)
        if out.get("code") != 0:
            raise FeishuSendError(f"读取群列表失败：{out.get('msg') or out}")
        data = out.get("data") or {}
        for item in data.get("items") or []:
            cid = str(item.get("chat_id") or "")
            if cid:
                chats.append((cid, str(item.get("name") or "")))
        page_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not page_token:
            break
    return chats


def chat_members(chat_id: str) -> dict[str, str]:
    """会话成员 → {open_id: 显示名}。

    用于把 `ou_xxx` 变成「张三」。失败返回空字典而不是抛错：名字是锦上添花，
    拿不到就显示 open_id 尾号，不该让整次读消息失败。
    """
    names: dict[str, str] = {}
    page_token = ""
    for _ in range(10):  # 上限 10 页（200 人）足够覆盖团队群
        params = {"member_id_type": "open_id", "page_size": "50"}
        if page_token:
            params["page_token"] = page_token
        try:
            out = _get(f"{_IM_CHAT_BASE}/chats/{chat_id}/members", params)
        except FeishuSendError:
            return names
        if out.get("code") != 0:
            return names
        data = out.get("data") or {}
        for item in data.get("items") or []:
            mid = str(item.get("member_id") or "")
            if mid:
                names[mid] = str(item.get("name") or "")
        page_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not page_token:
            break
    return names


def _message_text(msg_type: str, body: dict, *, max_chars: int = 400) -> str:
    """把一条飞书消息压成一行可读文本（附件只留占位说明）。"""
    content = body.get("content")
    try:
        data = json.loads(content) if isinstance(content, str) else (content or {})
    except ValueError:
        data = {}

    text = ""
    if msg_type == "text":
        text = str(data.get("text") or "")
    elif msg_type == "post":
        title = str(data.get("title") or "").strip()
        lines: list[str] = []
        for para in data.get("content") or []:
            if not isinstance(para, list):
                continue
            lines.append("".join(
                str(node.get("text") or "") for node in para if isinstance(node, dict)))
        text = "\n".join(x for x in ([title] if title else []) + lines if x)
    elif msg_type == "interactive":
        # 卡片：标题 + 所有文本节点（卡片 JSON 版本多，扫全树最稳）。
        # 飞书对旧版卡片的 body 只给 `{"title":..., "elements":[[{"tag":"img"}...]]}`，
        # 正文其实是渲染出来的图 —— 结构里扫不到字时明确说「卡片（内容为图片）」，
        # 而不是留一行只有时间的空消息让模型以为对方没说话。
        parts: list[str] = []

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                tag = node.get("tag")
                if tag in ("text", "plain_text", "lark_md") and node.get("content"):
                    parts.append(str(node["content"]))
                if tag == "text" and node.get("text"):
                    parts.append(str(node["text"]))
                for k, v in node.items():
                    if k != "tag":
                        _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(data)
        text = " ".join(p for p in parts if p.strip())
        # 飞书给旧版卡片的文本占位就是这一句，读进来毫无信息量，且会盖掉真正的标题
        text = re.sub(r"请升级至最新版本客户端[，,]?\s*以查看内容", "", text).strip()
        if not text.strip():
            title = str(data.get("title") or "").strip()
            # elements 里有 img 而正文扫不到字：卡片是「图片渲染」的，标题还有信息量，
            # 正文只能如实标注拿不到——别让模型以为这张卡只有标题。
            has_img = "image_key" in json.dumps(data)
            suffix = "（正文以图片渲染，仅能看到标题）" if has_img else ""
            text = f"[卡片] {title}{suffix}" if title else "[卡片（无文本）]"
    elif msg_type == "file":
        text = f"[文件 {data.get('file_name') or '未命名'}]"
    elif msg_type in ("image",):
        text = "[图片]"
    elif msg_type in ("audio", "media"):
        text = "[语音]" if msg_type == "audio" else "[视频]"
    elif msg_type == "sticker":
        text = "[表情]"
    elif msg_type == "share_chat":
        text = "[群名片]"
    elif msg_type == "merge_forward":
        text = "[合并转发的聊天记录]"
    else:
        text = f"[{msg_type}]"

    # 富文本正文里常带 <p>/<at> 标签，去掉再给模型
    text = re.sub(r"<[^>]+>", "", text).strip()
    return text[:max_chars]


def recent_messages(chat_id: str, limit: int = 20) -> list[dict]:
    """读会话最近的消息（倒序取，返回按时间正序）。

    每项：`{message_id, sender_id, sender_name, create_time, msg_type, text, from_bot}`。

    时间戳用飞书自己给的 `create_time`（毫秒），**不拿本机时钟过滤**：实测机器时钟
    与飞书能差几个小时，按本机 now 卡窗口会把刚发的消息全判成过期。
    """
    limit = max(1, min(int(limit or 20), 50))
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "sort_type": "ByCreateTimeDesc",
        "page_size": str(limit),
    }
    out = _get(f"{_IM_CHAT_BASE}/messages", params)
    if out.get("code") != 0:
        raise FeishuSendError(f"读取会话消息失败：{out.get('msg') or out}")
    items = list((out.get("data") or {}).get("items") or [])
    names = chat_members(chat_id)

    messages: list[dict] = []
    for item in reversed(items):  # 倒序取回，正序呈现
        sender = item.get("sender") or {}
        sender_id = str(sender.get("id") or "")
        from_bot = str(sender.get("sender_type") or "") == "app"
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        messages.append({
            "message_id": str(item.get("message_id") or ""),
            "sender_id": sender_id,
            "sender_name": names.get(sender_id) or ("机器人" if from_bot else sender_id[-6:]),
            "create_time": int(item.get("create_time") or 0),
            "msg_type": str(item.get("msg_type") or ""),
            "text": _message_text(str(item.get("msg_type") or ""), body),
            "from_bot": from_bot,
        })
    return messages


def format_conversation(messages: list[dict], *, self_open_id: str = "") -> str:
    """最近消息 → 一段带时间与说话人的上下文文本（给模型读）。

    `自` / `我` 的区分按 sender_id：机器人自己的消息标「我」，提问者标「你」。
    标不出来时用显示名——宁可多一个字，也不要让模型分不清谁说的。
    """
    if not messages:
        return ""
    lines: list[str] = []
    for m in messages:
        stamp = ""
        if m.get("create_time"):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(m["create_time"] / 1000))
        who = m.get("sender_name") or ""
        if m.get("from_bot"):
            who = "我(机器人)"
        elif self_open_id and m.get("sender_id") == self_open_id:
            who = "你"
        text = (m.get("text") or "").replace("\n", " ")
        lines.append(f"[{stamp}] {who}：{text}" if stamp else f"{who}：{text}")
    return "\n".join(lines)


def reply_text(message_id: str, text: str) -> str:
    """回复指定消息（引用该消息），返回新消息的 message_id。"""
    out = _post(f"{_IM_BASE}/{message_id}/reply", _body(text), token=_tenant_token())
    if out.get("code") != 0:
        raise FeishuSendError(f"回复失败: {out}")
    return str(out["data"]["message_id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_id", nargs="?", default=DEFAULT_CHAT)
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--default", dest="dtext", default="",
                    help="发到授权测试私聊的简写")
    ap.add_argument("--reply", dest="reply_to", default="",
                    help="回复指定 message_id（om_ 开头），忽略 chat_id")
    ap.add_argument("--read", dest="read_chat", default="",
                    help="读该会话最近消息（chat_id），不发送")
    ap.add_argument("--limit", type=int, default=20, help="--read 时取多少条（≤50）")
    args = ap.parse_args()
    if args.read_chat:
        try:
            messages = recent_messages(args.read_chat, args.limit)
        except FeishuSendError as e:
            print(f"❌ {e}")
            return 1
        print(format_conversation(messages) or "（该会话最近没有消息）")
        return 0
    text = args.dtext or args.text
    if not text:
        print("用法: feishu_send.py <chat_id> <文本>  或  --default <文本>  或  --read <chat_id>")
        return 2
    try:
        if args.reply_to:
            mid = reply_text(args.reply_to, text)
            print(f"replied message_id={mid} -> {args.reply_to}")
        else:
            mid = send_text(args.chat_id, text)
            print(f"sent message_id={mid} -> {args.chat_id}")
    except FeishuSendError as e:
        print(f"❌ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
