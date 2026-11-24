"""easyprd 服务装配 — watcher + 决策轮询 + 会议扫描（随 bot 启停）。

为什么单独一层：watcher 是「来消息才动」，决策轮询/会议扫描是「到点自己查」，
两者的生命周期都挂在 bot 上。放 main.py 会让启动段继续膨胀（已经 400+ 行），
且这些循环与飞书接入无关，不该由 main 关心细节。

**启动门禁**（2026-11-16 起）：以前要求 `enabled` 且 `watch_chats` 非空才起循环，
结果「只想整理会议」也被迫先配一个群。现在三种来源任一成立就起：
群聊监听（watch_chats ∪ story_chat_map 的群）、会议映射（minutes_story_map）、
会议自动扫描（meeting_auto）。三者共用同一个 watcher 与同一套识别/评论/卡片链路。

循环异常一律吞掉并记日志：定时器一旦因异常退出就再也不会回来（用户表现为
「PRD 评论永远没人确认」，比少跑一轮难查得多）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, timedelta
from typing import Any

from app.config import AppConfig
from app.domains.prd_watch.binding import BindingSource
from app.domains.prd_watch.card_actions import CardActionHandler
from app.domains.prd_watch.cr_store import CrStore
from app.domains.prd_watch.decisions import DecisionPoller
from app.domains.prd_watch.kb import (
    COLS as KB_COLS,
)
from app.domains.prd_watch.kb import (
    KnowledgeLedger,
    StoryRecord,
    render_story_card,
)
from app.domains.prd_watch.ledger import Ledger, MemoryLedger
from app.domains.prd_watch.local_adapters import (
    LocalCrAdapter,
    LocalKnowledgeAdapter,
    new_local_ledger,
)
from app.domains.prd_watch.meegle import MeegleClient, MeegleError
from app.domains.prd_watch.meeting import MeetingMaterial, from_link, search_minutes
from app.domains.prd_watch.minutes import mark_seen, poll_minutes
from app.domains.prd_watch.ui import UiMatcher, build_ui_frame_text, load_frames
from app.domains.prd_watch.watcher import PrdWatcher

logger = logging.getLogger("prd_watch.service")


class PrdWatchService:
    """总装：开关判定、旁路入口暴露、决策轮询、会议扫描、需求分析。"""

    def __init__(self, config: AppConfig, *,
                 send_card: Any = None,
                 record_hook: Any = None,
                 member_resolver: Any = None) -> None:
        self._config = config
        self._pc = config.prd_watch
        # 发消息能力（main 注入 _prd_send_card）。分析卡片要发回提问的会话，
        # 而 handler 只拿到 service —— 出口留在这里，handler 不直接碰 channel。
        self._send_card = send_card
        # 本地台账优先：三张表（绑定 / CR / 知识地图）是本能力自己的运行状态，
        # 落在本机 SQLite 里零配置、零凭证；配了 base 坐标也不会被读（避免两个事实源）。
        self.local: Any = (
            new_local_ledger(self._pc.local_ledger_path)
            if self._pc.local_ledger else None
        )
        # 本地台账就绪时，base 版一律退成「不落库」的替身；两者接口不同，
        # 下面这些分支是唯一的适配点（调用方只认 base 版的接口）。
        base_ledger_on = self._pc.ledger_ready and self.local is None
        self._ledger: Ledger = (
            Ledger(self._pc) if base_ledger_on else MemoryLedger(self._pc)
        )
        # CR 状态机台账（评论 id / 决策状态 / 分带）。本地台账就绪时由 adapter 接管。
        self.cr_store: Any = (
            LocalCrAdapter(self.local) if self.local is not None and self.local.ready
            else (CrStore(self._pc.cr_base_token, self._pc.cr_table)
                  if self._pc.cr_ready else CrStore("", ""))
        )
        # 需求知识台账（req-knowledge-ledger 内化）。未配且无本地台账时为 None，
        # 相关工具明说没配。
        self.kb: Any = (
            LocalKnowledgeAdapter(self.local) if self.local is not None and self.local.ready
            else (KnowledgeLedger(self._pc.kb_base_token, {
                "story": self._pc.kb_story_table,
                "meeting": self._pc.kb_meeting_table,
                "change": self._pc.kb_change_table,
            }) if self._pc.kb_ready else None)
        )
        # Story↔PRD↔参与人 从飞书项目读；多维表格退为「变更记录落库 + 老路径兜底」。
        # 装配失败（无凭证/网络）不阻断启动：绑定自动回落台账，并在 snapshot 里说清楚。
        self.binding: BindingSource | None = self._build_binding()
        self.watcher = PrdWatcher(self._pc, config.router,
                                  ledger=self._ledger,
                                  send_card=send_card,
                                  record_hook=record_hook,
                                  member_resolver=member_resolver,
                                  binding=self.binding,
                                  cr_store=self.cr_store,
                                  local=self.local)
        self.poller = DecisionPoller(self._pc, self._ledger, notify=send_card,
                                     binding=self.binding, cr_store=self.cr_store)
        # 群卡片按钮（采纳/修正/忽略）—— 与评论回复那条轮询路径并存，两种入口都认
        self.card_actions = CardActionHandler(self._pc, self._ledger,
                                              cr_store=self.cr_store)
        self._task: asyncio.Task[None] | None = None
        # 「绑定与变更记录能不能跨重启」只看本地台账是否就绪；base 坐标不再是判据
        self.degraded = not (
            (self.local is not None and self.local.ready)
            or self._pc.ledger_ready)
        self._meeting_scan_at = 0.0

    def _build_binding(self) -> BindingSource | None:
        if not self._pc.meegle_simple_name or not self._pc.story_chat_map:
            return None
        try:
            client: MeegleClient | None = MeegleClient(self._pc.meegle_simple_name)
        except MeegleError as e:
            logger.warning("easyprd Meegle 未装配（回落台账绑定）: %s", e)
            client = None
        if client is None:
            return None
        return BindingSource(client=client, story_chat_map=self._pc.story_chat_map,
                             prd_field=self._pc.prd_field,
                             task_field=self._pc.task_doc_field)

    @property
    def active(self) -> bool:
        """任一来源成立即可工作（不再要求必须配群）。"""
        if not self._pc.enabled:
            return False
        return bool(self.watcher.watch_chats()
                    or self._pc.minutes_story_map
                    or self._pc.meeting_auto)

    @property
    def ui_ready(self) -> bool:
        """有 figma token（配置/环境/mcp-gateway）即视为可用。

        「有 token」不等于「token 有效」——Figma personal token 会过期，
        有效性只在真调 REST 时才知道，过期原因会原样进卡片的 PRD↔UI 段，
        不在这里假装 ready 就等于能比对。
        """
        return bool(self._pc.figma_token or _figma_env_token())

    async def start(self) -> None:
        if not self.active:
            # 手动入口不需要群绑定（analyze_story 吃 Story ID/链接，analyze_requirement
            # 吃台账），但 `main._build_prd_watch` 只在 active 时把 watcher 交给
            # Handler——于是「不配群」会连带把工具一起关掉。这里起一个空转守卫：
            # 保持 poll 任务活着，watch_chats 一填就退出、交回真正的监听循环。
            logger.info("easyprd 手动模式：无授权群/会议来源 → 只开手动入口，"
                        "不开群监听（enabled=%s）", self._pc.enabled)
            self._task = asyncio.create_task(self._idle_guard(), name="prd_watch_idle")
            return
        if self.binding is not None:
            # 首次扫描放启动里：Story 列表与 PRD 链接解析较慢，但失败必须当场可见，
            # 不能等第一条变更来了才发现「绑定根本没建立」
            await self.binding.scan()
            snap = self.binding.snapshot()
            logger.info("easyprd 绑定：Meegle 空间 %s 映射 %d 个 Story → 生效 %d 个群%s",
                        self._pc.meegle_simple_name, snap["mapped"], snap["bound_chats"],
                        ("，异常 " + str(len(snap["errors"])) if snap["errors"] else ""))
            for err in snap["errors"][:3]:
                logger.warning("easyprd 绑定: %s", err)
        else:
            logger.info("easyprd 绑定：未配 meegle_simple_name/story_chat_map → 用多维表格绑定")
        if self.degraded:
            logger.warning("easyprd 旧台账坐标未配全 → 绑定与变更记录只在内存里，"
                           "重启即丢；配好 [projects.prd_watch].ledger_* 再开真链路")
        if not self.cr_store.ready:
            logger.warning("easyprd CR 表未配（cr_base_token/cr_table）→ "
                           "决策状态只在内存里，重启后 pending 会丢")
        self._task = asyncio.create_task(self._loop(), name="prd_watch_poll")
        logger.info("easyprd 启动：授权群 %d 个，窗口 %.0fs，dry_run=%s，轮询 %ds，"
                    "会议映射 %d 场，自动扫描=%s",
                    len(self.watcher.watch_chats()), self._pc.window_sec,
                    self._pc.dry_run, self._pc.poll_replies_sec,
                    len(self._pc.minutes_story_map), self._pc.meeting_auto)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _idle_guard(self) -> None:
        """空转守卫：没有监听来源时只保持 poll 任务存活，一旦配了群就让位。

        为什么需要它而不是直接 return：Handler 是否拿到 watcher 由 `active` 决定，
        而 `active` 是**属性**、随配置实时变化。守卫让 `stop()` 与状态面板看到的
        仍然是「任务在跑」，同时不必为「手动模式」再造一条启动分支。
        """
        while True:
            try:
                await asyncio.sleep(min(max(30.0, self._pc.window_sec), 300.0))
                if self.active:
                    logger.info("easyprd 监听源已配置 → 切入群/会议监听")
                    self._task = asyncio.create_task(self._loop(), name="prd_watch_poll")
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[prd_watch] idle guard error")
                await asyncio.sleep(60.0)

    async def _loop(self) -> None:
        interval = max(30, self._pc.poll_replies_sec)
        minutes_at = 0.0
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._pc.dry_run:
                    out = await self.poller.poll_once()
                    if out.applied or out.reminded or out.errors:
                        logger.info("[prd_watch] 决策轮询: %s", out.summary())
                        for err in out.errors[:3]:
                            logger.warning("[prd_watch] %s", err)
                # 妙记通道按自己的节奏跑（会议量远小于群消息量）
                if (self._pc.minutes_story_map
                        and time.time() - minutes_at >= self._pc.minutes_poll_sec):
                    minutes_at = time.time()
                    stats = await poll_minutes(self._pc, self._ledger, self.watcher,
                                               binding=self.binding)
                    if stats.get("new") or stats.get("errors"):
                        logger.info("[prd_watch] 妙记轮询: %s", stats)
                # 会议自动扫描：按需求名关键词搜妙记（默认关，见 config.toml 注释）
                if (self._pc.meeting_auto
                        and time.time() - self._meeting_scan_at >= self._pc.meeting_scan_sec):
                    self._meeting_scan_at = time.time()
                    scanned = await self.scan_meetings()
                    if scanned.get("ingested") or scanned.get("errors"):
                        logger.info("[prd_watch] 会议扫描: %s", scanned)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 绝不因一次异常退出循环（退出＝决策闭环静默停摆）
                logger.exception("[prd_watch] 轮询异常，%ds 后重试", interval)

    # ── 会议：贴链接整理 / 自动扫描 ────────────────────────────────
    async def ingest_meeting(self, chat_id: str, story_id: str, link: str) -> MeetingMaterial:
        """读一条会议链接 → 素材（只读，不写库、不识别）。"""
        return await from_link(link,
                               minutes_base_url=self._pc.minutes_base_url,
                               notes_base_url=self._pc.notes_base_url)

    async def run_meeting(self, chat_id: str, story_id: str,
                          link: str = "", material: MeetingMaterial | None = None
                          ) -> tuple[MeetingMaterial, Any]:
        """会议素材 → 识别 → 评论/卡片 → 知识台账。返回 (素材, WatchResult)。

        `link` 与 `material` 二选一：扫描路径已经读过素材，不必重复读一次
        （lark-cli 每次都是新子进程，重复读等于白等几秒）。
        同一场会的去重键是素材 token，**在读完素材之后**才能登记——
        先登记会在读失败时把这场会永久跳过（用户表现为「再贴一次就没反应」）。
        """
        mat = material or await self.ingest_meeting(chat_id, story_id, link)
        if not mat.ok:
            return mat, None
        token = mat.minute_token or mat.note_doc_token or link
        if not mark_seen(token):
            logger.info("[prd_watch] 会议 %s 已处理过，跳过", token[:16])
            return mat, None
        result = await self.watcher.handle_material(
            chat_id=chat_id, story_id=story_id, text=mat.text,
            source_label=mat.source_label, source_url=mat.source_url)
        # 识别跑完再落知识台账：评论写不进去时不该在台账里留下「已整理」的假象
        await self._record_meeting(mat, story_id, result)
        return mat, result

    async def _record_meeting(self, mat: MeetingMaterial, story_id: str,
                              result: Any) -> None:
        """把这场会写进需求知识台账（会议纪要 + 需求主表最新结论）。

        dry_run 与「台账未配」都直接跳过：这两种情况下不该产生任何持久化副作用，
        否则用户会以为已经在用知识地图，实际看到的是空表。
        """
        if self.kb is None or self._pc.dry_run or result is None:
            return
        story = await self.kb.find_story(story_id) if story_id else None
        try:
            await self.kb.add_meeting(mat, story, result)
        except Exception as e:
            logger.warning("[prd_watch] 会议落知识台账失败: %s", str(e)[:160])

    async def send_card(self, chat_id: str, card: Any) -> None:
        """发一张卡片到指定会话（未注入发送能力时静默，调用方会退回文字回执）。"""
        if self._send_card is None or not chat_id or card is None:
            return
        await self._send_card(chat_id, card)

    async def scan_meetings(self) -> dict[str, Any]:
        """按需求名关键词搜妙记并整理（自动扫描）。

        归属判据刻意保守：**只有关键词唯一命中一条 Story 才自动整理**。多候选时
        列出候选并跳过——靠会议标题猜需求是明令禁止的猜测行为（写错 PRD 比漏报贵）。
        """
        stats: dict[str, Any] = {"checked": 0, "ingested": 0, "skipped": 0, "errors": []}
        if self.kb is None:
            stats["errors"].append("知识台账未配（kb_base_token），无法按需求名搜索")
            return stats
        stories = await self.kb.stories()
        if not stories:
            stats["errors"].append("知识台账里没有需求记录，无从生成搜索词")
            return stats
        start = (date.today() - timedelta(days=max(1, self._pc.meeting_scan_days))).isoformat()
        today = date.today().isoformat()
        budget = max(1, self._pc.meeting_scan_limit)
        for story in stories:
            if budget <= 0:
                break
            chat = _bound_chat(self.binding, story.story_id)
            if not chat:
                continue                    # 没绑定群 = 没有授权边界，不碰
            stats["checked"] += 1
            # 关键词逐个试、合并去重：单一名词搜不到是常态（实测「钱包管理&钱包绑定」
            # 0 命中、「钱包」3 命中）。命中数在这里汇总，不参与归属判断。
            hits: list[dict[str, str]] = []
            seen_tok: set[str] = set()
            for query in _story_keywords(story):
                for hit in await search_minutes(query, start=start, end=today, limit=5):
                    token = hit.get("token") or ""
                    if token and token not in seen_tok:
                        seen_tok.add(token)
                        hits.append(hit)
                if len(hits) >= 8:
                    break
            unique = _unique_story_for(story, hits, stories)
            if unique is not True:
                if hits:
                    stats["skipped"] += 1
                    logger.info("[prd_watch] 会议候选 %d 场命中关键词「%s」，"
                                "但归属不唯一（%s），跳过", len(hits), query, unique)
                continue
            had = stats["ingested"]
            for hit in hits[:2]:
                token = hit.get("token") or ""
                if not token or _seen_token(token):
                    continue
                chat = _bound_chat(self.binding, story.story_id)
                # 去重登记在 run_meeting 里（读完素材才登记），这里只管读
                mat, result = await self.run_meeting(chat, story.story_id,
                                                     material=await self.ingest_meeting(
                                                         chat, story.story_id, token))
                if not mat.ok:
                    stats["errors"].extend(mat.errors[:1])
                    continue
                if result is not None:
                    stats["ingested"] += 1
                    if stats["ingested"] - had >= 2:
                        break
            budget -= max(0, stats["ingested"] - had)
            if budget <= 0:
                break
        return stats

    # ── 需求分析（同版本/相关联的需求）──────────────────────────────
    async def analyze_story(self, story_ref: str) -> tuple[str, Any, dict[str, Any] | None]:
        """给一条 Story 链接/ID 做分析 → (人话回执, StoryDigest, 卡片 dict)。

        分析内容见 `analysis.py`；这里只负责装配依赖（Meegle 客户端、妙记搜索）
        并把失败翻译成人话。卡片与回执同时返回：群里发卡片、模型读回执，
        两边说的是同一批结论。
        """
        from app.domains.prd_watch.analysis import build_digest, parse_story_ref
        from app.domains.prd_watch.analysis_card import build_story_card
        from app.domains.prd_watch.meegle import MeegleClient, MeegleError
        from app.domains.prd_watch.meeting import search_minutes

        if not parse_story_ref(story_ref):
            return ("⚠️ 没解析出 Story ID：请给 `https://project.feishu.cn/<空间>/story/detail/<id>` "
                    "形式的链接或纯数字 ID。"), None, None
        if not self._pc.meegle_simple_name:
            return ("⚠️ 未配置 `[projects.prd_watch].meegle_simple_name`（飞书项目空间名），"
                    "读不到 Story。"), None, None
        try:
            client = MeegleClient(self._pc.meegle_simple_name)
        except MeegleError as e:
            return (f"⚠️ 飞书项目凭证不可用：{str(e)[:160]}"), None, None

        async def _search(query: str) -> list[dict[str, str]]:
            return await search_minutes(query, start=_days_ago(90), limit=5)

        # UI 对账：有 Figma token 且需求有设计稿时顺手做（拿不到就写进 ui_errors，
        # 卡片上如实显示「未比对 + 原因」，不假装比过）
        from app.domains.prd_watch.ui import UiMatcher

        matcher = UiMatcher(self._config.router, self._pc)
        digest = await build_digest(story_ref=story_ref, meegle=client,
                                    search_meetings=_search, ui_matcher=matcher,
                                    figma_token=self._pc.figma_token)
        if not digest.story_id:
            return ("⚠️ " + "；".join(digest.errors[:2] or ["Story 读取失败"])), digest, None

        card = None
        try:
            # 先登记再渲染：卡片按钮回调只带回 AN- 编号，正文/块 id 全靠登记簿，
            # 顺序反了就会出现「卡片上能点、点了说找不到」。
            self.card_actions.feedback.register(digest)
            card = build_story_card(digest)
        except Exception as e:
            logger.warning("[prd_watch] 分析卡片渲染失败: %s", str(e)[:140])

        lines = [f"📋 {digest.title or digest.story_id}（#{digest.story_id}）",
                 f"状态：{digest.status or '未知'}"
                 + (f"｜节点：{digest.node}" if digest.node else ""),
                 f"PRD：{'有' if digest.prd_token else '无'}"
                 f"｜设计稿：{'有' if digest.design_url else '无'}"
                 f"｜相关会议：{len(digest.meetings)} 场",
                 f"检查项：{len(digest.findings)} 条"
                 + (f"（含 UI 差异 {sum(1 for f in digest.findings if f.kind == 'UI差异')} 条）"
                    if any(f.kind == "UI差异" for f in digest.findings) else "")]
        for i, f in enumerate(digest.findings, start=1):
            lines.append(f"  {i}. [{f.kind}] {f.title}"
                         + (f"（{f.section}）" if f.section else ""))
        if digest.related:
            lines.append(f"同族需求：{len(digest.related)} 条（{digest.related_basis}），"
                         "仅列候选")
        if digest.errors:
            lines.append("提示：" + "；".join(digest.errors[:2]))
        return "\n".join(lines), digest, card

    async def analyze_requirement(self, key: str) -> tuple[str, StoryRecord | None]:
        """需求知识地图（台账里的结论/变更历史）——与 `analyze_story` 分工不同：
        这个答「讨论到哪了」，那个答「这个需求本身有什么问题」。"""
        if self.kb is None:
            return ("⚠️ 需求知识台账未配置（`[projects.prd_watch].kb_base_token`）。"
                    "配好后我才能按 Story 汇总 PRD、会议与变更历史。"), None
        story = await self.kb.find_story(key)
        if story is None:
            return (f"⚠️ 台账里没找到「{key}」对应的需求记录。"
                    "可能是 Story 还没登记，或关键词命中多条（我不替你猜是哪条）。"), None
        changes = await self.kb.changes_for(story)
        meetings = await self.kb.meetings_for(story)
        text = render_story_card(story, changes, meetings)
        kin = await self._kin_stories(story)
        if kin:
            lines = ["", "【同族需求】"]
            for rel, rows in kin:
                lines.append(f"{rel}：" + "；".join(
                    f"{s.story_name or s.story_id}（#{s.story_id}"
                    + (f"｜{s.status}" if s.status else "") + "）" for s in rows[:6]))
            text += "\n" + "\n".join(lines)
        return text, story

    async def _kin_stories(self, story: StoryRecord) -> list[tuple[str, list[StoryRecord]]]:
        """父版本 / 相关需求（自关联 link 反查）。拿不到就返回空，不猜。"""
        if self.kb is None:
            return []
        out: list[tuple[str, list[StoryRecord]]] = []
        for label, ids in (("父版本", story.parent_ids), ("相关需求", story.related_ids)):
            rows: list[StoryRecord] = []
            for rid in ids:
                s = await self.kb.story_by_record_id(rid)
                if s is not None:
                    rows.append(s)
            if rows:
                out.append((label, rows))
        return out

    # ── 本地台账入口（绑定 / 分组 / 导出）────────────────────────────
    async def bind_story(self, story_ref: str, chat_id: str) -> tuple[Any, str]:
        """把一条 Story 登记进本地台账并绑定到群 → (LocalBinding, 人话回执)。

        为什么要单独一个入口：监听旁路与知识地图都要求「这个群盯的是哪条 Story」，
        而 Meegle 上没有「需求在哪个群」这个字段（设计稿明令禁止靠群名猜）。
        这里做的是**人明确指定**：解析 Story → 拿到 PRD 文档 token → 落本地台账。

        PRD token 的解析是全链路最容易失败的一步（字段里可能是摘要 / git 地址 /
        无权访问的文档），失败时**仍然登记**，但如实说「PRD 还没解析到」——
        否则用户会以为已经在收变更建议。
        """
        from app.domains.prd_watch.analysis import parse_story_ref
        from app.domains.prd_watch.local_ledger import LocalBinding

        if self.local is None or not self.local.ready:
            return None, "本地台账未开启"
        story_id = parse_story_ref(story_ref)
        if not story_id:
            return None, ("没解析出 Story ID：请给 "
                          "`https://project.feishu.cn/<空间>/story/detail/<id>` 或纯数字 ID")
        if not self._pc.meegle_simple_name:
            return None, "未配置 `[projects.prd_watch].meegle_simple_name`，读不到 Story"

        title = ""
        prd_token = ""
        prd_url = ""
        error = ""
        try:
            client = MeegleClient(self._pc.meegle_simple_name)
            story = await asyncio.to_thread(
                client.get_story, story_id,
                self._pc.prd_field, self._pc.task_doc_field)
            title = story.title
            prd_url = getattr(story, "prd_url", "") or ""
            # 复用与监听链路同一个解析器：wiki 链接 → obj_token 的规则只该有一份
            resolver = BindingSource(client=client, prd_field=self._pc.prd_field,
                                     task_field=self._pc.task_doc_field)
            prd_token = await resolver.resolve_prd(story)
        except Exception as e:
            error = f"Meegle 读取失败：{str(e)[:100]}"

        binding = LocalBinding(
            story_id=story_id, chat_id=chat_id, story_title=title,
            prd_url=prd_url, prd_token=prd_token, source="local")
        ok = await asyncio.to_thread(self.local.upsert_binding, binding)
        if not ok:
            return None, "本地台账写入失败（看 bot 日志）"

        head = f"✅ 已登记 #{story_id}" + (f"「{title}」" if title else "")
        head += f" → 绑定到 {chat_id[:12]}…"
        if prd_token:
            head += f"\nPRD 已解析（token {prd_token[:8]}…）"
        else:
            head += ("\n⚠️ PRD 文档还没解析到（字段可能是摘要 / git 地址 / 无权访问）。"
                     "这时只记录需求本身，不会产生 PRD 变更建议。")
        if error:
            head += f"\n⚠️ {error}"
        return binding, head


    async def group_stories(self, group_name: str, story_refs: list[str], *,
                            chat_id: str = "", note: str = "") -> str:
        """把多条 Story 归成一个版本组（写本地台账 + 需求主表的关系字段）。"""
        from app.domains.prd_watch.analysis import parse_story_ref
        from app.domains.prd_watch.local_ledger import LocalStory

        if self.local is None or not self.local.ready:
            return "本地台账未开启"
        ids = [i for i in (parse_story_ref(r) for r in story_refs) if i]
        if len(ids) < 2:
            return "⚠️ 至少要有 2 条能解析出 Story ID 的需求"
        lines = [f"🗂 版本组「{group_name}」"]
        for sid in ids:
            existing = await asyncio.to_thread(self.local.get_story, sid)
            story = LocalStory(**existing.__dict__) if existing else LocalStory(story_id=sid)
            # 组内互相关联（但不含自己）：关系字段只写别的成员
            story.related_ids = sorted({s for s in ids if s != sid})
            title = story.story_name
            if chat_id:
                bound = await self.bind_story(sid, chat_id)
                b = bound[0]
                if b is not None and not title:
                    title = b.story_title
                    story.story_name = title
                    story.prd_url = b.prd_url
            if note and not story.latest_summary:
                story.latest_summary = f"版本组：{group_name}（{note}）"
            ok = await asyncio.to_thread(self.local.upsert_story, story)
            lines.append(f"  · #{sid} {title or '（名称待取）'}"
                         + ("" if ok else " ⚠️ 写入失败"))
        lines.append(f"共 {len(ids)} 条；成员之间的「相关需求」已互相指向。"
                     "（只建关系，不改 Story 本身）")
        return "\n".join(lines)

    # ── PRD ↔ UI 对账 ─────────────────────────────────────────────
    async def review_ui(self, chat_id: str, story_id: str, figma_link: str
                        ) -> tuple[str, Any]:
        """对一份设计稿做文本层对账 → 差异走同一条评论/卡片/决策链路。

        返回 (人话结果, WatchResult)。差异为 0 时也要说清是「确实一致」还是
        「素材取不到」——后者说明没比，不该让用户以为已经比过。
        """
        if not self.ui_ready:
            return ("⚠️ 未配置 Figma token（`.env` 的 `FIGMA_API_KEY`），读不到设计稿。"), None
        frames, errors = await load_frames(figma_link, self._pc.figma_token)
        if not frames:
            return ("⚠️ 设计稿没有取到可比较的文本层素材：" + "；".join(errors[:2])), None
        bindings = await self._bindings_for(chat_id, story_id)
        if not bindings:
            return ("⚠️ 这个群/这条 Story 没有可用的 PRD 绑定（Meegle 与台账都没对上），"
                    "没有可比对的文档。"), None
        matcher = UiMatcher(self._config.router, self._pc)
        result = await self.watcher.run_with_matcher(
            bindings, build_ui_frame_text(frames),
            source=f"PRD↔UI 对账（{len(frames)} 个画板）",
            source_url=figma_link, matcher=matcher, kind="UI对账")
        return "", result

    async def _bindings_for(self, chat_id: str, story_id: str) -> list[Any]:
        """绑定解析：Story 直取优先，其次该群全部绑定。"""
        rows: list[Any] = []
        if story_id and self.binding is not None:
            b = await self.binding.binding_for_story(story_id)
            if b is not None:
                rows.append(b)
        if not rows and chat_id:
            if self.binding is not None:
                rows = list(await self.binding.bindings_for_chat(chat_id))
            if not rows:
                rows = list(await self._ledger.bindings_for_chat(chat_id))
        return [b for b in rows if getattr(b, "usable", False)]

    def snapshot(self) -> dict[str, Any]:
        snap = dict(self.watcher.snapshot())
        snap["degraded_ledger"] = self.degraded
        snap["binding"] = (self.binding.snapshot() if self.binding is not None
                           else {"source": "ledger"})
        snap["poll_loop_running"] = self._task is not None and not self._task.done()
        snap["cr_store"] = "ready" if self.cr_store.ready else "memory(重启即丢)"
        snap["kb"] = "ready" if self.kb is not None else "未配置"
        snap["ui"] = ("已配置（有效性在调用时校验）" if self.ui_ready
                      else "未配置 Figma token")
        snap["meeting_auto"] = self._pc.meeting_auto
        snap["minutes_mapped"] = len(self._pc.minutes_story_map)
        snap["analysis_feedback"] = self.card_actions.feedback.summary_line()
        return snap


def _days_ago(days: int) -> str:
    from datetime import timedelta as _td

    return (date.today() - _td(days=days)).isoformat()


def _figma_env_token() -> str:
    from app.domains.prd_watch.ui import resolve_token

    return resolve_token()


def _seen_token(token: str) -> bool:
    """该会议 token 是否已处理过（只读判断，登记在 run_meeting 里做）。"""
    from app.domains.prd_watch.minutes import seen_tokens

    return token in seen_tokens()


def _bound_chat(binding: BindingSource | None, story_id: str) -> str:
    """Story → 绑定的群（授权边界）。没配映射就返回空串，调用方据此跳过。"""
    if binding is None or not story_id:
        return ""
    return str(binding.story_chat_map.get(str(story_id)) or "")


def _story_keyword(story: StoryRecord) -> str:
    """需求名 → 搜索关键词：取括号之后**最长的产品名词片段**。

    实测坑：`【webportal】VA - 钱包管理&钱包绑定` 按「第一个破折号前」会切成 `VA`
    两个字母——搜出来是所有 VA 相关的会，等于没筛。按空格/破折号切片段取最长的一段，
    才会落到 `钱包管理&钱包绑定` 这种真正能定位会议的词。
    括号前缀（【webportal】）是产品线标签，检索价值低于名称本身，先剥掉。
    """
    name = story.story_name or ""
    for sep in ("】", "]"):
        if sep in name:
            name = name.split(sep, 1)[-1]
    name = name.strip()
    parts = [seg.strip(" -—（(）)：:") for seg in re.split(r"\s+|[-—]", name)]
    parts = [seg for seg in parts if len(seg) >= 2]
    if not parts:
        return name.strip()[:24]
    return max(parts, key=len)[:24]


def _story_keywords(story: StoryRecord) -> list[str]:
    """需求名 → 一组搜索词（主词优先，其次名称里的实词）。

    为什么不止一个：妙记搜索是**精确关键词**匹配，整串产品名常 0 命中
    （实测「钱包管理&钱包绑定」无结果，而「钱包」有 3 场）。多词逐个搜再合并，
    命中集合只用于「有没有相关会」，归属仍由 `_unique_story_for` 单独判。
    """
    name = story.story_name or ""
    for sep in ("】", "]"):
        if sep in name:
            name = name.split(sep, 1)[-1]
    name = name.strip()
    words: list[str] = []
    primary = _story_keyword(story)
    if len(primary) >= 2:
        words.append(primary)
    for seg in re.split(r"[\s&、,/（）()\[\]【】]+", name):
        seg = seg.strip(" -—：:。") 
        if len(seg) >= 2 and seg not in words:
            words.append(seg)
    return words[:4]


def _unique_story_for(story: StoryRecord, hits: list[dict[str, str]],
                      stories: list[StoryRecord]) -> bool | str:
    """关键词是否**只**指向这一条需求。返回 True / 其他需求的名称（说明不唯一）。

    判据只看需求名是否包含同一个关键词——会议标题命中的场次多少不参与判断，
    因为「这场会聊了什么」本来就无法从标题可靠推断。
    """
    query = _story_keyword(story)
    clashes = [s for s in stories
               if s.story_id != story.story_id and query and query in (s.story_name or "")]
    if clashes:
        return "、".join(s.story_name or s.story_id for s in clashes[:3])
    return True


__all__ = ["KB_COLS", "PrdWatchService"]
