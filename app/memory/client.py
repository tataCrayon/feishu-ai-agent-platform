"""TencentDB Agent Memory Gateway client.

Wraps the MemoryCore Gateway HTTP API at /capture, /recall, /session/end, /health.
Uses httpx for async HTTP with configurable timeout and retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("memory")


def _split_blocks(context: str) -> list[tuple[str, str]]:
    """按顶层 XML 块切分 gateway 返回的 context（<user-persona>…</user-persona> 等）。

    返回 (块原文, 块标签) 列表；无块结构的纯文本作为单块返回。
    """
    import re

    parts: list[tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"<(\w[\w-]*)>.*?</\1>", context, re.DOTALL):
        if m.start() > pos and context[pos:m.start()].strip():
            parts.append((context[pos:m.start()], "(text)"))
        parts.append((m.group(0), m.group(1)))
        pos = m.end()
    if pos < len(context) and context[pos:].strip():
        parts.append((context[pos:], "(text)"))
    return parts or [(context, "(text)")]


# ── Config ──────────────────────────────────────────────────────


@dataclass
class MemoryConfig:
    """Memory client configuration."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8420"
    api_key: str = ""
    service_id: str = "feishu-bot"
    timeout_sec: int = 10
    max_retries: int = 2
    recall_context_max_chars: int = 2000  # recall 返回上下文截断闸（v1 网关无条数/阈值参数）


# ── Client ──────────────────────────────────────────────────────


class MemoryClient:
    """Async client for MemoryCore Gateway.

    Usage:
        client = MemoryClient(MemoryConfig(base_url="http://127.0.0.1:8420"))
        await client.health()
        await client.capture("sess_1", "user msg", "assistant reply")
        ctx = await client.recall("query text", "sess_1")
        await client.end_session("sess_1")
    """

    def __init__(self, config: MemoryConfig):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_sec,
            headers=self._build_headers(),
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-tdai-service-id": self.config.service_id,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def close(self):
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        """Check Gateway health.

        Returns:
            dict with status, version, uptime, stores info.
            {"status": "ok", ...} on success.
        """
        try:
            resp = await self._client.get("/health")
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Memory health: {data.get('status')} v{data.get('version', '?')}")
            return data
        except httpx.HTTPError as e:
            logger.warning(f"Memory health check failed: {e}")
            return {"status": "unreachable", "error": str(e)}

    async def capture(
        self,
        session_key: str,
        user_content: str,
        assistant_content: str,
        user_id: str | None = None,
        messages: list[dict] | None = None,
    ) -> bool:
        """Capture a conversation round (L0 record).

        Args:
            session_key: Unique session identifier.
            user_content: User's message text.
            assistant_content: Assistant's reply text.
            user_id: Optional user identifier.
            messages: Optional full message list for richer context.

        Returns:
            True if recorded successfully.
        """
        if not self.config.enabled:
            return False

        payload: dict[str, Any] = {
            "user_content": user_content[:4000],  # safe truncation
            "assistant_content": assistant_content[:8000],
            "session_key": session_key,
            "user_id": user_id or "unknown",
        }
        if messages:
            payload["messages"] = messages

        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await self._client.post("/capture", json=payload)
                if resp.status_code == 429:
                    logger.warning(f"Capture rate limited, retry {attempt + 1}")
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                logger.debug(
                    f"Capture OK: l0={data.get('l0_recorded', 0)}, "
                    f"notified={data.get('scheduler_notified', False)}"
                )
                return True
            except httpx.HTTPError as e:
                logger.warning(f"Capture failed (attempt {attempt + 1}): {e}")
                if attempt < self.config.max_retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    return False

        return False

    async def recall(
        self,
        query: str,
        session_key: str,
        user_id: str | None = None,
    ) -> str:
        """Recall relevant memories for a query.

        网关 v1 /recall 合同（核对 MemoryCore server.ts
        handleRecall）：只消费 query + session_key，条数/策略/阈值均由
        Gateway 自律，客户端无可下发参数——因此返回体量在本侧截断兜底
        （recall_context_max_chars），防止不定长记忆注入撑爆路由上下文。

        Args:
            query: The user's question or context.
            session_key: Current session identifier.
            user_id: Optional user identifier.

        Returns:
            Formatted context string (截断到预算内), or empty string if no relevant memories.
        """
        if not self.config.enabled:
            return ""

        payload = {
            "query": query[:2000],
            "session_key": session_key,
            "user_id": user_id or "unknown",
        }

        try:
            resp = await self._client.post("/recall", json=payload)
            resp.raise_for_status()
            data = resp.json()

            context = data.get("context", "")
            strategy = data.get("strategy", "?")
            count = data.get("memory_count", 0)
            code = data.get("code", 0)

            if code != 0:
                logger.info(f"Recall returned code={code}: {data.get('message', '')}")
                return ""

            if count and count > 0:
                logger.info(f"Recall: {count} memories via {strategy}")
            budget = self.config.recall_context_max_chars
            if len(context) > budget:
                # 按块截断而非裸切：context 是 <user-persona>/<scene-navigation>/
                # <memory-tools-guide> 等块的拼接，裸 [:budget] 会把后面的块
                # 切成半截 XML 并整块丢弃（实测：5169 字符注入，
                # 教条类 persona 块独大占满 2000 预算，scene-navigation 和
                # 工具指南被截没——路由 LLM 失去「何时调记忆工具」的指引）。
                # 策略：整块装入，装不下的块整体跳过；最后保证至少注入
                # memory-tools-guide（教 router 主动搜记忆，弥补 keyword 策略
                # 召回不全的问题）。
                kept: list[str] = []
                dropped = 0
                blocks = _split_blocks(context)
                guide = next((b for b in blocks if "memory-tools-guide" in b[1]), None)
                for body, _tag in blocks:
                    if "memory-tools-guide" in _tag:
                        continue  # 指南最后单独装（保底）
                    if sum(len(x) for x in kept) + len(body) <= budget:
                        kept.append(body)
                    else:
                        dropped += 1
                if guide and sum(len(x) for x in kept) + len(guide[0]) <= budget:
                    kept.append(guide[0])
                elif guide:
                    kept.append(guide[0][:budget // 4])
                context = "\n".join(kept)
                logger.warning(
                    f"Recall context {len(context)} chars (raw over budget, "
                    f"{dropped} block(s) dropped) — blocks kept: {len(kept)}"
                )
            return context or ""
        except httpx.HTTPError as e:
            logger.warning(f"Recall failed: {e}")
            return ""

    async def end_session(
        self,
        session_key: str,
        user_id: str | None = None,
    ) -> bool:
        """End a session and flush pending memories.

        Args:
            session_key: Session to end.
            user_id: Optional user identifier.

        Returns:
            True if flushed successfully.
        """
        if not self.config.enabled:
            return False

        payload = {
            "session_key": session_key,
            "user_id": user_id or "unknown",
        }

        try:
            resp = await self._client.post("/session/end", json=payload)
            resp.raise_for_status()
            data = resp.json()
            flushed = data.get("flushed", False)
            logger.info(f"Session end: flushed={flushed}")
            return flushed
        except httpx.HTTPError as e:
            logger.warning(f"Session end failed: {e}")
            return False

    async def search_memories(
        self,
        query: str,
        limit: int = 10,
        type_filter: str | None = None,
        scene: str | None = None,
    ) -> str:
        """Search stored memories (L1/L2/L3).

        Args:
            query: Search query.
            limit: Max results.
            type_filter: Optional memory type filter.
            scene: Optional scene filter.

        Returns:
            Formatted memory results string.
        """
        params: dict[str, Any] = {"query": query, "limit": limit}
        if type_filter:
            params["type"] = type_filter
        if scene:
            params["scene"] = scene

        try:
            resp = await self._client.get("/search/memories", params=params)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("total", 0)
            results = data.get("results", "")
            strategy = data.get("strategy", "?")
            logger.info(f"Memory search: {total} results via {strategy}")
            return results or ""
        except httpx.HTTPError as e:
            logger.warning(f"Memory search failed: {e}")
            return ""

    async def search_conversations(
        self,
        query: str,
        limit: int = 10,
        session_key: str | None = None,
    ) -> str:
        """Search L0 conversation history.

        Args:
            query: Search query.
            limit: Max results.
            session_key: Optional session filter.

        Returns:
            Formatted conversation results string.
        """
        params: dict[str, Any] = {"query": query, "limit": limit}
        if session_key:
            params["session_key"] = session_key

        try:
            resp = await self._client.get("/search/conversations", params=params)
            resp.raise_for_status()
            data = resp.json()
            total = data.get("total", 0)
            results = data.get("results", "")
            logger.info(f"Conversation search: {total} results")
            return results or ""
        except httpx.HTTPError as e:
            logger.warning(f"Conversation search failed: {e}")
            return ""


# ── Singleton ───────────────────────────────────────────────────


_client: MemoryClient | None = None


def get_client(config: MemoryConfig | None = None) -> MemoryClient:
    """Get or create the global MemoryClient singleton."""
    global _client
    if _client is None:
        _client = MemoryClient(config or MemoryConfig())
    return _client


def set_client(client: MemoryClient):
    """Set the global MemoryClient singleton (for testing or config injection)."""
    global _client
    _client = client


def reset_client():
    """Reset the global MemoryClient singleton."""
    global _client
    _client = None