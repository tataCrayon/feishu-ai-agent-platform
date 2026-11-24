"""dsh 进程托管 — 启动、健康探测、崩溃自愈、优雅关停。

负责：拉起 `DshAdapter`、监视子进程存活、按指数退避重启、关停时先会话后整树回收。
不负责：协议细节（adapter.py）、事件翻译（events.py）。
调用方：bot 启动/关停路径（app/main.py）与需要按需取用适配器的上层模块。

失败语义：不向上抛 IO 类异常。`start()` 返回是否可用；运行中崩溃由后台监视
协程吞掉并重启——循环因异常退出就再也不会回来，用户只会看到「从此不响了」。
`acquire()` 在不可用时抛 `DshUnavailableError`，由调用方转成用户可见回执。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.dsh.adapter import (
    DshAdapter,
    DshError,
    DshProtocolError,
    DshUnavailableError,
    EventHandler,
    PermissionHandler,
)
from app.dsh.routing import RULES_PATH, load_rules

logger = logging.getLogger("dsh.supervisor")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

# 崩溃重启上限：连崩 3 次说明是配置/环境问题，继续重试只会刷屏。
_MAX_RESTARTS = 3
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0
# 崩溃后重启前先等一小段，给 dsh 的端口/文件锁释放时间。
_RESTART_DELAY_SEC = 0.5
# 存活探测节奏：进程死掉要尽快发现，但也不能空转。
_HEALTH_INTERVAL_SEC = 5.0
_PROBE_TIMEOUT_SEC = 20.0
_TERMINATE_TIMEOUT_SEC = 10.0
# 稳定运行这么久之后把重启计数清零：否则 bot 连续跑几天、偶发崩三次就会
# 永久失去自愈能力；反之短时间内连崩三次仍然判定为配置/环境问题。
_RESTART_COUNTER_RESET_SEC = 300.0

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(env: Any) -> list[dict[str, str]]:
    """agent.yaml 的 env 字典 → ACP 的 name/value 条目，并展开 `${VAR}`。

    ACP 的 env 是**条目数组**而不是字典，直接传 dict 会被当成空列表：子进程
    也就拿不到任何数据源凭据，表现为 MCP 服务起来后立刻退出（`Missing required
    environment variables`）。未定义的变量置空而不是留下字面量——保留
    `${NACOS_USERNAME}` 只会让失败更难看出来。
    """
    if not isinstance(env, dict):
        return []
    return [
        {"name": str(k), "value": _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), str(v))}
        for k, v in env.items()
    ]


# npm 提供的可执行文件在 Windows 上只有 `.cmd` 包装器，MCP 客户端不经过 shell，
# 直接 exec 会 FileNotFoundError（看起来像「服务起不来」）。这里把 npx/npm 拆成
# `node <cli.js>`：与 `.cmd` 内部做的事等价，且两个平台都成立。
_NPM_CLI = Path("node_modules") / "npm" / "bin"


def _resolve_command(command: str) -> tuple[str, list[str]]:
    """返回 (可执行文件绝对路径, 需要在原参数前补的启动参数)。

    空 command = 本仓 Python 解释器（模块形态的 server，如 `-m app.mcp.feishu_server`）。
    """
    if not command:
        return sys.executable, []
    if Path(command).is_absolute():
        return command, []
    name = Path(command).stem.lower()
    if name in ("npx", "npm"):
        node = shutil.which("node")
        if node:
            cli = Path(node).resolve().parent / _NPM_CLI / f"{name}-cli.js"
            if cli.is_file():
                return node, [str(cli)]
    return shutil.which(command) or command, []


# npx 在能联网时会先去 registry 解析包，而它连的是 registry.npmjs.org——这台机器
# 到不了（npmmirror 可达）。表现不是报错，而是 session/new 一直挂到超时。
# 因此：npx 缓存里有这个包时，直接指向缓存内的入口，完全不经过网络。
_NPX_CACHE = Path("node_cache") / "_npx"


def _npx_cached_entry(package: str) -> list[str] | None:
    """在 npx 缓存里找 `package` 的 bin 入口绝对路径。"""
    node = shutil.which("node")
    if node is None:
        return None
    cache = Path(node).resolve().parent / _NPX_CACHE
    if not cache.is_dir():
        return None
    for item in cache.iterdir():
        pkg_json = item / "node_modules" / package / "package.json"
        if not (item / "package.json").is_file() or not pkg_json.is_file():
            continue
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bins = pkg.get("bin")
        entry = bins if isinstance(bins, str) else next(iter((bins or {}).values()), None)
        if not entry:
            continue
        # dsh 不替 MCP 子进程补文件关联：Windows 上只能 exec `.exe`，而 npm 包里
        # 的 bin 往往是无扩展名脚本（靠 shebang 运行）。所以解析 shebang 换成
        # 解释器前缀，跟 `.cmd` 包装器里做的事一样。
        return _node_entry(pkg_json.parent / str(entry))
    return None


def _node_entry(target: Path) -> list[str] | None:
    """把 npm 包的 bin 目标解析成「给 node 的参数」。

    Windows 上 dsh 只能 exec `.exe`，而包里的 bin 常是无扩展名脚本（靠 shebang
    运行）。这里只看它能不能被 `node <path>` 跑起来；不能的直接返回 None 让调用方
    回退到 npx（宁可慢，也不要挂一个必然起不来的子进程）。
    """
    if not target.is_file():
        return None
    if target.suffix.lower() in (".exe", ".cmd", ".bat"):
        return None
    try:
        head = target.open("rb").read(120).decode("utf-8", "replace")
    except OSError:
        return None
    if head.startswith("#!"):
        return [target.as_posix()] if "node" in head.splitlines()[0] else None
    return [target.as_posix()]


def _cached_npx_args(command: str, args: list[str]) -> list[str] | None:
    """`npx -y <pkg> ...` 命中缓存时返回直接启动该包的参数。

    只替换 `-y` + 单个包名的形态：带版本约束（`pkg@1.2`）或带子命令的调用
    要保留 npx 的解析语义，交给原样转发。
    """
    if Path(command).stem.lower() != "npx":
        return None
    rest = [a for a in args if a not in ("-y", "--yes")]
    # `@scope/name` 是合法的包名（作用域），只有 `name@version` 才是版本约束。
    if not rest or rest[0].startswith("-") or rest[0].rpartition("@")[0]:
        return None
    entry = _npx_cached_entry(rest[0])
    if entry is None:
        return None
    return entry + rest[1:]


def _caller_env(chat_id: str, caller: dict[str, str] | None) -> dict[str, str]:
    """本会话的 MCP 子进程身份：谁在说话、在哪个会话。

    为什么放在 env 而不是工具参数：身份不能由模型填。工具参数是可生成的内容，
    模型从对话里「听说」一个用户名就会填进去，而订阅/关注必须绑真实发送者。
    dsh 给每个会话起独立的 MCP 进程，env 在启动时固定 → 天然做到「谁提问就是谁」。
    """
    env: dict[str, str] = {}
    if chat_id:
        env["PROMATE_CALLER_CHAT_ID"] = chat_id
    if caller:
        sender_id = str(caller.get("sender_id") or "")
        sender_name = str(caller.get("sender_name") or "")
        if sender_id:
            env["PROMATE_CALLER_SENDER_ID"] = sender_id
        if sender_name:
            env["PROMATE_CALLER_SENDER_NAME"] = sender_name
    return env


StartedHook = Callable[[DshAdapter], Awaitable[None]]
"""重启成功后回调（上层用它重建会话映射），异常只记日志。"""


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """dsh 进程托管配置。

    Attributes:
        profile: dsh profile 名（部署产物在 deploy/dsh/<profile>/）。
        dsh_home: 独立 DSH_HOME，避免与桌面版共享凭据与 session。
        patch_path: profile 之外的 overlay（默认用部署目录里的 cordis.patch.yml）。
        workspace: 会话工作目录（dsh 要求绝对路径）。
        model: (provider, model) 会话默认模型；None 表示沿用 profile 默认。
        dsh_bin / env: 可执行文件与额外环境变量（凭据从这里进）。
        mcp_servers: `session/new` 时挂载的 MCP server 规格列表。
    """

    profile: str = "agentbiz"
    dsh_home: Path = PROJECT_DIR.parent / ".dsh-bot"
    workspace: Path = PROJECT_DIR
    patch_path: Path = PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "cordis.patch.yml"
    model: tuple[str, str] | None = None
    dsh_bin: str | None = None
    env: dict[str, str] | None = None
    mcp_servers: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    max_restarts: int = _MAX_RESTARTS


class DshSupervisor:
    """单 dsh 进程的托管器：可用性判断、自动重启、关停。

    只有一个进程：dsh 会话本身支持并发（一个连接多个会话），
    多进程会带来 session 归属与并发闸的双份实现。
    """

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        on_event: EventHandler | None = None,
        permission_handler: PermissionHandler | None = None,
        on_restarted: StartedHook | None = None,
    ) -> None:
        self._cfg = config
        self._on_event = on_event
        self._permission_handler = permission_handler
        self._on_restarted = on_restarted
        self._adapter: DshAdapter | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._restart_count = 0
        self._started_at: float | None = None
        self._session_factory: Callable[[DshAdapter], Awaitable[None]] | None = None

    # ── 状态 ──

    @property
    def adapter(self) -> DshAdapter:
        """当前适配器；未启动时抛 `DshUnavailableError`。"""
        if self._adapter is None or not self._adapter.running:
            raise DshUnavailableError("dsh 进程未就绪：请先 start() 或等待自愈完成")
        return self._adapter

    @property
    def healthy(self) -> bool:
        return self._adapter is not None and self._adapter.running

    # ── 生命周期 ──

    async def start(self) -> bool:
        """拉起 dsh 并开始监视。返回是否可用（失败不抛，交由调用方决定是否降级）。"""
        # 规则在 profile 装配期就被读进系统提示词；这里先确认文件在，
        # 否则「会话照常建立但一句话规则都没有」只会在用户侧表现为乱答。
        self.verify_routing_rules()
        async with self._start_lock:
            if self.healthy:
                return True
            self._stopping.clear()
            ok = await self._spawn()
            if ok and self._monitor is None:
                self._monitor = asyncio.ensure_future(self._watch())
            return ok

    async def shutdown(self) -> None:
        """优雅关停：先关会话，再 terminate，超时后整树 kill。"""
        self._stopping.set()
        monitor = self._monitor
        self._monitor = None
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        await self._stop_adapter()
        logger.info("dsh supervisor 已关停")

    # ── 会话 ──

    def bind_session_factory(
        self, factory: Callable[[DshAdapter], Awaitable[None]]
    ) -> None:
        """登记重启后需要重新建立的会话（如 chat_id → sessionId 映射）。"""
        self._session_factory = factory

    async def open_session(
        self,
        chat_id: str = "",
        caller: dict[str, str] | None = None,
    ) -> tuple[DshAdapter, str]:
        """建一个会话并应用默认模型，返回 (adapter, sessionId)。

        路由规则不在这里注入：它由 profile 的 `system-prompt.personaPrefix` 装进
        系统提示词（见 `deploy/dsh/agentbiz/cordis.patch.yml` 与 `app/dsh/routing.py`），
        会话建立即生效，且不会被后续轮次冲淡或压缩裁剪。

        `chat_id` / `caller` 写进本会话 MCP 子进程的 env：dsh 给每个会话起自己
        的一份 MCP 进程（实测同一 spec 两个 session 是两个 pid），所以子进程里的
        能力工具能据此知道「现在是谁在说话、在哪个会话」——订阅/关注类写操作靠
        这个身份落地，模型口述的身份一律不认。
        """
        adapter = self.adapter
        env = _caller_env(chat_id, caller)
        session_id = await adapter.session_new(
            str(self._cfg.workspace), self.mcp_server_specs(env=env)
        )
        if self._cfg.model is not None:
            provider, model = self._cfg.model
            # 模型切换失败不阻断会话：profile 里已有默认模型，切换只是覆盖。
            with contextlib.suppress(DshProtocolError):
                await adapter.session_set_model(session_id, provider, model)
        return adapter, session_id

    def mcp_server_specs(
        self,
        only: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """构造 `session/new` 的 mcpServers 列表（已解析 command 与 env）。

        三处都是踩过的坑：
        - command 为空时用 `sys.executable`。dsh 子进程的 PATH 与 bot 进程不同
          （Windows 上 venv 常只在 bot 的激活脚本里生效），裸 `python` 会挂到
          一个没有本仓依赖的解释器上，表现出来只是「模型说没有这个工具」。
        - env 里的 `${VAR}` 在这里展开。dsh 不替 MCP 子进程做占位符替换，留着
          原样传下去等于把字面量 `${NACOS_USERNAME}` 当用户名。
        - Windows 上 `npx` / `npm` 是 `.cmd`，MCP 客户端直接 CreateProcess 找不到
          它们，只会看到「服务起不来」。这里解析成绝对路径的 node 入口。

        `env` 是**按会话**追加的键值（调用方身份 / 本会话 chat_id），与配置里的
        env 合并后一起下发；调用方身份覆盖配置里的同名项（身份只能有一个）。
        """
        specs: list[dict[str, Any]] = []
        for raw in self._cfg.mcp_servers:
            if only is not None and raw.get("name") != only:
                continue
            spec = dict(raw)
            args = [str(a) for a in (spec.get("args") or [])]
            cached = _cached_npx_args(str(spec.get("command") or ""), args)
            if cached is not None:
                # 缓存命中：command 用 node，args 只剩 `<bin.js> <原参数>`
                spec["command"] = shutil.which("node") or "node"
                spec["args"] = cached
            else:
                command, prefix = _resolve_command(str(spec.get("command") or ""))
                spec["command"] = command
                spec["args"] = prefix + args
            entries = _expand_env(spec.pop("env", None))
            if env:
                merged = {e["name"]: e["value"] for e in entries}
                merged.update(env)
                entries = [{"name": k, "value": v} for k, v in merged.items()]
            spec["env"] = entries
            # cwd 只影响**进程**工作目录，不影响 `-m` 找模块；dsh 也不把它透传给
            # stdio 子进程（实测：外部仓的 `-m order_agent.mcp_server`
            # 在 bot 仓库根下启动，No module named order_agent）。把目录并进
            # PYTHONPATH，`-m app.*` / `-m order_agent.*` 才与 cwd 无关地可寻址。
            if spec.get("cwd"):
                name = "PYTHONPATH"
                current = next((e["value"] for e in spec["env"]
                                if e["name"] == name), "")
                parts = [p for p in current.split(os.pathsep) if p]
                for extra in (str(PROJECT_DIR), str(spec["cwd"])):
                    if extra not in parts:
                        parts.append(extra)
                spec["env"] = [e for e in spec["env"] if e["name"] != name]
                spec["env"].append({"name": name, "value": os.pathsep.join(parts)})
            specs.append(spec)
        return specs

    def verify_routing_rules(self) -> None:
        """启动自检：规则必须可读且非空。规则缺失直接抛。

        规则本身由 profile 的 `system-prompt.personaSuffix` 注入（dsh 侧 `!!js`
        读同一份文件），这里只做前置校验——没有规则的会话等于随机分派，
        比起「跑得起来但乱答」，启动期失败更容易被发现。
        """
        load_rules()

    # ── 内部：拉起与监视 ──

    async def _spawn(self) -> bool:
        adapter = self._make_adapter()
        try:
            await asyncio.wait_for(adapter.start(), timeout=_PROBE_TIMEOUT_SEC)
        except (DshError, TimeoutError) as e:
            logger.error("dsh 启动失败: %s", e)
            await _kill_tree(adapter)
            return False
        self._adapter = adapter
        self._started_at = asyncio.get_running_loop().time()
        logger.info("dsh 就绪 (pid=%s)", adapter.pid)
        if self._on_restarted is not None:
            with contextlib.suppress(Exception):
                await self._on_restarted(adapter)
        return True

    def _make_adapter(self) -> DshAdapter:
        env = dict(self._cfg.env or {})
        # profile 里的 customSkillDirs 用 $DSH_BOT_SKILLS 取绝对路径，
        # 这里按仓库位置注入，部署目录搬迁时不必改 profile 文件。
        #
        # 指向**白名单同步产物**（deploy/dsh/agentbiz/skills），不是 bot/skills
        # 源目录：dsh 的技能目录就是路由候选集，源目录里混着 demo-guard / x 这类
        # 工具型与占位技能，直接指过去会让它们和业务能力抢分派（sync_skills.py
        # 与 cordis.patch.yml 的注释都是这个口径）。产物缺失时同步脚本会报错，
        # 静默回退到源目录反而把问题藏起来。
        env.setdefault(
            "DSH_BOT_SKILLS",
            _to_posix(PROJECT_DIR / "deploy" / "dsh" / "agentbiz" / "skills"),
        )
        # 业务规则文件：profile 的 system-prompt 段用 !!js 读它（规则正文 9KB，
        # 写进 yml 就是两份事实源）。这里按仓库位置注入，换机器不必改 profile。
        env.setdefault("DSH_ROUTING_RULES", _to_posix(RULES_PATH))
        # 共享 agents 根默认是 `~/.agents`（操作者与 Claude Code / Codex 共用的
        # 个人配置）。bot 必须独立：指到自己的空目录，既不带入个人技能/指令，
        # 也不往操作者家目录写东西。目录缺失也无妨——loader 只会读到「没有」。
        env.setdefault("DSH_AGENTS_HOME", _to_posix(self._cfg.dsh_home / "agents"))
        return DshAdapter(
            dsh_bin=self._cfg.dsh_bin,
            profile=self._cfg.profile,
            dsh_home=self._cfg.dsh_home,
            patch_path=self._cfg.patch_path if self._cfg.patch_path.exists() else None,
            env=env,
            on_event=self._on_event,
            permission_handler=self._permission_handler,
        )

    async def _watch(self) -> None:
        """存活监视 + 崩溃自愈。

        conventions §三：后台循环必须吞掉所有异常——循环一退出，
        自愈就永久失效且没有任何提示。
        """
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(_HEALTH_INTERVAL_SEC)
                if self._stopping.is_set():
                    return
                adapter = self._adapter
                if adapter is not None and adapter.running:
                    await self._probe(adapter)
                    self._reset_counter_after_uptime()
                    continue
                logger.warning("检测到 dsh 进程退出，进入重启流程")
                await self._restart()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("dsh 监视循环异常（继续运行）: %s", e)
                await asyncio.sleep(_BACKOFF_BASE_SEC)

    def _reset_counter_after_uptime(self) -> None:
        started_at = self._started_at
        if (
            self._restart_count
            and started_at is not None
            and asyncio.get_running_loop().time() - started_at >= _RESTART_COUNTER_RESET_SEC
        ):
            self._restart_count = 0
            logger.info("dsh 已稳定运行 %.0fs，重启计数清零", _RESTART_COUNTER_RESET_SEC)

    async def _probe(self, adapter: DshAdapter) -> None:
        """握手级健康探测：进程存活但 initialize 超时/报错 = 半死，按崩溃处理。"""
        try:
            await asyncio.wait_for(adapter.initialize(), timeout=_PROBE_TIMEOUT_SEC)
        except (DshError, TimeoutError) as e:
            logger.warning("dsh 健康探测失败，按崩溃处理: %s", e)
            await _kill_tree(adapter)
            await self._restart()

    async def _restart(self) -> None:
        if self._restart_count >= self._cfg.max_restarts:
            logger.error(
                "dsh 已连续重启 %d 次仍不可用，停止自愈（需人工介入检查 profile/凭据）",
                self._restart_count,
            )
            self._stopping.set()
            return
        self._restart_count += 1
        delay = min(_BACKOFF_BASE_SEC * (2 ** (self._restart_count - 1)), _BACKOFF_MAX_SEC)
        logger.warning(
            "dsh 重启第 %d/%d 次，%.1fs 后重试",
            self._restart_count, self._cfg.max_restarts, delay,
        )
        await _kill_tree(self._adapter)
        self._adapter = None
        await asyncio.sleep(delay + _RESTART_DELAY_SEC)
        if self._stopping.is_set():
            return
        async with self._start_lock:
            ok = await self._spawn()
        if ok:
            logger.info("dsh 重启成功，已连续重启 %d 次", self._restart_count)
            if self._session_factory is not None:
                with contextlib.suppress(Exception):
                    await self._session_factory(self.adapter)

    async def _stop_adapter(self) -> None:
        adapter = self._adapter
        self._adapter = None
        if adapter is None:
            return
        try:
            await asyncio.wait_for(adapter.shutdown(), timeout=_TERMINATE_TIMEOUT_SEC)
            return
        except Exception as e:
            logger.warning("dsh 优雅关停未完成，转整树回收: %s", e)
        await _kill_tree(adapter)


async def _kill_tree(adapter: DshAdapter | None) -> None:
    """整树回收子进程。

    Windows 上 dsh.cmd 会再拉 node：只 kill 主进程会留下 node 孤儿，
    下次启动时新旧两个进程抢同一个 DSH_HOME。
    """
    if adapter is not None:
        await adapter._force_release()


def _to_posix(path: Path) -> str:
    """absPath → 正斜杠形式。

    dsh 的 customSkillDirs 用 Node path.resolve 解析，Windows 反斜杠在 YAML
    双引号标量里还要转义；统一成正斜杠可避免两侧转义规则打架。
    """
    return path.resolve().as_posix()
