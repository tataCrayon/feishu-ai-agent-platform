"""MemoryCore Gateway 生命周期管理 — 随 bot 启动拉起、随 bot 关停停止。

对接腾讯开源 TencentDB-Agent-Memory（tdai-agent-memory/MemoryCore）：
bot 的记忆网关（app/memory/client.py）依赖该服务（127.0.0.1:8420），
此前靠手动 `npx tsx src/gateway/server.ts` 启动，机器重启后无人拉起，
bot 侧 recall/capture 全部静默失败。本模块把它纳入 bot 进程生命周期：

- 启动：health 检查 → 已在跑则复用；否则 spawn 子进程
  （`node --import tsx src/gateway/server.ts`，单进程树，便于整树回收），
  轮询就绪（默认 18s 上限）。
- 模型：LLM 三要素经环境变量 TDAI_LLM_* 注入（config.ts 的优先级：
  env > yaml），单源 bot 根 .env 的 LLM_MODEL/LLM_BASE_URL/LLM_API_KEY，
  与主编排器同模型同网关——yaml 里只留 ${VAR} 占位符。
- 关停：terminate() 用 taskkill /T /F 整树回收（Windows 上 npx→tsx→node
  多层子进程，仅 kill 主进程会留孤儿）。

配置：config.toml [projects.memory.lifecycle]（enabled/manage_process）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("memory.lifecycle")

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

DEFAULT_BASE_URL = "http://127.0.0.1:8420"
_READY_TIMEOUT_SEC = 18.0
_POLL_INTERVAL_SEC = 1.5

# Windows：taskkill /T 连带整棵进程树；非 Windows 直接 kill
_IS_WINDOWS = os.name == "nt"


@dataclass
class MemoryLifecycleConfig:
    """随 bot 托管 MemoryCore 进程的配置。"""

    enabled: bool = True            # 总开关；false = 不托管（沿用旧的手动方式）
    base_url: str = DEFAULT_BASE_URL
    # MemoryCore 仓库的 MemoryCore 目录（含 tdai-gateway.yaml / src/gateway/server.ts）
    core_dir: str = "~/IdeaProjects/tdai-agent-memory/MemoryCore"
    manage_process: bool = True     # true=bot 拉起/回收进程；false=只做健康检查不托管


class MemoryCoreLifecycle:
    """MemoryCore Gateway 进程托管器（启动拉起 + 关停整树回收）。"""

    def __init__(self, config: MemoryLifecycleConfig):
        self._cfg = config
        self._proc: subprocess.Popen | None = None
        # 子进程 stdout 的重定向句柄。须由本类持有并在 shutdown 关闭：
        # 交给 Popen 后仍归调用方所有，不关就是每次重启泄漏一个 fd。
        self._log_file: Any = None

    # ── 启动 ──

    async def startup(self) -> bool:
        """bot 启动时调用：确保 MemoryCore 可用。

        Returns:
            True = 服务可用（已在跑或本次拉起成功）。
        """
        if not self._cfg.enabled:
            return False

        if await self._is_healthy():
            logger.info("MemoryCore already healthy at %s — reuse", self._cfg.base_url)
            return True

        if not self._cfg.manage_process:
            logger.warning(
                "MemoryCore unreachable and manage_process=false — "
                "recall/capture will fail until it is started manually"
            )
            return False

        core_dir = Path(self._cfg.core_dir).expanduser()
        server_ts = core_dir / "src" / "gateway" / "server.ts"
        if not server_ts.exists():
            logger.error("MemoryCore server.ts not found: %s — skip spawn", server_ts)
            return False

        env = self._build_env()
        cmd = self._spawn_command(core_dir)
        logger.info("Spawning MemoryCore: %s (cwd=%s)", " ".join(cmd), core_dir)
        try:
            # CREATE_NEW_PROCESS_GROUP：Windows 下让子树独立于 bot 的 Ctrl+C，
            # 由我们显式 taskkill /T 回收，避免信号传播次序问题。
            # 子进程输出落 logs/memorycore/（服务是黑盒，排障靠它的日志）
            log_dir = PROJECT_DIR / "logs" / "memorycore"
            log_file: Any = subprocess.DEVNULL
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_file = open(log_dir / "gateway.log", "ab")  # noqa: SIM115
                self._log_file = log_file
            except Exception:
                pass
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(core_dir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception as e:
            logger.error("Failed to spawn MemoryCore: %s", e)
            return False

        if await self._wait_ready():
            logger.info(
                "MemoryCore started (pid=%s) and healthy at %s",
                self._proc.pid, self._cfg.base_url,
            )
            return True
        logger.error("MemoryCore did not become healthy within %.0fs", _READY_TIMEOUT_SEC)
        return False

    async def _wait_ready(self) -> bool:
        deadline = time.monotonic() + _READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                logger.error("MemoryCore process exited early (code=%s)", self._proc.returncode)
                return False
            if await self._is_healthy():
                return True
            await _sleep(_POLL_INTERVAL_SEC)
        return False

    # ── 关停 ──

    def shutdown(self) -> None:
        """bot 退出时调用：整树回收本次拉起的 MemoryCore。

        只回收自己 spawn 的进程（_proc 非 None），外部已运行的实例不动。
        """
        if self._proc is None:
            return
        pid = self._proc.pid
        logger.info("Shutting down MemoryCore process tree (pid=%s)", pid)
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=10, check=False,
                )
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception as e:
            logger.warning("MemoryCore shutdown error: %s", e)

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                logger.debug("close memorycore log file failed", exc_info=True)
            self._log_file = None

    # ── 内部 ──

    async def _is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self._cfg.base_url}/health")
                return resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception:
            return False

    def _build_env(self) -> dict[str, str]:
        """把 bot .env 的 LLM 三要素映射为 TDAI_LLM_* 注入子进程。

        config.ts 优先级 env > yaml：yaml 里是 ${TDAI_LLM_*} 占位符，
        环境变量存在时直接生效——模型与 bot 主链同源（.env 单值）。
        """
        env = os.environ.copy()
        mapping = {
            "TDAI_LLM_BASE_URL": "LLM_BASE_URL",
            "TDAI_LLM_API_KEY": "LLM_API_KEY",
            "TDAI_LLM_MODEL": "LLM_MODEL",
        }
        for target, source in mapping.items():
            value = os.getenv(source, "")
            if value:
                env[target] = value
            else:
                env.pop(target, None)  # 防继承到无关值
        return env

    def _spawn_command(self, core_dir: Path) -> list[str]:
        """单进程启动：node --import tsx（官方注释推荐的等价形式）。

        不走 npx：npx 会在 Windows 上拉出 npx→tsx-cli→node 三层树，
        这里 node --import tsx 只有一层 node，进程管理简单可靠。
        cwd 已是 core_dir，tsx 从 node_modules 就近解析。
        """
        if _IS_WINDOWS:
            node_exe = _find_node()
            return [node_exe, "--import", "tsx", "src/gateway/server.ts"]
        return ["node", "--import", "tsx", "src/gateway/server.ts"]


def _find_node() -> str:
    """定位 node.exe（优先 PATH，其次常见安装位置）。"""
    from shutil import which
    found = which("node")
    if found:
        return found
    for candidate in (
        r"D:\DevEnv\node-v24.18.0-win-x64\node.exe",
        r"C:\Program Files\nodejs\node.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return "node"


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
