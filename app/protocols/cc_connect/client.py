"""cc-connect webhook client — send prompts to cc-connect agents.

Uses the cc-connect webhook HTTP API (port 9111) to route prompts
to specific agents. cc-connect does NOT have its own Feishu connection
when feishu-agent-platform is the sole Feishu gateway, so replies are NOT pushed back
via platform — instead we poll the cc-connect session history file for the
agent's new reply and return it (see send_and_wait).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("cc_connect")

# Session history 轮询超时（秒）与间隔
_POLL_TIMEOUT = 90
_POLL_INTERVAL = 1.5
# 同步轮询超时后，后台守望迟到回复的总上限（秒）——Claude Code 重任务
# （新闻SQL 实测 17 分钟）远超 90s，结果不应静默丢弃
_LATE_GRACE_SEC = 1200

# send_and_wait 超时转后台守望的返回哨兵（调用方据此回「处理中」回执，
# 迟到结果经 on_reply 回调投递）
LATE_DELIVERY = "<<late-delivery>>"


# ── Config ──────────────────────────────────────────────────────


@dataclass
class CCConnectConfig:
    """cc-connect client configuration."""

    enabled: bool = True
    webhook_url: str = "http://127.0.0.1:9111/hook"
    webhook_token: str = ""
    data_dir: str = ""
    default_project: str = "dev-bot"
    timeout_sec: int = 120
    # Claude Code 会话并发上限：cc-connect 侧同时执行的 agent 会话数。
    # 每个 send_and_wait 占一个 Claude Code 会话直到回复/超时；超限请求排队等待。
    # 健康探测（fire_and_forget）不占额度——投递即返回，不等待回复。
    max_sessions: int = 2


# ── Client ──────────────────────────────────────────────────────


class CCConnectClient:
    """Async client for cc-connect.

    Modes:
    - fire_and_forget(): Send prompt via webhook, no response returned.
    - send_and_wait(): Send prompt via webhook, then poll the session history
      file until the agent's reply appears — returns the reply text.
      This avoids the `relay send` binding requirement (which fails when
      cc-connect has no active Feishu connection to auto-bind the chat).
    """

    def __init__(self, config: CCConnectConfig):
        self.config = config
        self._headers = {"Content-Type": "application/json"}
        if config.webhook_token:
            self._headers["Authorization"] = f"Bearer {config.webhook_token}"
        # Claude Code 会话并发闸（≤ config.max_sessions）：send_and_wait 全程
        # 持有一个会话额度（投递→轮询到回复/超时），超限请求在此排队。
        self._session_slots = asyncio.Semaphore(max(1, config.max_sessions))

    async def fire_and_forget(
        self,
        prompt: str,
        project: str = "",
        session_key: str = "",
    ) -> bool:
        """Send a prompt via webhook (fire-and-forget).

        Args:
            prompt: The prompt text to send.
            project: Target project name. Defaults to config.default_project.
            session_key: Session key for context continuity.

        Returns:
            True if the webhook accepted the request.
        """
        project = project or self.config.default_project

        payload: dict[str, Any] = {
            "project": project,
            "prompt": prompt,
        }
        if session_key:
            payload["session_key"] = session_key

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.config.webhook_url,
                    headers=self._headers,
                    json=payload,
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    logger.info(f"Webhook sent to {project}")
                    return True
                else:
                    logger.warning(f"Webhook returned {resp.status_code}: {resp.text}")
                    return False
        except httpx.HTTPError as e:
            logger.warning(f"Webhook request failed: {e}")
            return False

    async def send_and_wait(
        self,
        prompt: str,
        project: str = "",
        session_key: str = "",
        timeout: float | None = None,
        on_reply=None,
    ) -> str | None:
        """Send prompt via webhook, then poll the session history for the reply.

        cc-connect (`relay send`) requires a chat → project binding that is only
        auto-created when cc-connect itself receives a Feishu message on that
        chat. When feishu-agent-platform is the sole Feishu gateway, that binding never
        happens, so `relay_send` fails with "no binding for this chat". Instead:

        1. Webhook-投递带任务标记的 prompt 到 project（fire-and-forget）。
        2. 轮询 session 历史，**按标记精准关联**：找到含 `[task:tag]` 的 user
           消息，取其后第一条 assistant 回复（跳过 NO_REPLY）。同一会话并发
           多任务时不会串台（位置基线 base_len 启发式在并发
           投递下会把别人的回复当自己的，标记关联才是精准匹配）。
        3. 超时且挂了 on_reply → 转后台守望（上限 _LATE_GRACE_SEC），迟到结果
           经回调投递，同步返回 LATE_DELIVERY 哨兵（新闻SQL 事故：17 分钟产出
           被 90s 超时静默吞掉）。

        Args:
            prompt: The prompt text to send（标记由本方法注入，调用方不感知）。
            project: Target project name. Defaults to config.default_project.
            session_key: Session key (e.g. "feishu:{chat}:{user}").
            timeout: 轮询超时（秒），默认 _POLL_TIMEOUT。
            on_reply: 可选 async fn(text)，超时后迟到回复的投递回调。

        Returns:
            回复文本；超时且挂了 on_reply 时返回 LATE_DELIVERY；失败/超时返回 None。

        Note:
            受 max_sessions 并发闸约束：最多 N 个会话同时在跑，其余排队。
            同一 session_key 重复投递时 cc-connect 侧 resume 同一 Claude Code
            会话（is_resume=true），上下文天然延续——无需本层做会话池。
        """
        project = project or self.config.default_project
        timeout = timeout or _POLL_TIMEOUT

        # 并发闸：整个「投递→轮询到回复」周期占一个 Claude Code 会话额度。
        # 超过 max_sessions 的请求在此排队（不拒绝），保证 cc-connect 侧
        # 同时至多 N 个 Claude Code 会话在跑。
        async with self._session_slots:
            return await self._send_and_wait_locked(
                prompt, project, session_key, timeout, on_reply=on_reply
            )

    async def _send_and_wait_locked(
        self,
        prompt: str,
        project: str,
        session_key: str,
        timeout: float,
        on_reply=None,
    ) -> str | None:
        """send_and_wait 的执行体（调用方已持有会话额度）。"""
        # 任务标记：注入 prompt 头部，历史里 user 帧原样保留 → 精准关联锚点
        tag = uuid.uuid4().hex[:8]
        tagged_prompt = f"[task:{tag}] {prompt}"

        # 1) 投递 prompt（fire-and-forget，webhook 同步 200 即入队）
        ok = await self.fire_and_forget(
            tagged_prompt, project=project, session_key=session_key
        )
        if not ok:
            logger.warning("send_and_wait: webhook delivery failed")
            return None

        # 2) 轮询历史，按 tag 关联取回复（"" = 任务帧在但回复未就绪，继续等）
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            reply = self._find_reply_by_tag(project, session_key, tag)
            if reply:
                logger.info(
                    "send_and_wait: got reply (%d chars) in %ds tag=%s",
                    len(reply), time.time() - (deadline - timeout), tag,
                )
                return reply
        if on_reply is not None:
            logger.info(
                "send_and_wait: %ds 内无回复 → 后台守望 tag=%s（上限 %ds，迟到结果经回调投递）",
                int(timeout), tag, _LATE_GRACE_SEC,
            )
            asyncio.get_running_loop().create_task(
                self._late_reply_watcher(
                    project, session_key, tag,
                    time.time() + _LATE_GRACE_SEC, on_reply,
                )
            )
            return LATE_DELIVERY
        logger.warning("send_and_wait: timed out after %ds (no reply for tag=%s)", timeout, tag)
        return None

    def _find_reply_by_tag(
        self, project: str, session_key: str, tag: str
    ) -> str | None:
        """历史里找含 [task:tag] 的 user 帧，返回其应得的 assistant 回复。

        关联规则 = 「本任务帧之前还有几个未答复的 user 帧」：同一会话内
        cc-connect 按 FIFO 串行处理（并发闸保证同 session_key 不并行），所以
        排在前面、尚未答复的任务先拿回复——B 的快回复不能算给 A。
        有效回复判定跳过 NO_REPLY 占位与健康探测 pong（pong 是 fire_and_forget
        探测的应答，不对应任何 task 任务帧，计入会错位）。

        返回 ""（空串）= 找到任务但回复未就绪；None = 历史不可读/任务帧未出现。
        """
        msgs = self._read_session_history(project, session_key)
        if not msgs:
            return None
        marker = f"[task:{tag}]"
        idx = -1
        for i, m in enumerate(msgs):
            if m.get("role") == "user" and marker in str(m.get("content") or ""):
                idx = i  # 取最后一次出现（同 tag 重投递时以最新为准）
        if idx < 0:
            return None

        def _valid_assistant(content: str) -> bool:
            c = content.strip()
            return bool(c) and c.upper() != "NO_REPLY" and c.lower().lstrip("🟢✅ ").strip() != "pong"

        # 本任务帧之前的「未答复 user 帧」数 = 排在我们前面的队列深度
        ahead = 0
        for j in range(idx):
            if msgs[j].get("role") != "user":
                continue
            answered = any(
                m.get("role") == "assistant" and _valid_assistant(str(m.get("content") or ""))
                for m in msgs[j + 1: idx]
            )
            if not answered:
                ahead += 1
        # 从本帧往后，第 (ahead+1) 条有效 assistant 回复才是我们的
        skip = ahead
        for m in msgs[idx + 1:]:
            if m.get("role") != "assistant":
                continue
            content = str(m.get("content") or "").strip()
            if not _valid_assistant(content):
                continue
            if skip > 0:
                skip -= 1
                continue
            return content
        return ""

    async def _late_reply_watcher(
        self, project: str, session_key: str, tag: str,
        deadline: float, on_reply,
    ) -> None:
        """超时后的后台迟到回复守望者（与同步轮询同一 tag 关联口径）。"""
        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            reply = self._find_reply_by_tag(project, session_key, tag)
            if reply:
                logger.info(
                    "late reply arrived (%d chars) tag=%s — delivering via on_reply",
                    len(reply), tag,
                )
                try:
                    await on_reply(reply)
                except Exception as e:
                    logger.warning("on_reply callback failed: %s", e)
                return
        logger.info("late watcher expired with no reply (%s tag=%s)", session_key[:30], tag)

    # ── session 历史文件定位与读取 ──

    def _session_file(self, project: str) -> Path | None:
        """定位 project 的 session 历史文件（~/.cc-connect/sessions/{project}_*.json）。"""
        data_dir = Path(self.config.data_dir) if self.config.data_dir else Path.home() / ".cc-connect"
        sessions_dir = data_dir / "sessions"
        if not sessions_dir.is_dir():
            return None
        # 取最新的 {project}_*.json（可能有多个历史文件）
        candidates = sorted(
            sessions_dir.glob(f"{project}_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _read_session_history(self, project: str, session_key: str) -> list[dict] | None:
        """读取指定 project+session_key 的历史消息列表（user/assistant）。"""
        f = self._session_file(project)
        if not f:
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            active = (data.get("active_session") or {}).get(session_key)
            if not active:
                return None
            s = (data.get("sessions") or {}).get(active, {})
            return s.get("history", [])
        except Exception as e:
            logger.warning("Failed to read session history %s: %s", f, e)
            return None

    async def relay_send(
        self,
        message: str,
        to_project: str = "",
        from_project: str = "dev-bot",
        session_key: str = "",
        platform: str = "",
        scope: str = "",
        user_id: str = "",
    ) -> str | None:
        """Send a message to another project's agent and wait for response.

        Uses `cc-connect relay send` CLI command synchronously.

        Args:
            message: The message/prompt to send.
            to_project: Target project name.
            from_project: Source project name (default: dev-bot).
            session_key: Session key for context continuity.
                         If provided, used as-is (must be `{platform}:{scope}:{user_id}` format).
            platform: Platform name (e.g. "feishu"). Used to build session_key if not provided.
            scope: Scope/chat ID. Used to build session_key if not provided.
            user_id: User identifier. Used to build session_key if not provided.

        Returns:
            The agent's response text, or None on failure.
        """
        to_project = to_project or self.config.default_project

        # Build session_key in the format cc-connect expects: {platform}:{scope}:{user_id}
        # If not already in that format and we have components, build it.
        if not session_key and platform:
            session_key = f"{platform}:{scope or ''}:{user_id or ''}"

        cmd = ["cc-connect.cmd", "relay", "send", "--to", to_project, "--from", from_project]

        if session_key:
            cmd.extend(["--session-key", session_key])
        if self.config.data_dir:
            cmd.extend(["--data-dir", self.config.data_dir])

        cmd.extend(["--message", message])

        logger.info(f"Relay send: {from_project} → {to_project}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.config.timeout_sec,
                )
            except TimeoutError:
                process.kill()
                logger.warning(f"Relay send timed out ({self.config.timeout_sec}s)")
                return None

            if process.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace").strip()
                logger.warning(f"Relay send failed (exit={process.returncode}): {stderr_text[:200]}")
                return None

            response = stdout.decode("utf-8", errors="replace").strip()
            logger.info(f"Relay response from {to_project}: {len(response)} chars")
            return response

        except Exception as e:
            logger.warning(f"Relay send error: {e}")
            return None


# ── Singleton ───────────────────────────────────────────────────

_client: CCConnectClient | None = None


def get_client(config: CCConnectConfig | None = None) -> CCConnectClient:
    global _client
    if _client is None:
        _client = CCConnectClient(config or CCConnectConfig())
    return _client


def set_client(client: CCConnectClient):
    global _client
    _client = client


def reset_client():
    global _client
    _client = None