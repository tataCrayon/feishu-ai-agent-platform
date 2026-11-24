"""会话历史缓冲 — 按「会话 + 用户」隔离的近期对话环形缓冲。

为路由 LLM 与子 Agent 提供最近 N 轮对话上下文，实现多轮语义理解
（指代消解、上下文承接）。内存常驻，无外部依赖。

线程/并发安全：
- 每个会话一个 asyncio.Lock，record / snapshot / clear 均加锁，
  避免同一用户连发消息时并发读写导致乱序或丢帧。
- key = "{chat_id}:{sender_id}"，群聊中不同用户互不串上下文。

与 MemoryCore 的关系：
- 本缓冲提供「最近连续多轮」的稳定短期上下文（不受 keyword recall 质量影响）。
- MemoryCore 的 capture/recall 保留做长期记忆，两者互补。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("orchestrator.history")

# 单条消息内容截断上限（避免历史无限膨胀挤爆上下文）
_MAX_CONTENT_LEN = 2000

# 澄清 pending 状态 TTL（秒）：反问后用户长时间不回，状态过期失效，
# 下一轮按全新消息路由（避免隔天「好的」误触旧确认）。
_PENDING_TTL_SEC = 300

# 历史落盘默认目录（相对项目根；config 可覆盖）
DEFAULT_PERSIST_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "history"


class SessionHistory:
    """按会话+用户维护近期对话历史。

    Args:
        max_turns: 每个会话保留的最近轮数（1 轮 = user + assistant）。
        persist_dir: 历史落盘目录；None = 不落盘（纯内存，重启即丢）。
            落盘时机：每次 record 后整会话原子重写（按 key 分文件，量小），
            重启后首次访问该会话时惰性回填，实现对话不丢失。
    """

    def __init__(self, max_turns: int = 6, persist_dir: Path | None = None):
        self._max_turns = max(max_turns, 1)
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 澄清闭环 pending：key → {"intent_summary","candidates","question","ts"}。
        # 内存常驻（TTL 内有效），不落盘——反问确认是短时交互，重启后丢失
        # 可接受（用户重新说需求即可），避免磁盘文件里堆积半截会话状态。
        self._pending: dict[str, dict[str, Any]] = {}
        self._persist_dir = persist_dir
        if persist_dir is not None:
            try:
                persist_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("History persist dir unavailable (%s): %s", persist_dir, e)
                self._persist_dir = None

    @staticmethod
    def key(chat_id: str, sender_id: str) -> str:
        """构造会话+用户的隔离 key。"""
        return f"{chat_id}:{sender_id}"

    def _lock_for(self, key: str) -> asyncio.Lock:
        """获取（或惰性创建）指定 key 的锁。"""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _buffer_for(self, key: str) -> deque[dict[str, Any]]:
        """获取（或惰性创建）指定 key 的历史缓冲；重启后从磁盘回填。"""
        buf = self._buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self._max_turns * 2)
            if self._persist_dir is not None:
                self._restore_from_disk(key, buf)
            self._buffers[key] = buf
        return buf

    # ── 落盘 / 回填（调用方已持有该 key 的锁）──────────────────────────

    def _file_for(self, key: str) -> Path:
        # key 含 ":"，Windows 路径非法，替换为下划线
        safe = key.replace(":", "_").replace("/", "_").replace("\\", "_")
        assert self._persist_dir is not None, "persist disabled"
        return self._persist_dir / f"{safe}.json"

    def _restore_from_disk(self, key: str, buf: deque[dict[str, Any]]) -> None:
        """从磁盘回填指定会话历史（仅启动后首次访问时触发一次）。"""
        try:
            f = self._file_for(key)
            if not f.exists():
                return
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            limit = buf.maxlen or len(msgs)
            for m in msgs[-limit:]:
                buf.append({"role": m.get("role", "user"),
                            "content": str(m.get("content", ""))[:_MAX_CONTENT_LEN]})
            if msgs:
                logger.debug("History restored from disk: key=%s turns=%d", key, len(msgs) // 2)
        except Exception as e:
            logger.warning("History restore failed for %s: %s", key, e)

    def _flush_to_disk(self, key: str, buf: deque[dict[str, Any]]) -> None:
        """把指定会话历史原子重写到磁盘（临时文件 + rename 防写坏）。"""
        if self._persist_dir is None:
            return
        try:
            f = self._file_for(key)
            tmp = f.with_suffix(".json.tmp")
            payload = {"key": key, "messages": list(buf)}
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)
        except Exception as e:
            logger.warning("History flush failed for %s: %s", key, e)

    async def record(
        self,
        chat_id: str,
        sender_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """追加一轮对话（用户消息 + 助手回复）到指定会话。

        加锁保证同一会话的并发追加不丢帧。
        """
        key = self.key(chat_id, sender_id)
        async with self._lock_for(key):
            buf = self._buffer_for(key)
            buf.append({
                "role": "user",
                "content": (user_content or "")[:_MAX_CONTENT_LEN],
            })
            buf.append({
                "role": "assistant",
                "content": (assistant_content or "")[:_MAX_CONTENT_LEN],
            })
            self._flush_to_disk(key, buf)
        logger.debug(
            "SessionHistory.record: key=%s turns=%d",
            key, len(buf) // 2,
        )

    async def snapshot(self, chat_id: str, sender_id: str) -> list[dict[str, Any]]:
        """返回指定会话的历史快照（最旧在前），用于注入 LLM messages。

        返回 list 而非 deque 引用，避免外部修改内部缓冲。
        """
        key = self.key(chat_id, sender_id)
        async with self._lock_for(key):
            buf = self._buffer_for(key)  # 重启后首次访问时惰性回填
            if not buf:
                return []
            return list(buf)

    async def clear(self, chat_id: str, sender_id: str) -> None:
        """清空指定会话的历史（含磁盘档案与澄清 pending）。"""
        key = self.key(chat_id, sender_id)
        async with self._lock_for(key):
            buf = self._buffers.get(key)
            if buf:
                buf.clear()
            self._pending.pop(key, None)
            if self._persist_dir is not None:
                try:
                    self._file_for(key).unlink(missing_ok=True)
                except Exception as e:
                    logger.warning("History persist clear failed for %s: %s", key, e)

    # ── 澄清闭环 pending（SPEC-20261002）────────────────────────────

    async def record_pending(
        self,
        chat_id: str,
        sender_id: str,
        *,
        intent_summary: str,
        candidates: list[str] | None = None,
        question: str = "",
    ) -> None:
        """记录一次「待用户确认」的澄清反问状态。

        clarify 节点反问用户时调用；下一轮消息路由前由 pop_pending 取出，
        注入路由上下文实现确认闭环（用户回「对，就是那个」「查库那个」时
        路由 LLM 能定向到上轮候选）。
        """
        key = self.key(chat_id, sender_id)
        async with self._lock_for(key):
            self._pending[key] = {
                "intent_summary": (intent_summary or "")[:120],
                "candidates": [str(c) for c in (candidates or []) if c][:3],
                "question": (question or "")[:200],
                "ts": time.time(),
            }
        logger.debug(
            "[pending] recorded: key=%s intent=%r candidates=%s",
            key, (intent_summary or "")[:40], candidates,
        )

    async def pop_pending(self, chat_id: str, sender_id: str) -> dict[str, Any] | None:
        """取出（并清除）指定会话的澄清 pending 状态。

        过期（TTL 5 分钟）视为不存在并顺手清除。取即删：pending 只对下一轮
        消息有效——用户连续两轮才答完确认时，第二轮的 pending 由 clarify
        节点视需要重新记录。
        """
        key = self.key(chat_id, sender_id)
        async with self._lock_for(key):
            item = self._pending.pop(key, None)
        if not item:
            return None
        if time.time() - float(item.get("ts", 0)) > _PENDING_TTL_SEC:
            logger.debug("[pending] expired: key=%s", key)
            return None
        return item

    def stats(self) -> dict[str, Any]:
        """返回历史缓冲统计（用于可观测性/打点）。"""
        return {
            "active_sessions": len(self._buffers),
            "max_turns": self._max_turns,
            "persist": self._persist_dir is not None,
            "pending_clarifies": len(self._pending),
        }
