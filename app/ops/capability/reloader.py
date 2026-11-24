"""热重建 — 审批通过后把能力落盘并让运行时感知。

职责（设计稿 3.2 HotReloader）：
1. Agent 包 → bot/agents/{name}/；Skill 包 → bot/skills/{name}/
2. registry.reload() + setup_skills 重注入
3. generator 补写 CLAUDE.md / .claude/settings.json（MCP 隔离）
4. 回调 orchestrator.rebuild()：重派路由工具 → 重编译 graph → 原子替换
5. 同步 .claude/skills/（cc-connect 下个会话生效——如实回执给管理员）

部署失败时回滚落盘文件（备份目录恢复），旧图继续服务。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.agents.registry import AgentRegistry
from app.ops.capability.store import CapabilityRecord, CapabilityStore

logger = logging.getLogger("capability.reloader")

PROJECT_DIR = Path(__file__).resolve().parents[3]
AGENTS_DIR = PROJECT_DIR / "bot" / "agents"
SKILLS_DIR = PROJECT_DIR / "bot" / "skills"


class DeployResult:
    """rebuild 的结果汇总（卡片回执数据源）。"""

    def __init__(self) -> None:
        self.ok = False
        self.target_path: str = ""
        self.effective_scope = ""  # 即时生效 / 新会话生效说明
        self.degraded: list[str] = []  # MCP 凭据未配置等降级说明
        self.error = ""


class HotReloader:
    """把 pending 档案落盘为正式能力，并触发运行时热重建。"""

    def __init__(
        self,
        registry: AgentRegistry,
        store: CapabilityStore,
        rebuild_callback=None,
    ):
        """rebuild_callback: 无参异步或同步可调用（orchestrator.rebuild），
        返回 (ok, detail) 或 None；失败时由本模块回滚落盘。"""
        self._registry = registry
        self._store = store
        self._rebuild = rebuild_callback

    def deploy(self, rec: CapabilityRecord, pkg_dir: Path) -> DeployResult:
        """把解包目录部署为正式能力。失败时抛异常或返回 ok=False 并回滚。"""
        result = DeployResult()
        if rec.type == "skill":
            target = SKILLS_DIR / rec.name
        else:
            # agent.yaml 包：目录名取 agent name（与注册发现约定一致）
            target = AGENTS_DIR / rec.name
        result.target_path = str(target)

        installed = False
        backup_dir = self._store.backup_root() / (
            f"{rec.capability_id}-backup-{target.name}"
        )
        try:
            # D4：覆盖同名前备份旧版。落盘采用「同盘 rename 秒级切换」：
            # 旧版先 rename 暂存（保留原目录），新版装到临时目录校验完整后
            # 一次 rename 上位 —— copytree 中途失败不会留下半写目录，
            # 任何一步失败旧版都能原位恢复（回滚零拷贝）。
            if target.exists():
                self._store.backup_existing(target, rec.capability_id)
            staging = target.parent / f".staging-{rec.capability_id}"
            shutil.rmtree(staging, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pkg_dir, staging, ignore=shutil.ignore_patterns("manifest.json"))
            if not (staging / "SKILL.md").exists() and not (staging / "agent.yaml").exists():
                # 结构校验：没有能力入口文件的包不允许占位正式目录
                raise RuntimeError("落盘校验失败：staging 目录缺少 SKILL.md / agent.yaml")
            target_existed = target.exists()
            if target_existed:
                shutil.move(str(target), str(backup_dir) + "-target")
            shutil.move(str(staging), str(target))
            installed = True

            # registry 热重载 + Skill 重注入 + 生成物
            self._registry.reload()
            self._post_install()

            # graph 热重建（回调 orchestrator）
            if self._rebuild is not None:
                ok, detail = self._invoke_rebuild()
                if not ok:
                    result.error = f"graph 重建失败：{detail}"
                    raise RuntimeError(result.error)

            result.ok = True
            result.effective_scope = (
                "✅ 已即时生效（进程内路由/编排）"
                if rec.type == "agent"
                else "✅ 已生效（cc-connect 新会话可用；存量会话需重启 cc-connect daemon）"
            )
            result.degraded = self._check_degraded(rec.name)
            return result
        except Exception as e:
            logger.exception("[capability] deploy failed: %s", e)
            if installed:
                # 回滚：移除新落盘；旧版优先从原位暂存恢复（零拷贝），
                # 其次从 archive 备份目录恢复
                shutil.rmtree(target, ignore_errors=True)
                moved_aside = Path(str(backup_dir) + "-target")
                if moved_aside.exists():
                    shutil.move(str(moved_aside), str(target))
                    logger.warning("[capability] rolled back %s from in-place stash", target)
                elif backup_dir.exists():
                    shutil.copytree(backup_dir, target)
                    logger.warning("[capability] rolled back %s from archive backup", target)
                try:
                    self._registry.reload()
                    self._post_install()
                    if self._rebuild is not None:
                        self._invoke_rebuild()
                except Exception:
                    logger.exception("[capability] rollback rebuild failed (old graph keeps serving)")
            else:
                # 未 installed：staging/暂存目录清理，target 若被移开也恢复
                shutil.rmtree(target.parent / f".staging-{rec.capability_id}", ignore_errors=True)
                moved_aside = Path(str(backup_dir) + "-target")
                if moved_aside.exists() and not target.exists():
                    shutil.move(str(moved_aside), str(target))
            # 清理临时 stash（target 恢复完成后不再需要）
            shutil.rmtree(Path(str(backup_dir) + "-target"), ignore_errors=True)
            if not result.error:
                result.error = str(e)
            return result

    # ── 内部 ──

    def _invoke_rebuild(self) -> tuple[bool, str]:
        """兼容同步/异步 rebuild 回调。"""
        import inspect

        out = self._rebuild()
        if inspect.iscoroutine(out):
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在主事件循环线程内同步等待（deploy 本身跑在 executor 里）
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    out = ex.submit(loop.run_until_complete, out).result(timeout=60)
            else:
                out = loop.run_until_complete(out)
        return bool(out[0]), str(out[1]) if len(out) > 1 else ""

    def _post_install(self) -> None:
        """registry 重载后的配套生成：Skill 注入 + generator 生成物。"""
        from app.agents.generator import AgentConfigGenerator
        from app.agents.skills import setup_skills

        setup_skills(self._registry)
        AgentConfigGenerator(self._registry).write_settings_files()
        AgentConfigGenerator(self._registry).write_claude_md_files()

    def _check_degraded(self, agent_name: str) -> list[str]:
        """检测 MCP 凭据占位符是否已在 .env 配置；未配置项进降级清单。"""
        import os

        degraded: list[str] = []
        agent = self._registry.get(agent_name)
        if agent is None:
            return degraded
        for srv, cfg in (agent.mcp_servers or {}).items():
            for _k, v in (cfg.get("env") or {}).items():
                if (
                    isinstance(v, str)
                    and v.startswith("${")
                    and v.endswith("}")
                ):
                    env_name = v[2:-1]
                    if not os.environ.get(env_name):
                        degraded.append(
                            f"MCP {srv}: 环境变量 {env_name} 未配置，该 MCP 暂不可用"
                            f"（请把凭据补进 .env 后重启进程）"
                        )
        return degraded
