"""dsh ACP 客户端 — 全仓唯一触碰 ACP 协议的地方（conventions §L2）。

负责：spawn `dsh --profile <p>`、JSON-RPC over stdio 分帧收发、会话生命周期、
`session/update` 报文到内部事件（`events.py`）的转换、权限请求应答。
不负责：进程重启与健康探测（supervisor.py）、事件到卡片/TaskRecord 的翻译（events.py）。
调用方：`app/dsh/supervisor.py`；其余模块只通过 `app.dsh` 包入口拿适配器。

失败语义：统一抛本模块异常，不返回错误值。
- `DshUnavailableError`：可执行文件缺失 / 进程起不来 / 进程在调用中途退出 / stdio 断开。
- `DshProtocolError`：报文非法、RPC 返回 error、调用超时。调用方可重试，但重试前
  应确认会话是否仍存在（超时不等于未执行）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

from app.dsh.events import (
    StopEvent,
    TextChunkEvent,
    TextChunkKind,
    ToolCallEvent,
    ToolCallPhase,
    UsageEvent,
)

logger = logging.getLogger("dsh.adapter")

_PROTOCOL_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _local_launch() -> tuple[str, list[str]] | None:
    """仓库内安装的 dsh 启动方式 `(node, [bin.js])`；不存在返回 None。

    走 `node <bin.js>` 而不是 `.bin/dsh`：后者是 shell/cmd 包装器，Windows 上
    Python 直接 exec 会失败（ECUNKNOWN/ENOENT），而 `node` 本就要有才能跑 dsh。
    """
    entry = _REPO_ROOT / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not entry.is_file():
        return None
    node = shutil.which("node")
    if node is None:
        logger.warning("仓库内已装 dsh，但 PATH 里没有 node，回退全局安装")
        return None
    return node, [entry.as_posix()]
# 单帧上限：dsh 的输出会整块塞进一行 JSON（工具结果可能很大），
# 超过后仍按行切分，只是读缓冲会占更多内存——宁可占内存也不能截断报文。
_READ_CHUNK = 65536
_DEFAULT_TIMEOUT_SEC = 30.0
_PROMPT_TIMEOUT_SEC = 1800.0
# 关闭会话/进程时的等待窗口：超过就走强杀，避免关停卡死整个 bot。
_CLOSE_TIMEOUT_SEC = 15.0

_RAW_INPUT_LIMIT = 2000
# 自定义 profile 的 bundle 组合：base 提供内核，acp-app 提供 ACP stdio 服务。
# 与 dsh 自带 acp 模板同构，自定义 profile 必须自己声明（dsh 不会自动套模板）。
_DEFAULT_BUNDLES = ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app")


class DshError(Exception):
    """dsh 适配层错误基类。"""


class DshUnavailableError(DshError):
    """dsh 进程不可用（未安装 / 启动失败 / 中途退出 / stdio 断开）。"""


class DshProtocolError(DshError):
    """协议层失败（报文非法 / RPC 返回 error / 调用超时）。"""


@dataclass(frozen=True, slots=True)
class PermissionOption:
    """权限请求里的一个可选项（协议原样：optionId / name / kind）。"""

    option_id: str
    name: str = ""
    kind: str = ""


PermissionHandler = Callable[[str, tuple[PermissionOption, ...]], Awaitable[str | None]]
"""`(session_id, options) -> option_id | None`；None 表示取消（不选任何项）。

默认实现批准第一个选项；后续接飞书审批卡时替换此回调即可，适配层无需改动。
"""
EventHandler = Callable[[Any], None]
"""事件回调，在读取协程内同步调用——实现方不得阻塞，重活自行 create_task。"""


class DshAdapter:
    """一个 dsh 进程 + 其上多个 ACP 会话的客户端。

    事件通过构造时传入的 `on_event` 回调分发（事件对象来自 `app.dsh.events`）；
    回调抛异常会被吞掉并记日志——观测失败不允许打断执行。
    """

    def __init__(
        self,
        *,
        dsh_bin: str | None = None,
        profile: str = "agentbiz",
        dsh_home: str | os.PathLike[str] | None = None,
        patch_path: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        on_event: EventHandler | None = None,
        permission_handler: PermissionHandler | None = None,
        request_timeout: float = _DEFAULT_TIMEOUT_SEC,
        prompt_timeout: float = _PROMPT_TIMEOUT_SEC,
    ) -> None:
        self._dsh_bin = dsh_bin
        self._profile = profile
        self._dsh_home = os.fspath(dsh_home) if dsh_home is not None else None
        self._patch_path = os.fspath(patch_path) if patch_path is not None else None
        self._env_extra = dict(env or {})
        self._on_event = on_event
        self._permission_handler = permission_handler or _approve_first_option
        self._request_timeout = request_timeout
        self._prompt_timeout = prompt_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._closed = False
        self._image_prompts = False

    # ── 进程与握手 ──

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def image_prompts_supported(self) -> bool:
        """本连接的当前路由是否接受 inline 图片（initialize 时协商）。"""
        return self._image_prompts

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closing

    async def start(self) -> None:
        """spawn 进程并完成 initialize。

        Raises:
            DshUnavailableError: 找不到可执行文件或进程起不来。
            DshProtocolError: 握手超时或返回协议错误。
        """
        if self.running:
            return
        exe = self._resolve_binary()
        self._ensure_profile()
        # 仓库内安装给的是 bin.js 路径，需要 node 前缀；全局安装给的是可直接
        # exec 的 .cmd（Windows）/ 无扩展名脚本（POSIX）。
        cmd = [exe, "--profile", self._profile]
        if exe.endswith("bin.js"):
            cmd = [shutil.which("node") or "node", exe, "--profile", self._profile]
        if self._patch_path:
            cmd += ["--patch", self._patch_path]
        try:
            # Windows 下 dsh 是 .cmd 包装器（内部再拉 node），不能走 shell=True：
            # cmd.exe 会多一层进程且引号/转义规则不同，整树回收也更麻烦。
            # CREATE_NEW_PROCESS_GROUP 让子树不继承 bot 的 Ctrl+C，交由 supervisor 整树回收。
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.getcwd(),
                env=self._build_env(),
                creationflags=creationflags,
            )
        except OSError as e:
            raise DshUnavailableError(f"dsh 启动失败: {e}") from e

        self._closing = False
        self._closed = False
        self._spawn_background("read", self._read_loop())
        self._spawn_background("stderr", self._drain_stderr())
        logger.info("dsh spawned (pid=%s, profile=%s)", self._proc.pid, self._profile)
        try:
            await self.initialize()
        except DshError:
            # 握手失败时进程可能还活着（半死），就地回收：
            # 交给调用方处理会留下一个占着 DSH_HOME 的孤儿进程。
            await self._force_release()
            raise

    async def initialize(self) -> dict[str, Any]:
        """ACP 握手；返回 result（含 agentCapabilities）。"""
        result = await self._rpc(
            "initialize",
            {"protocolVersion": _PROTOCOL_VERSION, "clientCapabilities": {}},
            timeout=self._request_timeout,
        )
        self._image_prompts = _advertises_image_prompts(result)
        if not self._image_prompts:
            logger.info("dsh 未广告 inline image 提示能力，图片将降级为文字说明")
        return result

    async def shutdown(self) -> None:
        """关闭所有会话后释放进程（不强杀；强杀由 supervisor 负责）。"""
        if self._proc is None:
            return
        self._closing = True
        for session_id in list(self._sessions):
            with contextlib.suppress(DshError):
                await self.session_close(session_id)
        self._sessions.clear()
        self._close_stdin()
        await self.wait_exit(_CLOSE_TIMEOUT_SEC)

    async def wait_exit(self, timeout: float) -> bool:
        """等待进程退出（强杀后调用，让 asyncio 回收 transport）。返回是否已退出。"""
        proc = self._proc
        if proc is None:
            return True
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except (TimeoutError, ProcessLookupError):
            return False
        finally:
            self._closed = True
            self._cancel_background()

    async def _force_release(self) -> None:
        """强杀进程并等待回收（握手失败 / 崩溃后的自清理路径）。"""
        proc = self._proc
        if proc is None:
            return
        self._closing = True
        try:
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=_CLOSE_TIMEOUT_SEC,
                    check=False,
                )
            else:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        except Exception as e:
            logger.warning("强杀 dsh 进程失败 (pid=%s): %s", proc.pid, e)
        await self.wait_exit(_CLOSE_TIMEOUT_SEC)

    # ── 会话 ──

    async def session_new(
        self,
        cwd: str,
        mcp_servers: Sequence[Mapping[str, Any]] | None = None,
    ) -> str:
        """创建会话，返回 sessionId。cwd 必须是绝对路径（dsh 侧强校验）。"""
        if not os.path.isabs(cwd):
            raise DshProtocolError(f"session/new 的 cwd 必须是绝对路径: {cwd!r}")
        result = await self._rpc(
            "session/new",
            {"cwd": cwd, "mcpServers": [dict(m) for m in (mcp_servers or [])]},
            timeout=self._request_timeout,
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise DshProtocolError(f"session/new 未返回 sessionId: {result!r}")
        self._sessions.add(session_id)
        return session_id

    async def session_set_model(self, session_id: str, provider: str, model: str) -> None:
        """切换本会话模型；value 是 JSON 字符串（双重编码，协议就是这么定的）。"""
        # 分隔符必须紧凑：dsh 用字符串相等匹配 configOptions 里的 value，
        # 默认分隔符会在逗号后加空格（`["a", "b"]`），与服务端给出的
        # `["a","b"]` 不相等，报 unknown model option。
        value = json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))
        await self._rpc(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": value},
            timeout=self._request_timeout,
        )

    async def session_prompt(
        self,
        session_id: str,
        text: str,
        blocks: Sequence[dict[str, Any]] | None = None,
    ) -> StopEvent:
        """提交一轮提示词并等待其结算。

        `blocks` 是完整的内容块序列（含图片）；省略时退化成纯文本块。
        图片能力按 `initialize` 广告的 `agentCapabilities.promptCapabilities.image`
        判断——服务端没广告却在 prompt 里塞图片块会整轮被拒，所以送到 adapter
        之前由 `media.build_prompt` 的 `text_only()` 降级。

        文本与工具事件在过程中经 `on_event` 分发；返回值里的 stopReason 表示结算原因。
        一次会话同时只能有一个 prompt，并发调用会被 dsh 拒绝（协议错误）。
        """
        prompt: list[dict[str, Any]] = (
            list(blocks) if blocks is not None else [{"type": "text", "text": text}]
        )
        result = await self._rpc(
            "session/prompt",
            {"sessionId": session_id, "prompt": prompt},
            timeout=self._prompt_timeout,
        )
        stop = StopEvent(
            session_id=session_id,
            stop_reason=str(result.get("stopReason") or "end_turn"),
        )
        self._emit(stop)
        return stop

    async def session_cancel(self, session_id: str) -> None:
        """取消进行中的 prompt（notification，无响应）。"""
        self._write({"jsonrpc": "2.0", "method": "session/cancel",
                     "params": {"sessionId": session_id}})

    async def session_close(self, session_id: str) -> None:
        """关闭会话；会话不存在视为已关闭（幂等）。"""
        if session_id not in self._sessions:
            return
        try:
            await self._rpc(
                "session/close", {"sessionId": session_id}, timeout=_CLOSE_TIMEOUT_SEC
            )
        except DshProtocolError:
            # 会话已被服务端清理时 close 会报 unknown session，
            # 对调用方而言结果等价于已关闭。
            logger.debug("session close 报错（按已关闭处理）: %s", session_id)
        finally:
            self._sessions.discard(session_id)

    # ── JSON-RPC 收发 ──

    async def _rpc(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        if not self.running:
            raise DshUnavailableError(f"dsh 进程不可用，无法调用 {method}")
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                         "params": dict(params)})
            message = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise DshProtocolError(f"{method} 超时（>{timeout:.0f}s）") from None
        finally:
            self._pending.pop(request_id, None)

        error = message.get("error")
        if error is not None:
            raise DshProtocolError(f"{method} 返回错误: {_error_text(error)}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise DshProtocolError(f"{method} 返回非对象 result: {result!r}")
        return result

    def _write(self, payload: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed:
            raise DshUnavailableError("dsh stdin 不可写（进程已退出或已关闭）")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError) as e:
            raise DshUnavailableError(f"dsh stdin 写入失败: {e}") from e

    async def _read_loop(self) -> None:
        # 按字节缓冲自行切行：StreamReader.readline 有 64KB 上限，
        # 大工具结果会整行超限，直接抛 LimitOverrunError 把读取循环打死。
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buffer = b""
        try:
            while True:
                chunk = await proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    line, buffer = buffer[:index], buffer[index + 1:]
                    text = line.strip()
                    if text:
                        self._handle_frame(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("dsh stdout 读取循环异常退出: %s", e)
        finally:
            self._fail_pending(DshUnavailableError("dsh 进程 stdio 已关闭"))

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                # dsh 的 stderr 是诊断信息（协议只在 stdout），只记尾部片段，
                # 避免把整段错误栈塞进日志。
                logger.debug("dsh stderr: %s", line.decode("utf-8", "replace").strip()[:500])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("dsh stderr 读取失败: %s", e)

    def _handle_frame(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("dsh 输出非 JSON（已忽略）: %s", raw[:200])
            return
        if not isinstance(message, dict):
            logger.warning("dsh 输出非对象（已忽略）: %s", raw[:200])
            return

        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                self._spawn_background(
                    "reply", self._answer_server_request(message["id"], method, message)
                )
            elif method == "session/update":
                self._handle_update(message.get("params"))
            else:
                logger.debug("dsh 通知（未处理）: %s", method)
            return

        request_id = message.get("id")
        if isinstance(request_id, int) and request_id in self._pending:
            future = self._pending[request_id]
            if not future.done():
                future.set_result(message)
        else:
            logger.debug("dsh 响应无匹配请求: id=%r", request_id)

    async def _answer_server_request(
        self, request_id: Any, method: str, message: Mapping[str, Any]
    ) -> None:
        if method != "session/request_permission":
            # 未知的客户端方法必须显式报错，否则 dsh 会一直等这个响应。
            self._safe_write({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not supported: {method}"},
            })
            return

        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        session_id = str(params.get("sessionId") or "")
        options = tuple(
            PermissionOption(
                option_id=str(option.get("optionId", "")),
                name=str(option.get("name", "")),
                kind=str(option.get("kind", "")),
            )
            for option in params.get("options") or []
            if isinstance(option, dict) and option.get("optionId")
        )
        try:
            chosen = await self._permission_handler(session_id, options)
        except Exception as e:
            logger.error("权限回调异常，按拒绝处理: %s", e)
            chosen = None

        if chosen is None:
            outcome: dict[str, Any] = {"outcome": "cancelled"}
        else:
            outcome = {"outcome": "selected", "optionId": chosen}
        self._safe_write({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"outcome": outcome},
        })

    def _handle_update(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        update = params.get("update")
        if not isinstance(session_id, str) or not session_id or not isinstance(update, dict):
            return
        try:
            event = _translate_update(session_id, update)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("session/update 无法翻译（已忽略）: %s", e)
            return
        if event is not None:
            self._emit(event)

    def _emit(self, event: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.error("事件回调异常（已忽略）: %s", e)

    # ── 内部工具 ──

    def _spawn_background(self, name: str, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            _log_task_result(name, finished)

        task.add_done_callback(_done)

    def _cancel_background(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def _close_stdin(self) -> None:
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _safe_write(self, payload: Mapping[str, Any]) -> None:
        try:
            self._write(payload)
        except DshUnavailableError as e:
            logger.debug("丢弃写往已退出进程的报文: %s", e)

    def _ensure_profile(self) -> None:
        """确保 `$DSH_HOME/profiles/<profile>` 存在且带 bundle 列表。

        dsh 只对自带模板（acp/web/headless/sdk）做首启初始化，自定义 profile 名会
        直接报 "profile does not exist"，而建一个 profile 目录本质就是写
        package.json 的 bundles —— 这里用 ACP 组合复刻一遍，避免部署时手工前置步骤。
        """
        if not self._dsh_home:
            return
        profile_dir = Path(self._dsh_home) / "profiles" / self._profile
        manifest = profile_dir / "package.json"
        if manifest.exists():
            return
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "name": f"dsh-profile-{self._profile}",
                        "private": True,
                        "dependencies": {},
                        "dsh": {"profile": {"bundles": list(_DEFAULT_BUNDLES)}},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            logger.info("已初始化 dsh profile: %s", profile_dir)
        except OSError as e:
            raise DshUnavailableError(f"初始化 dsh profile 失败: {e}") from e

    def _resolve_binary(self) -> str:
        """定位 dsh 入口：显式配置 > 仓库内安装 > 全局安装。

        优先仓库内（`<repo>/node_modules/@deepseek-ai/dsh/lib/bin.js`）：版本由
        本仓 `package.json` 钉死，bot 用哪个版本就不受操作者的全局安装影响——
        桌面版升级 dsh 不会顺带改掉线上行为。回退全局是为了「没跑过 npm install
        也能启动」，但会在日志里写明用的是外部安装。
        """
        if self._dsh_bin:
            return self._dsh_bin
        local = _local_launch()
        if local is not None:
            logger.info("使用仓库内安装的 dsh: %s", local[1][0])
            return local[1][0]
        # npm 全局包在 Windows 上只有 .cmd 包装器可被 Python 直接执行；
        # 裸 `dsh` 是无扩展名 shell 脚本，create_subprocess_exec 会 ENOENT/EACCES。
        for candidate in ("dsh.cmd", "dsh"):
            found = shutil.which(candidate)
            if found:
                logger.warning("使用全局安装的 dsh（%s），版本不受本仓 package.json 约束", found)
                return found
        raise DshUnavailableError(
            "未找到 dsh 可执行文件：在本仓执行 `npm install`（package.json 已钉版本），"
            "或在配置里指定 [projects.dsh].dsh_bin"
        )

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env_extra)
        if self._dsh_home:
            env["DSH_HOME"] = self._dsh_home
        return env


def _translate_update(session_id: str, update: Mapping[str, Any]) -> Any | None:
    """ACP `session/update` → 内部事件；不认识的事件类型返回 None。"""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        return TextChunkEvent(
            session_id=session_id,
            kind=TextChunkKind.MESSAGE,
            text=_content_text(update.get("content")),
            message_id=str(update.get("messageId") or ""),
        )
    if kind == "agent_thought_chunk":
        return TextChunkEvent(
            session_id=session_id,
            kind=TextChunkKind.THOUGHT,
            text=_content_text(update.get("content")),
            message_id=str(update.get("messageId") or ""),
        )
    if kind == "tool_call":
        return ToolCallEvent(
            session_id=session_id,
            tool_call_id=str(update.get("toolCallId") or ""),
            title=str(update.get("title") or ""),
            status=str(update.get("status") or "in_progress"),
            kind=str(update.get("kind") or "other"),
            phase=ToolCallPhase.STARTED,
            raw_input=_raw_input(update.get("rawInput")),
        )
    if kind == "tool_call_update":
        return ToolCallEvent(
            session_id=session_id,
            tool_call_id=str(update.get("toolCallId") or ""),
            title=str(update.get("title") or ""),
            status=str(update.get("status") or "in_progress"),
            kind=str(update.get("kind") or "other"),
            phase=ToolCallPhase.UPDATED,
            output_preview=_result_text(update.get("content")),
        )
    if kind == "usage_update":
        return UsageEvent(
            session_id=session_id,
            used_tokens=int(update.get("used") or 0),
            context_size=int(update.get("size") or 0),
        )
    logger.debug("未知 session/update 类型: %s", kind)
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(_content_text(item) for item in content)
    return ""


def _result_text(content: Any) -> str:
    """工具结果块的文本：`[{type: content, content: {type: text, text}}]` 或裸块。"""
    if isinstance(content, dict):
        inner = content.get("content")
        if inner is not None:
            return _content_text(inner)
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(_result_text(item) for item in content)
    return ""


def _raw_input(raw: Any) -> str:
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if len(text) <= _RAW_INPUT_LIMIT:
        return text
    return text[:_RAW_INPUT_LIMIT] + "…"


def _advertises_image_prompts(initialize_result: Mapping[str, Any]) -> bool:
    """服务端是否广告了 inline image 提示能力。

    dsh 侧只有在「当前路由模型声明了 image 输入」时才会把这个标成 true；
    没广告却提交图片块，整轮 prompt 会被 ACP 以 invalid params 拒绝。
    """
    caps = initialize_result.get("agentCapabilities")
    if not isinstance(caps, Mapping):
        return False
    prompt_caps = caps.get("promptCapabilities")
    return isinstance(prompt_caps, Mapping) and prompt_caps.get("image") is True


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _log_task_result(name: str, task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("dsh 后台任务 %s 失败: %s", name, error)


async def _approve_first_option(
    _session_id: str, options: tuple[PermissionOption, ...]
) -> str | None:
    """默认权限策略：批准第一个选项；没有选项时取消。"""
    return options[0].option_id if options else None
