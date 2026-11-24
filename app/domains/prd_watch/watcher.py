"""easyprd 监听编排 — 攒窗口 → 识别 → 锚点 → 写评论 → 发卡片 → 落台账。

调用来源两类（都走同一个 handle_message，避免两套逻辑）：
1. 主链路旁路：白名单群里的非@消息（app/main.py 在 submit 给编排器之前分流）
2. 手动触发：prd-watch-agent 的 Handler（「总结今天这个群的讨论」）

窗口语义：一次讨论往往十几条消息逐条蹦出来。逐条识别既烧模型调用又刷屏，
所以按 chat 攒 window_sec 秒合并成一批（与 gateway/aggregator 的 debounce 同思路，
但这里合并的是「送识别的素材」，不是「用户的连发消息」）。

dry_run 是第一道保险：只算不发，把 candidates/dropped/flagged 三个数落日志，
误报率看清之前不允许真写评论（设计稿 §8.2 灰度第 1 步）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch.anchor import BlockAnchor
from app.domains.prd_watch.cr_store import CrRow, CrStore
from app.domains.prd_watch.dedupe import EmittedIndex, duplicate_of, locator_terms
from app.domains.prd_watch.extract import ChangeExtractor
from app.domains.prd_watch.ledger import COLUMNS, Ledger, StoryBinding
from app.domains.prd_watch.models import ChangePoint, PrdCandidate, WatchResult
from app.domains.prd_watch.render import (
    CardItem,
    StoryMeta,
    build_notice_card,
    comment_primary_link,
    make_cr_id,
    render_comment,
)

logger = logging.getLogger("prd_watch.watcher")

TZ = timezone(timedelta(hours=8))

# fn(chat_id, payload)：payload 给 str 走 markdown 消息，给 dict 走交互卡片。
# 两种都支持是因为决策回执（decisions.py）本来就发纯文本，没必要为了统一改它。
SendCard = Callable[[str, Any], Awaitable[Any]]
# 观测回调：fn(WatchResult, 提取统计)。由 main 注入 task_recorder——domains 层
# 不直接 import 可观测层（依赖方向：编排/gateway → domains，反向只走回调）。
RecordHook = Callable[["WatchResult", dict[str, Any]], None]


@dataclass
class _Window:
    """一个群的待识别缓冲。"""

    lines: list[str] = field(default_factory=list)
    first_ts: float = 0.0
    task: asyncio.TimerHandle | None = None
    senders: dict[str, str] = field(default_factory=dict)  # open_id → 显示名

    @property
    def text(self) -> str:
        return "\n".join(self.lines)[:12000]


class PrdWatcher:
    """旁路监听器。channel 只用来发消息，不反向依赖编排层。"""

    def __init__(
        self,
        config: PrdWatchConfig,
        router: RouterConfig,
        *,
        ledger: Ledger | None = None,
        send_card: SendCard | None = None,
        record_hook: RecordHook | None = None,
        member_resolver: Callable[[str], Awaitable[Any]] | None = None,
        binding: Any = None,
        cr_store: Any = None,
        local: Any = None,
    ):
        self._cfg = config
        self._extractor = ChangeExtractor(router, config)
        self._anchor = BlockAnchor()
        self._ledger = ledger or Ledger(config)
        # 本地 SQLite 台账（配了 local_ledger 时）：绑定解析优先读它。
        # 它与 base 版的 `_ledger` 接口不同，所以只在这一处做分叉，其余流程只认
        # `StoryBinding` 列表——两套存储不该让下游各写一遍分支。
        self._local = local
        # CR 状态机台账：配了坐标就落多维表格，否则内存替身（重启即丢，状态面板会明说）
        self._cr = cr_store or CrStore(config.cr_base_token, config.cr_table)
        self._send_card = send_card
        self._record_hook = record_hook
        # fn(chat_id) → [ChatMember(id, name)]；未注入时 @ 退化为纯文本
        self._member_resolver = member_resolver
        # Story↔PRD↔参与人 的事实源（飞书项目）。没装配就只用多维表格绑定。
        self._binding = binding
        self._windows: dict[str, _Window] = {}
        self._seq_by_day: dict[str, int] = {}   # StoryID+日期 → 当日序号
        self._loop: asyncio.AbstractEventLoop | None = None
        # 观测：调阈值、判误报全靠这几个数（也进 TaskRecord）
        self.metrics: dict[str, int] = {
            "seen": 0, "no_binding": 0, "candidates": 0, "emitted": 0,
            "dropped": 0, "flagged": 0, "unanchored": 0, "errors": 0, "dry_run": 0,
            "from_meegle": 0,
            # 多 PRD 归属相关的两个数：一个是"模型想归给一篇读不到正文的文档"，
            # 一个是"模型给的 story_id 不在候选集里"。这两个是**错位拦截**的唯一证据，
            # 看不见就不知道 prompt 的归属判据到底有没有生效。
            "no_excerpt": 0, "unattributed": 0,
            # 归一/去重掉的条数（批内近义合并 + 跨轮重复）
            "duplicates": 0,
            # 文档里已有等价评论、没重复写的条数（跨进程判重，见 dedupe.duplicate_of）
            "already_commented": 0,
        }
        # 跨轮去重索引（进程内；重启后重来一次最多多一条正确评论，见 dedupe.py）
        self._dedupe = EmittedIndex()

    # ── 入口 ─────────────────────────────────────────────────────
    def watch_chats(self) -> list[str]:
        """被授权监听（放开非@消息）的群。

        = 配置的 watch_chats ∪ Meegle 绑定里出现的群。两个来源都是管理员显式配的，
        分两处填必然出现「填了一处漏另一处」的静默失效：watch_chats 填了但没绑定
        → 收消息却永不产出建议；绑定填了但没进 watch_chats → 消息根本推不到本进程。
        合并后任一填了即生效，并在启动日志里打出实际群数。
        """
        out = list(self._cfg.watch_chats)
        for chat_id in (self._binding_bound_chats() or []):
            if chat_id not in out:
                out.append(chat_id)
        return out

    def _binding_bound_chats(self) -> list[str]:
        if self._binding is None:
            return []
        return [str(chat_id) for chat_id in self._binding.story_chat_map.values() if chat_id]

    async def handle_message(
        self, *, chat_id: str, sender_id: str, sender_name: str, text: str
    ) -> None:
        """主链路旁路入口（非@消息）。不 return 值：旁路不得影响消息本身。"""
        self.metrics["seen"] += 1
        binding = await self._resolve_binding(chat_id)
        if binding is None:
            # 群在 watch_chats 里但台账没绑定 → 只计数。绑错比不写更糟，不猜。
            self.metrics["no_binding"] += 1
            logger.debug("[prd_watch] %s 无台账绑定，跳过", chat_id[:12])
            return

        body = (text or "").strip()
        if not body:
            return
        window = self._windows.setdefault(chat_id, _Window(first_ts=time.time()))
        stamp = datetime.now(TZ).strftime("%H:%M")
        who = sender_name or sender_id[:8] or "某人"
        window.lines.append(f"[{stamp}] {who}: {body}")
        if sender_id:
            window.senders[sender_id] = who
        self._schedule_flush(chat_id)

    async def _resolve_bindings(self, chat_id: str) -> list[Any]:
        """群 → **全部**可用绑定（一群多 Story / 多篇 PRD 是常态，不是一份）。

        Meegle 优先、台账兜底，但**不是二选一地截断**：Meegle 只解析出 2 条而台账配了
        5 条时（新 Story 还没进列表、PRD 链接没填），剩下的仍要从台账取——否则
        「配了却没被监听」又是一个查不出来的洞。按 story_id 去重合并。
        """
        rows: list[Any] = list(await _call_many(self._binding, chat_id))
        rows.extend(await self._local_bindings(chat_id))
        rows.extend(await _call_many(self._ledger, chat_id))
        if self._binding is not None and any(
                getattr(b, "source", "") == "meegle" for b in rows):
            self.metrics["from_meegle"] += 1
        out: list[Any] = []
        seen: set[str] = set()
        for b in rows:
            if b is not None and b.usable and b.story_id not in seen:
                seen.add(b.story_id)
                out.append(b)
        return out

    async def _local_bindings(self, chat_id: str) -> list[Any]:
        """本地台账里该群的绑定（story_id → 已解析的 PRD token）。

        本地台账存的是 `chat_id + story_id + prd_token`；还没解析出 PRD 的行
        （刚由 `bind_prd_story` 手工登记、尚未跑过 Meegle）不参与，避免把空 token
        带进评论链路。
        """
        if self._local is None or not getattr(self._local, "ready", False):
            return []
        try:
            rows = await asyncio.to_thread(self._local.bindings_for_chat, chat_id)
        except Exception as e:
            logger.warning("[prd_watch] 本地绑定读取失败(%s): %s", chat_id[:12], e)
            return []
        return [r.as_binding() for r in rows if r.usable]

    async def _local_bindings_for_story(self, story_id: str) -> list[Any]:
        """按 Story 直取本地绑定（私聊里没有「群」这个上下文时的入口）。"""
        if not story_id or self._local is None or not getattr(self._local, "ready", False):
            return []
        try:
            row = await asyncio.to_thread(self._local.binding_for_story, story_id)
        except Exception as e:
            logger.warning("[prd_watch] 本地绑定读取失败(story %s): %s", story_id, e)
            return []
        return [row.as_binding()] if row is not None and row.usable else []

    async def _resolve_binding(self, chat_id: str) -> Any:
        """单绑定视图（旧接口，供未升级到多绑定的调用方）。"""
        rows = await self._resolve_bindings(chat_id)
        return rows[0] if rows else None

    async def handle_material(
        self, *, chat_id: str, story_id: str, text: str, source_label: str,
        source_url: str = "",
    ) -> WatchResult:
        """非群消息来源（妙记/智能纪要/手动灌素材）直接进管道，不攒窗。

        与 handle_message 的区别：群消息要攒窗（一次讨论几十条），一场会本来就是
        一份完整素材，攒窗只会让结论迟到 window_sec 秒。

        `story_id` 只是**入口**（会议映射表给的那条），实际候选集是那个群绑定的
        **全部** Story —— 一场「接口对齐」会改 5 篇 PRD，只按入口那篇判会把
        申购的结论评论进持仓那篇文档。
        """
        bindings = await self._bindings_for_material(chat_id, story_id)
        if not bindings:
            result = WatchResult(chat_id=chat_id, story_id=story_id)
            result.errors.append(f"无 Story {story_id} 绑定（Meegle 与台账都没对上）")
            self.metrics["no_binding"] += 1
            return result
        return await self._run(bindings, text, source=source_label,
                               source_url=source_url)

    async def _bindings_for_material(self, chat_id: str, story_id: str) -> list[Any]:
        """会议素材的候选集 = 入口 Story + 该群其余绑定。"""
        rows: list[Any] = []
        if self._binding is not None:
            b = await self._binding.binding_for_story(story_id)
            if b is None:
                b = await self._binding.binding_for_chat(chat_id) if chat_id else None
            if b is not None:
                rows.append(b)
        if not rows:
            b = await self._ledger.binding_for_story(story_id)
            if b is not None:
                rows.append(b)
        # 入口 Story 所在群的其他 Story 一起进候选（同一场会改多篇 PRD）
        chat = chat_id or (rows[0].chat_id if rows else "")
        if chat:
            rows.extend(await self._resolve_bindings(chat))
        else:
            # 纯私聊 + 指了 Story：本地台账按 Story 直取（一条 Story 可能绑在别的群）
            local = await self._local_bindings_for_story(story_id)
            rows.extend(local)
        out, seen = [], set()
        for b in rows:
            if b is not None and b.usable and b.story_id not in seen:
                seen.add(b.story_id)
                out.append(b)
        return out

    async def flush_now(self, chat_id: str) -> WatchResult:
        """立即处理该群缓冲（手动触发入口用，也供单测驱动）。"""
        window = self._windows.pop(chat_id, None)
        if window is not None and window.task is not None:
            window.task.cancel()
        return await self._process(chat_id, window)

    # ── 窗口调度 ─────────────────────────────────────────────────
    def _schedule_flush(self, chat_id: str) -> None:
        window = self._windows.get(chat_id)
        if window is None or window.task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（单测直调），不排定时
        self._loop = loop
        window.task = loop.call_later(
            max(5.0, self._cfg.window_sec), lambda: loop.create_task(self._auto_flush(chat_id))
        )

    async def _auto_flush(self, chat_id: str) -> None:
        try:
            await self.flush_now(chat_id)
        except Exception:
            logger.exception("[prd_watch] 窗口处理失败 chat=%s", chat_id[:12])

    # ── 主流程 ───────────────────────────────────────────────────
    async def _process(self, chat_id: str, window: _Window | None) -> WatchResult:
        if window is None or not window.lines:
            return WatchResult(chat_id=chat_id)
        rows = await self._resolve_bindings(chat_id)
        if not rows:
            result = WatchResult(chat_id=chat_id)
            result.errors.append("无绑定（Meegle 与台账都没对上这个群）")
            self.metrics["no_binding"] += 1
            return result
        # 来源标签用群里的**第一个** Story 标题没意义（一场讨论常跨多个 Story），
        # 所以给"群讨论 + 涉及 Story 数"，具体归属由识别器逐条判定。
        return await self._run(rows, window.text,
                               source=f"群讨论（{len(rows)} 个 Story 在监听）",
                               source_url=_chat_app_link(chat_id),
                               messages=len(window.lines))

    async def run_with_matcher(
        self, bindings: StoryBinding | list[StoryBinding], text: str, *,
        source: str, source_url: str = "", matcher: Any, kind: str = "UI对账",
    ) -> WatchResult:
        """换识别器跑一次主流程（PRD↔UI 对账）。

        暴露成公开方法而不是让 service 直接调 `_run`：下游（去重/锚点/评论/卡片/
        决策闭环）全部复用，只有「拿什么比」不同——这条边界值得一个显式入口，
        否则 service 会开始依赖 watcher 的私有实现细节。
        """
        return await self._run(bindings, text, source=source, source_url=source_url,
                               matcher=matcher, kind=kind)

    async def _run(
        self, bindings: StoryBinding | list[StoryBinding], text: str, *,
        source: str, source_url: str = "", messages: int = 1,
        matcher: Any = None, kind: str = "讨论",
    ) -> WatchResult:
        """识别 → 按 Story 分组 → 锚点 → （干跑 or 出评论）→ 落观测。

        `bindings` 给一个或多个：**一场会/一段讨论可以同时改多篇 PRD**
        。单绑定形态保留兼容（旧调用方与单测仍传单个）。

        候选集只有 1 篇时走单篇路径（不要求模型给 story_id）；≥2 篇时走多篇归属
        路径，模型给的 story_id 不在候选集里的变更**直接丢弃**——写进别人的 PRD
        比漏一条代价高得多。

        `matcher` 给了就换识别器（PRD↔UI 对账用 `UiMatcher`）：差异与变更共用同一套
        去重/锚点/评论/卡片/决策闭环，只是「拿什么比」不同。`kind` 落进 CR 的来源列，
        状态面板据此分开统计两种产出的准确率。
        """
        rows = bindings if isinstance(bindings, list) else [bindings]
        rows = [b for b in rows if b is not None and b.usable]
        primary = rows[0] if rows else None
        started = time.time()
        result = WatchResult(chat_id=primary.chat_id if primary else "",
                             story_id=primary.story_id if primary else "",
                             source_ref=source_url or (primary.chat_id if primary else ""))
        if not rows:
            result.errors.append("无可用绑定")
            return result

        points, stats = await self._extract_multi(text, rows, matcher=matcher)
        result.candidates = int(stats.get("candidates") or 0)
        result.dropped = int(stats.get("dropped") or 0)
        result.flagged = int(stats.get("flagged") or 0)
        result.unattributed = int(stats.get("unattributed") or 0)
        if stats.get("error"):
            result.errors.append(str(stats["error"]))
            self.metrics["errors"] += 1
        self.metrics["candidates"] += result.candidates
        self.metrics["dropped"] += result.dropped
        self.metrics["flagged"] += result.flagged
        self.metrics["unattributed"] += result.unattributed
        stats["duration_ms"] = round((time.time() - started) * 1000, 1)
        stats["messages"] = messages
        stats["window_text"] = text
        stats["window_ts"] = started
        stats["model"] = self._extractor.model
        stats["source"] = source
        stats["stories"] = [b.story_id for b in rows]

        if not points:
            logger.info("[prd_watch] %s 无达阈变更（候选 %d，丢弃 %d，无归属 %d）",
                        result.story_id, result.candidates, result.dropped,
                        result.unattributed)
            self._record(result, stats)
            return result

        # 跨轮归一 + 去重：同一份素材重跑不得在 PRD 上堆出近义评论（实测措辞每轮都变，
        # 条数已稳定 7/7/7）。键 = (Story, 素材标识)，素材换了（PRD 已改、新讨论）照常出。
        materials: dict[str, str] = {}
        for cp in points:
            sid = cp.story_id or rows[0].story_id
            materials.setdefault(sid, f"{source}|{source_url}|{hash(text[:2000])}")
        deduped: list[ChangePoint] = []
        for sid in {cp.story_id or rows[0].story_id for cp in points}:
            group = [cp for cp in points if (cp.story_id or rows[0].story_id) == sid]
            fresh, dup = self._dedupe.drop_duplicates(sid, materials[sid], group)
            deduped.extend(fresh)
            result.duplicates += dup
        if result.duplicates:
            self.metrics["duplicates"] += result.duplicates
            logger.info("[prd_watch] 归一/去重掉 %d 条（批内近义合并 + 跨轮重复）",
                        result.duplicates)
        points = deduped
        if not points:
            self._record(result, stats)
            return result

        # 按归属分组：每篇 PRD 只处理属于自己的变更（锚点与评论都落在自己那篇）
        by_story: dict[str, list[ChangePoint]] = {}
        for cp in points:
            by_story.setdefault(cp.story_id or rows[0].story_id, []).append(cp)
        # 锚定结果顺带算出「原文在哪一节」与「块直达链接」，卡片要这两项
        sections: dict[int, tuple[str, str]] = {}       # id(cp) → (章节, 跳转链接)
        for b in rows:
            for cp in by_story.get(b.story_id, []):
                cp.block_id = await self._locate_point(b, cp)
                if not cp.located:
                    result.unanchored += 1
                    continue
                try:
                    sections[id(cp)] = (await self._anchor.section_of(b.prd_token,
                                                                      cp.block_id),
                                        _block_link(b, cp.block_id))
                except Exception as e:      # 章节是增强项，取不到不影响主链路
                    logger.info("[prd_watch] 章节解析失败(%s): %s", cp.block_id[:12],
                                str(e)[:60])
        self.metrics["unanchored"] += result.unanchored

        # 文档侧判重：跳过「这处已经有等价评论」的变更。
        # 必须放在锚定之后 —— 判重的主信号是"同一个块 + 同一个说法"，
        # 而块 id 只有锚完才有。dry_run 也走这一步（干跑就是要看"真跑会写几条"）。
        points = await self._drop_already_commented(rows, points, result)
        if not points:
            self._record(result, stats)
            return result
        result.changes = points

        if self._cfg.dry_run:
            self.metrics["dry_run"] += len(points)
            logger.info("[prd_watch] dry_run 命中 %d 条（未写评论）覆盖 %d 篇 PRD: %s",
                        len(points), len(by_story),
                        " | ".join(p.title or p.suggested_text[:20] for p in points))
            self._record(result, stats)
            return result

        await self._emit(rows, by_story, result, source=source, source_url=source_url,
                         sections=sections, kind=kind)
        self._record(result, stats)
        return result

    async def _locate_point(self, binding: StoryBinding, cp: ChangePoint) -> str:
        """一条变更落到哪个块：依次试 quote_text / current_text / 建议表述里的实词。

        为什么不止试一次：模型给的 `quote_text` 常常是**它综合后的表述**而不是原文
        逐字片段（实测「补充申购赎回入口互不影响的表述」在文档里根本不存在，
        而它对应的原文段落就在那儿）。只试 quote 会把这类变更全判成"未定位"，
        评论降级成整篇——卡片上看不出这处改的是哪一段。

        最后一步只用**建议表述里出现过的实词**做关键词筛（取最长 8 字以上的一段），
        仍然定位不到就返回空串，绝不硬凑一个块。
        """
        for probe in (cp.quote_text, cp.current_text):
            if not probe:
                continue
            try:
                bid = await self._anchor.locate(binding.prd_token, probe)
            except Exception as e:
                logger.info("[prd_watch] 锚定异常(%s): %s", str(probe)[:20], str(e)[:60])
                continue
            if bid:
                return bid
        # 兜底：模型写的建议表述不会逐字出现在文档里，所以按**关键词共现**找块
        terms = locator_terms(f"{cp.current_text}{cp.suggested_text}{cp.title}")
        if len(terms) >= 2:
            try:
                bid = await self._anchor.locate_by_terms(binding.prd_token, terms)
            except Exception:
                bid = ""
            if bid:
                logger.info("[prd_watch] 原文未逐字命中，按关键词共现定位到 %s：%s（词 %s）",
                            bid[:12], cp.title[:24], "/".join(terms[:3]))
                return bid
        return ""

    async def _drop_already_commented(
        self, rows: list[StoryBinding], points: list[ChangePoint], result: WatchResult,
    ) -> list[ChangePoint]:
        """去掉文档里已有等价评论的变更，返回剩下的。

        每篇 PRD 只拉一次评论列表。拉取失败**不改判**：宁可多写一条评论，
        也不要因为读不到评论就把真变更静默吞掉——但要在日志里说清是哪篇失败，
        否则下次排查只会看到"这条为什么没写"。
        """
        by_story = {b.story_id: b for b in rows}
        cache: dict[str, list[dict[str, Any]]] = {}
        out: list[ChangePoint] = []
        from app.protocols.lark_cli import LarkCli
        for cp in points:
            binding = by_story.get(cp.story_id or (rows[0].story_id if rows else ""))
            doc = binding.prd_token if binding else ""
            if not doc:
                out.append(cp)
                continue
            if doc not in cache:
                try:
                    cache[doc] = await LarkCli().list_comments(
                        doc, identity=self._cfg.comment_identity)
                except Exception as e:
                    cache[doc] = []
                    logger.warning("[prd_watch] 评论列表读取失败(%s)，本篇不做文档侧判重: %s",
                                   doc[:12], str(e)[:120])
            hit = duplicate_of(cp, cache[doc])
            if hit is not None:
                result.already_commented += 1
                self.metrics["already_commented"] += 1
                logger.info("[prd_watch] 已有评论跳过：%s ≈ comment %s（块 %s）",
                            cp.title[:30], hit.get("comment_id"), str(cp.block_id)[:12])
                continue
            out.append(cp)
        return out

    def _record(self, result: WatchResult, stats: dict[str, Any]) -> None:
        """把一次窗口产出交回可观测层（未注入 hook 时静默——观测是旁路）。"""
        if self._record_hook is None:
            return
        try:
            self._record_hook(result, stats)
        except Exception:
            logger.debug("[prd_watch] 任务档案写入失败", exc_info=True)

    async def _extract_multi(
        self, text: str, rows: list[StoryBinding], matcher: Any = None,
    ) -> tuple[list[ChangePoint], dict[str, Any]]:
        """取每篇 PRD 的节选，交给识别器判归属。

        单篇也走 candidates 形态会让模型必须回填 story_id（多一层出错面），
        所以候选只有 1 篇时退回旧的单篇帧——老路径的行为一字不变。

        `matcher` 给了就整段换掉识别器（UI 对账）：UI 素材自带「画板文案」，
        不需要按讨论关键词挑 PRD 节选，节选直接取全文摘要。
        """
        if matcher is not None:
            excerpt = ""
            try:
                excerpt = await self._anchor.excerpt(rows[0].prd_token, 6000)
            except Exception as e:
                logger.warning("[prd_watch] PRD 原文取不到(%s): %s",
                               rows[0].prd_token[:12], e)
            points, stats = await matcher.match(text, excerpt)
            for cp in points:
                cp.story_id = rows[0].story_id
            return points, stats
        if len(rows) == 1:
            excerpt = ""
            try:
                excerpt = await self._anchor.excerpt_for(rows[0].prd_token, text)
            except Exception as e:
                logger.warning("[prd_watch] PRD 原文取不到(%s): %s",
                               rows[0].prd_token[:12], e)
            points, stats = await self._extractor.extract(text, excerpt)
            for cp in points:
                cp.story_id = rows[0].story_id      # 单篇路径：归属唯一，不用模型猜
            return points, stats

        candidates: list[PrdCandidate] = []
        for b in rows:
            try:
                excerpt = await self._anchor.excerpt_for(b.prd_token, text)
            except Exception as e:
                logger.warning("[prd_watch] PRD 节选取不到(%s/%s): %s",
                               b.story_id, b.prd_token[:12], str(e)[:80])
                excerpt = ""
            if not excerpt:
                # 正文取不到就不放进候选：放进去等于邀请模型把变更归给一篇读不到的文档
                self.metrics["no_excerpt"] += 1
                logger.info("[prd_watch] Story %s 的 PRD 正文为空，本次不进候选", b.story_id)
                continue
            candidates.append(PrdCandidate(story_id=b.story_id,
                                           story_title=b.story_title or b.story_id,
                                           prd_token=b.prd_token, excerpt=excerpt))
        if not candidates:
            return [], {"candidates": 0, "dropped": 0, "flagged": 0, "unattributed": 0,
                        "error": f"{len(rows)} 篇候选 PRD 正文全部取不到"}
        return await self._extractor.extract(text, "", candidates=candidates)

    async def _emit(
        self, rows: list[StoryBinding], by_story: dict[str, list[ChangePoint]],
        result: WatchResult, *, source: str, source_url: str = "",
        sections: dict[int, tuple[str, str]] | None = None, kind: str = "讨论",
    ) -> None:
        """按 Story 分组写评论，最后发**一张**卡片（一场讨论一张，不逐 Story 刷屏）。

        逐篇写、一次发：写要分开是因为评论必须落在各自文档的 block 上；发要合并是因为
        同一场会拆成 5 张卡片，产品要在群里翻 5 条消息才知道这次会改了多少东西。
        """
        occurred = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        by_id = {b.story_id: b for b in rows}
        items: list[CardItem] = []
        for story_id, points in by_story.items():
            binding = by_id.get(story_id)
            if binding is None:
                continue
            for cp in points:
                cr_id = self._next_cr_id(binding.story_id)
                cp.cr_id = cr_id
                comment = render_comment(
                    cp, cr_id=cr_id, story_title=binding.story_title or binding.story_id,
                    story_id=binding.story_id,
                    source=source, occurred_at=occurred,
                    discussion_url=source_url,        # 群会话深链 / 妙记链接
                    story_url=binding.story_url,
                )
                comment_id = await self._write_comment(cp, binding, comment, source_url)
                # 决策状态机落 CR 表（与知识台账的时序归档分开，见 cr_store.py）。
                # 写失败只告警：评论已经进 PRD 了，台账写不进去不该回滚这条变更。
                await self._cr.save(CrRow(
                    cr_id=cr_id, title=cp.title, change_type=cp.change_type,
                    current_text=cp.current_text, suggested_text=cp.suggested_text,
                    story_id=binding.story_id, prd_token=binding.prd_token,
                    block_id=cp.block_id, comment_id=comment_id,
                    chat_id=binding.chat_id, source=source, source_url=source_url,
                    confidence=cp.confidence, band=cp.band, diff_kind=kind))
                row = cp.to_ledger_row(cr_id, binding.story_id, binding.chat_id)
                # 决策轮询靠这两个坐标回查评论（缺一个就永远 pending，见 decisions.py）
                row[COLUMNS["comment_id"]] = comment_id
                row["PRD文档token"] = binding.prd_token
                row["关联群chat_id"] = binding.chat_id
                await self._ledger.record_change(row)
                self._dedupe.record(binding.story_id,
                                    result.source_ref or source, cp)
                section, jump = (sections or {}).get(id(cp), ("", ""))
                items.append(CardItem(
                    story=_meta(binding, prd_title=await self._prd_title(binding)),
                    prd_url=binding.prd_url, cr_id=cr_id, point=cp,
                    section=section, jump_url=jump))

        at_ids, at_names = await self._collect_at(rows)
        card = build_notice_card(items, source=source, source_url=source_url,
                                 at_open_ids=at_ids, at_names=at_names)
        if self._send_card is not None and items:
            try:
                await self._send_card(rows[0].chat_id, card)
            except Exception as e:
                logger.warning("[prd_watch] 群卡片发送失败: %s", e)
                result.errors.append(f"卡片发送失败: {str(e)[:80]}")
        self.metrics["emitted"] += len(items)
        logger.info("[prd_watch] 出 %d 条评论覆盖 %d 篇 PRD，卡片已发（来源：%s）",
                    len(items), len(by_story), source)

    async def _prd_title(self, binding: StoryBinding) -> str:
        """PRD 文档标题（卡片 PRD 列的可点文本）。

        从已取过的块里拿（`BlockAnchor` 同一窗口有缓存，不额外发请求）：
        飞书文档第一块就是标题（实测 `[{'text': '智能纪要：…'}]`）。取不到返回空串，
        渲染层退回 Story 标题——不编标题，也不让标题取失败拖垮整张卡片。
        """
        try:
            return (await self._anchor.title(binding.prd_token))[:40]
        except Exception:
            return ""

    async def _collect_at(self, rows: list[StoryBinding]) -> tuple[list[str], list[str]]:
        """多篇 Story 的负责人合成一份 @ 名单（同一个人挂多篇/多角色只 @ 一次）。"""
        ids: list[str] = []
        names: list[str] = []
        for b in rows:
            known, pending = _split_at_targets(b)
            ids.extend(known)
            names.extend(pending)
        resolved, unmatched = await self._resolve_at(rows[0].chat_id, names)
        return list(dict.fromkeys(ids + resolved))[:6], list(dict.fromkeys(unmatched))

    async def _resolve_at(
        self, chat_id: str, names: list[str],
    ) -> tuple[list[str], list[str]]:
        """姓名 → 该群 open_id（按群成员名单精确匹配，重名与猜都不做）。

        Meegle 给的是姓名与 email，而 email 换 open_id 要应用通讯录权限（实测该接口
        在当前应用下 404），所以走「本群成员名单」这条已在用的路径（同
        main.resolve_sender_name）。名单取不到就整体退回 @姓名纯文本——通知照发，
        只是不会推送给个人。
        """
        if not names or self._member_resolver is None:
            return [], names
        try:
            members = await self._member_resolver(chat_id)
        except Exception as e:
            logger.warning("[prd_watch] 群成员名单取失败，退回 @姓名: %s", str(e)[:100])
            return [], names
        by_name: dict[str, str] = {}
        for item in members or []:
            name = str(getattr(item, "name", "") or "").strip()
            oid = str(getattr(item, "id", "") or "").strip()
            if name and oid:
                # 重名不猜：出现第二次就把它标为不可用（@错人比不@更糟）
                by_name[name] = oid if name not in by_name else ""
        ids = [by_name[n] for n in names if by_name.get(n)]
        missing = [n for n in names if not by_name.get(n)]
        if missing:
            logger.info("[prd_watch] %s 有 %d 人不在本群（或重名），以 @姓名 形式提示",
                        chat_id[:12], len(missing))
        return ids, missing

    async def _write_comment(
        self, cp: ChangePoint, binding: StoryBinding, comment: str,
        discussion_url: str = "",
    ) -> str:
        """写一条评论，返回 comment_id（拿不到就返回空串，台账仍记 pending）。

        `discussion_url` 作为可点的 link 元素附在评论末尾：正文是纯文本，
        裸 URL 在客户端点不开，而「这条建议是从哪段讨论来的」正是要能点回去的东西。
        """
        from app.protocols.lark_cli import LarkCli, LarkCliError

        try:
            payload = await LarkCli().add_comment(
                binding.prd_token, comment, block_id=cp.block_id,
                identity=self._cfg.comment_identity,
                link_url=comment_primary_link(discussion_url, binding.story_url),
            )
        except LarkCliError as e:
            hint = ""
            if e.missing_scopes:
                hint = "（缺 scope: " + ",".join(e.missing_scopes) + "）"
            logger.warning("[prd_watch] 写评论失败%s: %s", hint, str(e)[:150])
            return ""
        except Exception as e:
            logger.warning("[prd_watch] 写评论异常: %s", str(e)[:150])
            return ""
        data = payload.get("data") or {} if isinstance(payload, dict) else {}
        return str(data.get("comment_id") or data.get("id") or "")

    def _next_cr_id(self, story_id: str) -> str:
        key = f"{story_id}:{datetime.now(TZ).strftime('%Y%m%d')}"
        seq = self._seq_by_day.get(key, 0) + 1
        self._seq_by_day[key] = seq
        return make_cr_id(story_id, seq)

    def snapshot(self) -> dict[str, Any]:
        """供 /help 之外的巡检与单测断言。"""
        return {
            "enabled": self._cfg.enabled,
            "dry_run": self._cfg.dry_run,
            "watch_chats": len(self._cfg.watch_chats),
            "open_windows": {c: len(w.lines) for c, w in self._windows.items()},
            "ledger": self._ledger.disabled_reason or "ready",
            "binding": (self._binding.snapshot() if self._binding is not None
                        else {"source": "ledger"}),
            "from_meegle": self.metrics.get("from_meegle", 0),
            "metrics": dict(self.metrics),
            "prompt_version": self._extractor.prompt_version,
            # CR 台账是否持久化：False = 决策状态只在内存里，重启即丢。
            # 必须进 snapshot，否则「点了采纳重启后又要再点」会被当成偶发 bug。
            "cr_store": "ready" if self._cr.ready else "memory(重启即丢)",
        }


def _meta(binding: StoryBinding, prd_title: str = "") -> StoryMeta:
    """绑定 → 卡片需要的 Story 坐标。空间名/类型只有 Meegle 来源才有，
    台账手填时留空，渲染层自己跳过缺项（不编假空间名）。"""
    return StoryMeta(
        story_id=binding.story_id, story_title=binding.story_title,
        story_url=binding.story_url, space_name=getattr(binding, "space_name", ""),
        story_type=getattr(binding, "story_type", "Story") or "Story",
        status=binding.status, prd_title=prd_title)


def _block_link(binding: StoryBinding, block_id: str) -> str:
    """PRD 块的直达链接：`{文档链接}#{block_id}`。

    格式依据 lark-cli `lark-doc` skill 的「生成 block 直达链接」：返回
    `文档基础 URL#block_id`，**没有 block_id 时不得猜测**（所以这里 block 为空就返回空串）。
    """
    base = binding.prd_url or ""
    if not base or not block_id:
        return ""
    return f"{base.split('#')[0]}#{block_id}"


def _chat_app_link(chat_id: str) -> str:
    """群会话的 applink 深链（卡片「变更依据」要能点回原讨论）。

    格式依据 lark-cli 内嵌文档（`lark-cli skills read lark-im`）：
    `https://<applink_host>/client/chat/open?openChatId=<oc_xxx>`，并明确写了
    「never `chatId=<oc_xxx>`」——所以参数名不能改着玩。
    """
    if not chat_id.startswith("oc_"):
        return ""
    return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"


async def _call_many(src: Any, chat_id: str) -> list[Any]:
    """从一个绑定源取该群全部绑定；源只实现了单绑定接口时退回单条。

    写成鸭子类型而不是改协议：BindingSource 与 Ledger 都有 `bindings_for_chat`，
    但第三方/测试桩可能只给了 `binding_for_chat`——那里抛的不是类型错误，
    而是"这个群的第二个 Story 永远不被监听"，看不见也难查。
    """
    if src is None or not chat_id:
        return []
    many = getattr(src, "bindings_for_chat", None)
    if callable(many):
        return list(await many(chat_id) or [])
    one = await src.binding_for_chat(chat_id)
    return [one] if one is not None else []


def _split_at_targets(binding: StoryBinding) -> tuple[list[str], list[str]]:
    """绑定里的负责人 → (已知 open_id, 待解析姓名)。

    两种来源的形态不同，都在这儿归一：Meegle 给姓名（需按群成员名单换成 open_id），
    而多维表格老路径里管理员常直接填 `ou_...`（那就是成品，不必再解析）。
    """
    ids: list[str] = []
    names: list[str] = []
    for value in binding.owners.values():
        value = (value or "").strip()
        if not value:
            continue
        if value.startswith("ou_") or value.startswith("on_"):
            if value not in ids:
                ids.append(value)
        elif value not in names:
            names.append(value)
    return ids[:6], names[:6]
