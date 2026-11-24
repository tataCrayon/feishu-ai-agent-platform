"""Feishu Channel SDK integration — WebSocket event-driven message gateway.

Replaces the polling-based gateway.py with push-based WebSocket events.

## 回调为什么必须投回本仓的循环

SDK 自己起一个后台线程 + 一条独立事件循环（`lark_channel` 的 `_ensure_bg_loop`），
WS 事件在该循环上拆包并 `await` 我们注册的 handler。如果就地执行，同一条消息的
两个阶段会分属两条循环：`_process_batch` 在 SDK 循环、dsh 的进度帧（adapter 读协程）
在主循环——两边共用的 `asyncio.Lock` 一旦出现竞争就抛
`is bound to a different event loop`（实测 3 次，均为 `[locked, waiters:2]`），
异常从完成卡那次刷新冒出去，把**已经拿到的答案整条丢掉**，用户只看到一句
「处理出错」。

所以注册时记下本仓的循环，回调统一 `run_coroutine_threadsafe` 投回去；SDK 线程
只做传输。这样 `RuntimeGuard` 的信号量、`DshGateway` 的映射锁也回到同一条循环上
（它们同样是主循环创建、原本在 SDK 循环上等待）。
"""

import asyncio
import inspect
import logging
import re
from pathlib import Path

from lark_channel import (
    FeishuChannel,
    FileTokenStore,
    GroupOverride,
    InboundConfig,
    InboundMessage,
    LogLevel,
    OutboundConfig,
    PolicyConfig,
    SendResult,
)

from app.protocols.feishu_card import ensure_button_behaviors

logger = logging.getLogger("channel")


def _as_dict(value) -> dict:
    """递归把 SDK 对象转成 dict。

    飞书 SDK 的 list 接口返回的是对象，嵌套的 body/sender 也是对象。
    只转一层会导致 body.content 取不到，附件文件名整个丢失。
    """
    if isinstance(value, dict):
        return {k: _as_dict(v) if isinstance(v, (dict, list)) or hasattr(v, "__dict__") else v
                for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return value  # 列表字段此处不消费
    if hasattr(value, "__dict__"):
        return _as_dict(value.__dict__)
    return {}


def _sender_open_id(raw: dict) -> str:
    """飞书 list 接口的 sender 在顶层，不在 body 里。id 类型不固定是 open_id。"""
    sender = _as_dict(raw.get("sender") or {})
    nested = _as_dict(sender.get("sender_id"))
    id_type = str(sender.get("id_type") or sender.get("sender_id_type") or "")
    if id_type == "open_id":
        return str(sender.get("id") or nested.get("open_id") or "")
    return str(nested.get("open_id") or "")


def _message_content(raw: dict) -> dict:
    import json

    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    content = body.get("content") if body else raw.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return {}
    return content if isinstance(content, dict) else {}


def _file_parts(message_id: str, msg_type: str, content: dict) -> list[tuple[str, str, str]]:
    """一条消息里能下载的文件。post 里的图片、文件消息、图片消息都算。"""
    found: list[tuple[str, str, str]] = []
    if msg_type == "file" and content.get("file_key"):
        found.append((message_id, str(content["file_key"]), str(content.get("file_name") or "attachment")))
    elif msg_type == "image" and content.get("image_key"):
        found.append((message_id, str(content["image_key"]), "image.png"))
    elif msg_type in ("post", "interactive"):
        for row in content.get("content") or []:
            if not isinstance(row, list):
                continue
            for node in row:
                if not isinstance(node, dict):
                    continue
                if node.get("tag") == "img" and node.get("image_key"):
                    found.append((message_id, str(node["image_key"]), "image.png"))
                elif node.get("tag") == "media" and node.get("file_key"):
                    found.append((
                        message_id,
                        str(node["file_key"]),
                        str(node.get("file_name") or "attachment"),
                    ))
    return found


def _ensure_button_behaviors(card: dict) -> dict:
    """兼容旧名的薄壳 —— 实现已收进协议层（`app/protocols/feishu_card.py`）。

    保留这个名字是因为回调侧与测试都按它做断言；主进程与 MCP 子进程必须
    走**同一个**实现，否则 2.0 卡片的按钮在一条链路上能点、另一条点不动。
    """
    return ensure_button_behaviors(card)


class BotChannel:
    """Feishu bot channel — WebSocket-based message gateway."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        token_store_dir: Path | None = None,
        log_level: LogLevel = LogLevel.INFO,
        no_mention_chats: list[str] | None = None,
    ):
        """no_mention_chats: 这些群放开「不@我也推消息」（旁路监听用）。

        默认全局 require_mention=True——群里不@机器人的消息根本不进本进程。
        只对显式列出的 chat_id 做覆盖（SDK 的 policy.group_overrides 语义），
        其余群的准入行为一字不变：监听员工群聊是隐私敏感动作，必须逐群授权。
        """
        token_store = None
        if token_store_dir:
            token_store = FileTokenStore(str(token_store_dir))

        overrides = {
            chat_id: GroupOverride(require_mention=False)
            for chat_id in (no_mention_chats or [])
            if chat_id
        }
        self.no_mention_chats = set(overrides)

        self._channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            log_level=log_level,
            token_store=token_store,
            # Policy: handle both DM and group messages
            policy=PolicyConfig(
                dm_policy="open",
                group_policy="open",
                require_mention=True,
                respond_to_mention_all=False,
                group_overrides=overrides or None,
            ),
            inbound=InboundConfig(
                drop_self_sent=True,
            ),
            outbound=OutboundConfig(),
        )
        self._handlers: dict[str, list] = {}
        self._ready = asyncio.Event()
        # 本仓的循环：注册 handler 时锁定（见模块 docstring），SDK 后台线程的回调
        # 一律投回这里执行。None = 还没注册过 handler，此时不会有回调。
        self._home_loop: asyncio.AbstractEventLoop | None = None

    def _bind_home_loop(self) -> None:
        """锁定本仓循环（首次注册时调用；之后重复注册不覆盖）。"""
        if self._home_loop is not None:
            return
        try:
            self._home_loop = asyncio.get_running_loop()
        except RuntimeError:
            # 在事件循环外注册（离线脚本/单测）：不投递，就地执行即可，
            # 那种场景没有第二条循环可撞。
            logger.debug("BotChannel 在事件循环外注册 handler，回调不跨循环投递")

    def _deliver(self, handler):
        """把回调包成 SDK 期望的形状：同步调用、结果可 await。

        SDK 的 `_invoke` 是「同步调用 handler，结果若可 await 就 await」，所以跨循环
        投递必须返回 **asyncio 的** Future（`concurrent.futures.Future` 不会被它 await，
        等于 fire-and-forget，异常也就丢了）。

        同一条循环上（本仓循环 == SDK 循环，或注册发生在事件循环外）直接调，
        不多绕一次调度；同步 handler（`on_error` 那种）也走同一条路。
        """
        home = self._home_loop

        def _wrapped(*args):
            def _invoke():
                return handler(*args)

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if home is None or home.is_closed() or running is home:
                return _invoke()

            async def _call():
                result = _invoke()
                return await result if inspect.isawaitable(result) else result

            fut = asyncio.run_coroutine_threadsafe(_call(), home)
            if running is None:
                # 没有运行中的循环的同步调用方：没法包成 asyncio Future，
                # 交给它一个 concurrent Future（不可 await，等于 fire-and-forget）。
                return fut
            return asyncio.wrap_future(fut, loop=running)

        return _wrapped

    def on_message(self, handler):
        """Register a message handler. Called with InboundMessage."""
        self._bind_home_loop()
        self._channel.on("message", self._deliver(handler))
        return handler

    def on_card_action(self, handler):
        """Register a card action (button click) handler. Called with CardActionEvent."""
        self._bind_home_loop()
        # SDK 事件名是 cardAction（Events.CARD_ACTION），非下划线形式
        self._channel.on("cardAction", self._deliver(handler))
        return handler

    def on_comment(self, handler):
        """Register a document comment handler. Called with CommentEvent."""
        self._bind_home_loop()
        self._channel.on("comment", self._deliver(handler))
        return handler

    def on_error(self, handler):
        """Register an error handler."""
        self._bind_home_loop()
        self._channel.on("error", self._deliver(handler))

    async def reply(self, msg: InboundMessage, text: str) -> SendResult:
        """Reply to a message with text."""
        return await self._channel.reply(
            msg,
            {"markdown": text},
        )

    async def reply_card(self, chat_id: str, message_id: str, card: dict) -> SendResult:
        """把卡片挂成对某条消息的**回复**（反馈卡贴在答复下面）。

        为什么不另发一条：会话里别人的消息一插进来，会话末尾那张「这条回答有
        帮助吗」就分不清指哪一轮。挂在回复里，卡片永远贴着它评的那条答案。

        要回的是机器人自己发出去的消息，手里没有它的 InboundMessage 对象，
        所以走 `send(chat_id, {...}, {"reply_to": message_id})`。
        """
        return await self._channel.send(
            chat_id,
            {"card": _ensure_button_behaviors(card)},
            {"reply_to": message_id},
        )

    async def reply_markdown(self, msg: InboundMessage, markdown: str) -> SendResult:
        """Reply with markdown content."""
        return await self._channel.reply(
            msg,
            {"markdown": markdown},
        )

    async def send(self, chat_id: str, text: str) -> SendResult:
        """Send a message to a chat."""
        return await self._channel.send(
            chat_id,
            {"markdown": text},
        )

    async def send_image(self, chat_id: str, image_path: str, caption: str = "") -> SendResult:
        """发送本地图片到会话（上传拿 image_key 后发 image 消息）。"""
        from lark_channel.channel.types import MediaSource

        image_key = await self._channel.upload_media(
            MediaSource(kind="file", path=image_path),
            kind="image",
        )
        payload = {"image_key": image_key}
        if caption:
            payload["caption"] = caption
        return await self._channel.send(chat_id, {"image": payload})

    async def send_card(self, chat_id: str, card: dict) -> SendResult:
        """发送交互卡片（dict 形态的 card JSON），返回 SendResult（含 message_id）。"""
        return await self._channel.send(
            chat_id,
            {"card": _ensure_button_behaviors(card)},
        )

    async def update_card(self, message_id: str, card: dict) -> SendResult:
        """更新已发送的交互卡片（审批结果回执用）。"""
        return await self._channel.update_card(message_id, _ensure_button_behaviors(card))

    async def react(self, msg: InboundMessage, emoji_type: str) -> bool:
        """给消息添加表情回应（豆包式「收到开始处理」轻回执）。

        emoji_type 用飞书表情枚举名（如 "Typing" 打字/ "THUMBSUP" 点赞），
        失败返回 False 不阻断（回执是增强项）。
        """
        try:
            await self._channel.add_reaction(getattr(msg, "id", "") or "", emoji_type)
            return True
        except Exception:
            logger.debug("add_reaction failed (emoji=%s)", emoji_type, exc_info=True)
            return False

    async def recall(self, message_id: str) -> bool:
        """撤回机器人自己发的消息（进度卡在正式答复发出后退场用）。

        失败返回 False 不阻断（撤回是增强项；权限未开时卡片原地保留）。
        """
        if not message_id:
            return False
        try:
            await self._channel.recall_message(message_id)
            return True
        except Exception:
            logger.debug("recall_message failed (msg=%s)", message_id, exc_info=True)
            return False

    async def download_resource_bytes(
        self, file_key: str, resource_type: str = "file", message_id: str = "",
    ) -> tuple[bytes | None, str | None]:
        """下载媒体资源到内存，返回 (bytes, content_type)。

        多模态识别专用（图片/视频 base64 注入、文档解析）；失败返回
        (None, None) 不抛异常。message_id 传原始消息 id（飞书
        message-resource 端点校验 file_key 归属）。
        """
        try:
            return await self._channel.download_resource(
                file_key, resource_type=resource_type, message_id=message_id,
            )
        except Exception:
            logger.debug("download_resource_bytes failed (key=%s)", file_key,
                         exc_info=True)
            return None, None

    async def download_single_resource(self, msg: InboundMessage, resource, dest_dir):
        """下载单个附件资源到 dest_dir，返回落盘 Path（失败返回 None）。

        能力包接入（app/ops/capability/ingest.py）专用：与 download_resources
        不同，不做扩展名白名单限制（能力包格式由扫描器判定）。
        """
        file_key = getattr(resource, "file_key", None)
        if not file_key:
            return None
        fname = getattr(resource, "file_name", None) or "capability-pkg"
        return await self._channel.download_resource_to_file(
            file_key,
            resource_type=getattr(resource, "type", "file") or "file",
            message_id=getattr(msg, "id", None),
            dest_dir=dest_dir,
            file_name=fname,
        )

    async def stream_reply(self, msg: InboundMessage, producer):
        """Stream a reply to a message. `producer` is an async generator yielding markdown chunks."""
        markdown_producer = producer
        return await self._channel.stream(
            msg.conversation.chat_id,
            {"markdown": markdown_producer},
            {"reply_to": msg.id},
        )

    async def download_resources(self, msg: InboundMessage, dest_dir: str) -> list[dict]:
        """下载消息中的附件资源，返回附件 dict 列表（文件名/路径/类型/大小）。

        对每条 resources 用 FeishuChannel.download_resource_to_file 下载，
        收集 {filename, file_path, doc_type, size}。失败附件跳过（不阻断）。

        安全校验：
        - 按会话/消息隔离到子目录（避免多轮附件混存）
        - 扩展名白名单（仅文本/表格/图片等可解析类型）
        - 文件大小上限
        """
        resources = getattr(msg, "resources", None) or []
        if not resources:
            return []
        from pathlib import Path

        # 扩展名白名单 + 大小上限（解析器能吃下的类型）
        # .msg/.eml：Outlook 邮件文件（常以附件转发的往来邮件，
        # 由 multimodal 通道层解析）
        # 与 `app.multimodal._DOC_EXTS` 保持同一份口径：这里拦掉的后缀，
        # 解析器再强也读不到（用户看到的是「附件被拒」，不是「不支持解析」）。
        _ALLOWED_EXTS = {".md", ".txt", ".csv", ".tsv", ".xlsx", ".xls", ".doc", ".docx",
                         ".pptx", ".ppt", ".pdf", ".odt", ".ods", ".odp", ".rtf",
                         ".png", ".jpg", ".jpeg", ".gif", ".json", ".jsonl", ".sql", ".log",
                         ".msg", ".eml", ".bmp", ".webp", ".yaml", ".yml", ".xml",
                         ".html", ".htm", ".py", ".sh", ".ini", ".conf", ".properties",
                         ".java", ".js", ".ts", ".go", ".rs", ".c", ".h", ".cpp", ".toml",
                         ".ipynb"}
        _MAX_BYTES = 20 * 1024 * 1024  # 20MB

        out: list[dict] = []
        dest = Path(dest_dir)
        # 按会话隔离：logs/attachments/{chat_id}/{message_id}/
        session_sub = f"{getattr(msg, 'chat_id', '') or 'unknown'}_{getattr(msg, 'id', '') or 'unknown'}"
        dest = dest / re.sub(r"[^\w\-]", "_", session_sub)
        dest.mkdir(parents=True, exist_ok=True)

        for res in resources:
            file_key = getattr(res, "file_key", None)
            if not file_key:
                continue
            fname = getattr(res, "file_name", None) or "attachment"
            # 扩展名白名单校验
            ext = Path(fname).suffix.lower()
            if ext not in _ALLOWED_EXTS:
                logger.warning("Rejected attachment (extension not allowed): %s", fname)
                continue
            try:
                path = await self._channel.download_resource_to_file(
                    file_key,
                    resource_type=getattr(res, "type", "file") or "file",
                    # 聚合批次合并的附件带来源消息 id（跨消息下载须用原始
                    # message_id，飞书校验 file_key 归属）；缺省回退本条消息
                    message_id=getattr(res, "_source_message_id", None)
                    or getattr(msg, "id", None),
                    dest_dir=dest,
                    file_name=fname,
                )
                if not path:
                    continue
                # 大小上限校验
                if path.exists() and path.stat().st_size > _MAX_BYTES:
                    logger.warning("Rejected attachment (too large): %s (%d bytes)",
                                   fname, path.stat().st_size)
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                out.append({
                    "filename": fname,
                    "file_path": str(path),
                    "doc_type": getattr(res, "type", "file") or "file",
                    "size": path.stat().st_size if path.exists() else 0,
                })
            except Exception as e:
                logger.warning("Download resource %s failed: %s", file_key, e)
        return out

    async def download_recent_sender_files(
        self,
        msg: InboundMessage,
        dest_dir: str,
        *,
        within_sec: int = 1800,
        page_size: int = 20,
    ) -> list[dict]:
        """回看本群最近消息，下载同一发送者、within_sec 内的文件和图片。

        飞书「回复」不携带更早几条消息上的附件。问题描述和文件常常是
        分开的两条，只读当前消息会得到空附件列表。
        """
        import time
        from pathlib import Path

        from lark_channel.api.im.v1.model.list_message_request import ListMessageRequest

        chat_id = getattr(msg, "chat_id", "") or ""
        sender_id = getattr(msg, "sender_id", "") or ""
        if not chat_id or not sender_id:
            return []
        inner = self._channel
        client = getattr(inner, "_client", None)
        if client is None:
            return []
        try:
            # 不把 start_time 收成 2 分钟。飞书这个过滤和 create_time 不是同一时钟，
            # 收太窄会返回空页，文件明明在群里也看不到。先倒序取一页，再在本地
            # 按 3 分钟和 open_id 收紧。
            req = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .sort_type("ByCreateTimeDesc")
                .page_size(page_size)
                .build()
            )
            resp = await client.im.v1.message.alist(req)
            data = getattr(resp, "data", None)
            items = list(getattr(data, "items", None) or [])
            code = getattr(resp, "code", 0)
            if code not in (0, None):
                logger.warning("recent message list rejected: code=%s", code)
                return []
        except Exception:
            logger.warning("recent message list failed", exc_info=True)
            return []
        logger.info("recent messages in window: %d (chat=%s)", len(items), chat_id[:12])

        found: list[tuple[str, str, str]] = []
        other_open_id = 0
        type_counts: dict[str, int] = {}
        # 机器时钟可能和飞书差几小时，拿本机 now 比对 create_time 会把刚发的
        # 文件全判成「太旧」（17:32 那次 15 个全丢就是这个原因）。以最新一条
        # 用户消息为锚点，比它早 within_sec 的算过期——锚点来自飞书自己，无漂移。
        newest = 0
        for item in items:
            raw = _as_dict(item)
            if str(_as_dict(raw.get("sender")).get("sender_type") or "") == "user":
                newest = max(newest, int(raw.get("create_time") or 0))
        cutoff = newest - max(within_sec, 60) * 1000
        for item in items:
            raw = _as_dict(item)
            body = raw.get("body") if isinstance(raw.get("body"), dict) else raw
            message_id = str(raw.get("message_id") or body.get("message_id") or "")
            msg_type = str(raw.get("msg_type") or body.get("msg_type") or "")
            type_counts[msg_type or "?"] = type_counts.get(msg_type or "?", 0) + 1
            if not message_id:
                continue
            created = int(raw.get("create_time") or 0)
            if newest and created and created < cutoff:
                continue
            sid = _sender_open_id(raw)
            parts = _file_parts(message_id, msg_type, _message_content(raw))
            if not parts:
                continue
            if not sid or sid != sender_id:
                other_open_id += 1
                continue
            found.extend(parts)
        logger.info(
            "recent files kept: %d (not this sender: %d, types: %s, anchor=%s)",
            len(found), other_open_id, type_counts,
            time.strftime("%H:%M:%S", time.localtime(newest / 1000)) if newest else "none",
        )

        if not found:
            return []
        dest = Path(dest_dir) / re.sub(r"[^\w\-]", "_", f"{chat_id}_recent")
        dest.mkdir(parents=True, exist_ok=True)
        out: list[dict] = []
        seen: set[str] = set()
        for message_id, file_key, fname in found:
            if file_key in seen:
                continue
            seen.add(file_key)
            ext = Path(fname).suffix.lower()
            if ext and ext not in {
                ".md", ".txt", ".csv", ".tsv", ".xlsx", ".xls", ".doc", ".docx",
                ".pptx", ".ppt", ".pdf", ".odt", ".ods", ".odp", ".rtf",
                ".png", ".jpg", ".jpeg", ".gif", ".json", ".jsonl", ".sql", ".log",
                ".msg", ".eml", ".bmp", ".webp", ".yaml", ".yml", ".xml",
                ".html", ".htm", ".py", ".sh", ".ini", ".conf", ".properties",
                ".java", ".js", ".ts", ".go", ".rs", ".c", ".h", ".cpp", ".toml",
                ".ipynb",
            }:
                continue
            resource_type = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"} else "file"
            try:
                path = await inner.download_resource_to_file(
                    file_key,
                    resource_type=resource_type,
                    message_id=message_id,
                    dest_dir=dest,
                    file_name=fname,
                )
            except Exception:
                logger.warning("recent file download failed: %s", fname, exc_info=True)
                continue
            if not path:
                continue
            out.append({
                "filename": fname,
                "file_path": str(path),
                "doc_type": ext.lstrip(".") or "file",
                "size": path.stat().st_size if path.exists() else 0,
            })
        return out

    async def get_chat_members(self, chat_id: str):
        """拉取会话成员名单（透传 FeishuChannel，SDK 自带缓存）。

        resolve_sender_name / 审批人显示名解析依赖此方法；p2p 会话同样
        可调（im 成员 API 返回会话用户，实测 200）。此前未透传导致
        resolve_sender_name 里 AttributeError 被静默吞掉，名字永远为空。
        """
        return await self._channel.get_chat_members(chat_id)

    async def connect(self):
        """Connect to Feishu via WebSocket."""
        try:
            await self._channel.connect()
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            raise

    async def start(self):
        """Start the channel (blocking)."""
        self._channel.start()

    async def stop(self):
        """Stop the channel."""
        await self._channel.stop()

    async def run_forever(self):
        """Connect and run until interrupted."""
        try:
            await self._channel.connect()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await self._channel.stop()
        except Exception as e:
            logger.error(f"Channel error: {e}")
            raise