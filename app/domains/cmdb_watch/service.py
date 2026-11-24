"""CMDB 工单提醒服务装配 — watcher 轮询循环（随 bot 启停）。

为什么单独一层：与 prd_watch 同构的分工——watcher 只懂「拉状态、判事件、发卡」，
循环的启停、首轮回溯、异常吞掉都归这里。main.py 只调 start/stop/snapshot。

循环异常一律吞掉并记日志：定时器因一次异常退出就再也不会回来，用户表现为
「提醒从此不响了」，比多跑一轮难查得多。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import AppConfig
from app.domains.cmdb_watch.client import ENV_LABELS, ENVS, session_valid
from app.domains.cmdb_watch.watcher import CmdbWatcher

logger = logging.getLogger("cmdb_watch.service")


class CmdbWatchService:
    """总装：开关判定、首轮回溯、轮询循环。"""

    def __init__(self, config: AppConfig, *, send_card: Any = None,
                 record_hook: Any = None, watcher: CmdbWatcher | None = None) -> None:
        self._cfg = config.cmdb_watch
        self.watcher = watcher or CmdbWatcher(self._cfg, send_card=send_card,
                                              record_hook=record_hook)
        self._task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.watcher.active

    async def start(self) -> None:
        if not self.active:
            logger.info("CMDB 工单提醒未启动：enabled=false")
            return
        # 首轮回溯：首次启动（没有状态文件）时把最近 N 张工单当作基线扫一遍，
        # 让「已经在跑的任务」进跟踪但**不补发提醒**（watcher 的首次见逻辑保证）。
        if self._cfg.initial_scan and not self.watcher.store.orders \
                and not any(self.watcher.store.scanned_max.values()):
            await self._seed(int(self._cfg.initial_scan))
        # 登录态体检：过期时列表入口会静默降级成 id 探测（发现慢一轮），
        # 这属于「功能还在但不准时」的那类问题，启动时就说清楚
        if self._cfg.refresh_session_on_start:
            await self._check_session()
        self._task = asyncio.create_task(self._loop(), name="cmdb_watch_poll")
        logger.info("CMDB 工单提醒启动：间隔 %ds，全量校准 %ds，订阅方式=对话自助",
                    self._cfg.interval_sec, self._cfg.refresh_sec)

    async def _check_session(self) -> None:
        client = self.watcher._client
        ok = await asyncio.to_thread(session_valid, client.session_id, self._cfg.base_url)
        if ok:
            logger.info("CMDB 登录态有效，新工单走列表接口发现（一次拿全最近 id）")
            return
        # 登录态过期不代表要人工介入，但**也不代表该后台自动登录**：CMDB 单会话登录，
        # 后台一登就把同账号的浏览器会话顶掉（这正是「浏览器老是掉线」的根因）。
        # 所以默认只告警、降级为 id 探测，要不要登由 CMDB_ALLOW_RELOGIN 决定。
        renewed = await asyncio.to_thread(client.renew_session)
        if renewed:
            logger.info("CMDB 登录态已续期，列表发现走快路径")
            return
        logger.warning(
            "CMDB 登录态无效且未自动登录 → 新工单发现降级为 order_id 顺序探测（慢一轮）。"
            "根治办法：申请一个独立服务账号写入 ~/.cmdb-bot.conf（CMDB_USER/CMDB_PASS）；"
            "临时办法：把浏览器里现成的 sessionid 同步到 ~/.cmdb-session")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _seed(self, count: int) -> None:
        """把回溯范围之外的历史 id 标记为「已看过」，只回溯 count 张。

        为什么不是直接把水位线减 count：列表接口回的是最近一批，
        id 可能不连续（实测 5760~5779 之间有空洞）。直接探 count 个 id 更贴近
        「最近 count 张」的语义，代价是 count 次 HTTP（每张 0.05s 量级）。

        两个环境各回溯各的：UAT 列表很短（实测 ~190 张），共用一套水位会让其中
        一侧看起来「全都没看过」。
        """
        for env in ENVS:
            try:
                ids = await asyncio.to_thread(
                    self.watcher._client.list_recent_order_ids, env)
            except Exception as e:
                logger.info("CMDB 回溯（%s）：列表入口不可用（%s），从当前 id 往后看",
                            ENV_LABELS.get(env, env), str(e)[:80])
                continue
            if not ids:
                continue
            latest = max(ids)
            floor = max(0, latest - count)
            self.watcher.store.scanned_max[env] = floor
            logger.info("CMDB 回溯（%s）：列表最新 #%s，从 #%s 起扫（回溯 %d 张）",
                        ENV_LABELS.get(env, env), latest, floor + 1, count)

    async def _loop(self) -> None:
        interval = max(60, int(self._cfg.interval_sec))
        while True:
            try:
                stats = await self.watcher.scan_once()
                if stats.cards_sent or stats.errors:
                    logger.info("[cmdb_watch] %s", stats.summary())
                    for err in stats.errors[:3]:
                        logger.warning("[cmdb_watch] %s", err)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 绝不因一次异常退出循环（退出＝提醒静默停摆）
                logger.exception("[cmdb_watch] 轮询异常，%ds 后重试", interval)
            await asyncio.sleep(interval)

    def snapshot(self) -> dict[str, Any]:
        snap = self.watcher.snapshot()
        snap["loop_running"] = self._task is not None and not self._task.done()
        snap["interval_sec"] = self._cfg.interval_sec
        snap["enabled"] = self._cfg.enabled
        return snap

    async def send_test_card(self) -> bool:
        """自检入口（dashboard / 飞书「发个测试卡片」）：发给自己私聊。"""
        return await self.watcher.send_test_card()
