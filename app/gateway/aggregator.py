"""消息聚合器 — 按「会话+用户」debounce 合并短时连发消息。

解决的核心痛点：
- 用户习惯把一个需求拆成多条短消息连发（"帮我查一下" + "t_rwa_product 表结构"）
- 旧逻辑每条消息独立进编排器：N 条 = N 次路由 + N 份回执 + N 份回复，上下文割裂

设计：
- per (chat_id, sender_id) 缓冲；首条到达后进入静默窗口 debounce_sec 秒
- 窗口内有新消息则继续等（打字中不抢跑），总等待不超过 max_wait_sec
- 窗口结束后按序合并为一条文本，交给 process 回调统一处理
- process 执行期间新到的消息自动进入下一批（排队，不打断进行中的处理）

约定：process 回调需自行兜住异常（不得向外抛），本层仅做防御性捕获。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("bot.aggregator")

# process 回调签名：(merged_text, last_msg, merged_count) -> None
ProcessFn = Callable[[str, Any, int], Awaitable[None]]

# 静默窗口轮询间隔（秒）：检查"是否还在来新消息"的节奏
_POLL_INTERVAL = 0.5


def extract_text(msg: Any) -> str:
    """从 InboundMessage 提取纯文本，并带上被引用消息的正文。

    飞书「回复」只在 msg.reply（或 raw.parent_id）里给父消息 id。不把父消息
    正文拼进来，路由只能看到「你确认一下」，会当成空 @。

    优先取调用方预取好的 `_prefetched_text`（main.on_message 异步拉完引用
    正文后 setattr 挂回）——aggregator worker 是同步循环，没法替它拉；
    不认这条，引用正文在 on_message 里算完也会被丢掉
    （事故：工单正文丢失，LLM 拿历史里的
    DATA2026092903 出了错单）。
    """
    prefetched = getattr(msg, "_prefetched_text", None)
    if isinstance(prefetched, str) and prefetched.strip():
        return prefetched.strip()
    own = (getattr(msg, "body_text", None) or getattr(msg, "content_text", None) or "").strip()
    quoted = _quoted_text(msg)
    if quoted and quoted not in own:
        if own:
            return f"{own}\n（引用的上一条：{quoted}）"
        return f"（引用的上一条：{quoted}）"
    return own


def _quoted_text(msg: Any) -> str:
    """ReplyRef.text 有值就用。没有则留给调用方异步去拉。"""
    reply = getattr(msg, "reply", None)
    text = getattr(reply, "text", None) if reply else None
    return str(text or "").strip()


def session_key_of(msg: Any) -> tuple[str, str]:
    """构造 (chat_id, sender_id) 隔离 key。"""
    conv = getattr(msg, "conversation", None)
    sender = getattr(msg, "sender", None)
    return (
        getattr(conv, "chat_id", "") or "",
        getattr(sender, "open_id", "") or "",
    )


class MessageAggregator:
    """Debounce 消息聚合器 — 同一会话同一用户的连发消息合并为一批处理。

    Args:
        debounce_sec: 静默窗口。首条消息后等待 N 秒无新消息才触发处理。
        max_wait_sec: 单批最大等待上限（用户持续打字时防无限延长）。
    """

    def __init__(self, debounce_sec: float = 3.0, max_wait_sec: float = 15.0):
        self._debounce = max(float(debounce_sec), _POLL_INTERVAL)
        self._max_wait = max(float(max_wait_sec), self._debounce)
        # key -> 待合并消息列表
        self._buffers: dict[tuple[str, str], list[Any]] = {}
        # key -> 当前批次起始时间（max_wait 从这里起算）
        self._batch_start: dict[tuple[str, str], float] = {}
        # key -> 最近一条消息入缓冲时间（判断静默）
        self._last_append: dict[tuple[str, str], float] = {}
        # key -> 活跃 worker 任务
        self._workers: dict[tuple[str, str], asyncio.Task] = {}

    # ── 公开接口 ──

    def submit(self, msg: Any, process: ProcessFn) -> None:
        """提交一条消息。同会话同用户的短时连发会被合并为一批统一处理。

        无活跃 worker 时启动新批次；已有 worker 在跑（前一批处理中）则只追加，
        由 worker 循环在处理完成后自然消化积压消息。
        """
        key = session_key_of(msg)
        self._buffers.setdefault(key, []).append(msg)

        now = time.time()
        self._last_append[key] = now
        worker = self._workers.get(key)
        if worker is None or worker.done():
            self._workers[key] = asyncio.create_task(self._worker(key, process))

    def stats(self) -> dict[str, Any]:
        """聚合器运行统计（可观测性）。"""
        return {
            "pending_sessions": len(self._buffers),
            "active_workers": sum(
                1 for t in self._workers.values() if not t.done()
            ),
            "debounce_sec": self._debounce,
            "max_wait_sec": self._max_wait,
        }

    # ── 内部实现 ──

    async def _worker(self, key: tuple[str, str], process: ProcessFn) -> None:
        """单会话消费循环：等静默窗口 → 取批合并 → 处理 → 循环消化积压。

        退出条件：一轮 flush 时缓冲为空（无新增也无积压）。
        """
        try:
            while True:
                self._batch_start[key] = time.time()

                # ── 静默窗口：等到"安静了 debounce 秒"或到达 max_wait ──
                while True:
                    await asyncio.sleep(_POLL_INTERVAL)
                    now = time.time()
                    quiet_for = now - self._last_append.get(
                        key, self._batch_start[key]
                    )
                    if quiet_for >= self._debounce:
                        break
                    if now - self._batch_start[key] >= self._max_wait:
                        logger.info(
                            "Aggregate max-wait hit for %s:%s — flush early",
                            key[0], key[1],
                        )
                        break

                msgs = self._buffers.pop(key, [])
                self._last_append.pop(key, None)
                if not msgs:
                    break  # 无待处理消息 → 退出，等下一条消息重启

                texts = [
                    t for t in (extract_text(m).strip() for m in msgs) if t
                ]
                if not texts:
                    continue

                # 整批附件合并到 last_msg：下游（webportal 聚合分派的附件下载）
                # 只认最后一条消息的 resources，而用户习惯「先发文件、后发指令」
                # ——末条常是无附件的文本（3 个 .msg + 表单文本同批到达时附件会全丢，
                # 引擎收到空描述）。按 file_key 去重合并；
                # 每条资源打上来源 message_id（飞书 message-resource 端点校验
                # file_key 归属，跨消息下载必须用原始消息 id）。
                all_res: list[Any] = []
                seen_keys: set[Any] = set()
                for m in msgs:
                    for r in (getattr(m, "resources", None) or []):
                        k = getattr(r, "file_key", None) or id(r)
                        if k not in seen_keys:
                            seen_keys.add(k)
                            try:
                                r._source_message_id = getattr(m, "id", None)
                            except Exception:
                                pass
                            all_res.append(r)
                if all_res:
                    msgs[-1].resources = all_res

                merged = "\n".join(texts)
                logger.info(
                    "Aggregated %d message(s) for %s:%s (%d chars)",
                    len(texts), key[0], key[1], len(merged),
                )
                # process 自行兜异常；期间新到的消息留在缓冲，由下一轮消化
                await process(merged, msgs[-1], len(texts))
        except Exception:
            logger.exception("Aggregator worker failed for %s:%s", key[0], key[1])
        finally:
            # 仅清理 worker 注册；缓冲不动 —— 若异常时仍有积压，
            # 下一条消息 submit 会看到 done worker 并重启新批次消化它们
            self._workers.pop(key, None)
            self._batch_start.pop(key, None)
