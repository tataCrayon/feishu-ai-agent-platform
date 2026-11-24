"""CMDB 工单观察编排 — 轮询 → 事件判定 → 按订阅者发卡片 → 落状态。

三类事件（顺序即优先级）：

| 事件 | 判定 | 卡片 |
|---|---|---|
| `order_new` | 工单里第一次出现「创建人=某订阅者」的任务 | 绿头·新工单 |
| `task_started` | 跟踪中的工单里，某订阅者的任务 未执行 → 执行中 | 蓝头·开始执行 |
| `task_done` / `task_failed` | 同上，→ 终态 | 绿头·完成 / 红头·失败 |

**取数策略（两档，按「这张单是不是新来的」分流）**

| 场景 | 接口 | 成本（实测 12 张采样） |
|---|---|---|
| 发现最近有哪些工单 | `TotalOrderApi?action=tree`（一次 150 张 id） | ~260ms / 数 KB |
| 新工单全量读 | `OrderApi?action=data` | ~125ms / 3~183KB（`html_content` 占大头） |
| 已跟踪工单轮询 | `OrderApi?action=deploystatus` | ~56ms / <1KB |
| 每 `refresh_sec` 全量校准 | `OrderApi?action=data` | 同「新工单全量读」 |

新工单必须全量读一次（要判断任务创建人是谁）；一旦入了跟踪，每轮只轮询状态码，
每 `refresh_sec` 才全量重拉一次补齐 SQL / NACOS 状态与任务增删。全量重扫全部历史
工单是最贵的做法，这里用增量逻辑避免了。

设计取舍（写在这里是为了下一个人别再问一遍）：

1. **「新工单」判定看任务创建人，不看工单创建人**。`data.client[].creator` 是子单
   发起人，而 `sql_list[].creator` / `deploy_data[].creator` 才是实际要做那条 SQL /
   那个发布的人。提醒按任务层归属，才可能给每个订阅者只看自己的部分。
2. **只有命中的工单进入长期跟踪**。没人订阅的工单看一眼就丢，不然 state.json 会长成
   整个 CMDB 的镜像，且每轮要拉几十张工单（内网接口不快）。
3. **首轮是「发现」而不是「提醒风暴」**。首次见到一张工单时，只对「还没有任何任务
   开始执行」发 order_new；工单里已经有任务在跑/已结束的，直接静默入册——否则
   打开开关那一刻会把所有历史工单都喊一遍。
4. **状态迁移的基线来自上一轮**，绝不来自「我以为是未执行」。第一轮见到一个
   已经在跑的任务时不发 started（那不是「刚刚开始」），只记录基线。
5. **一人一张卡**。同一张工单常有多人协作，每个人只该看到自己名下的任务，
   所以卡片按订阅者切分、按订阅者的推送目标发送；去重键也带 open_id。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import CmdbWatchConfig
from app.domains.cmdb_watch.client import (
    DEPLOY_STATES,
    ENVS,
    ENV_LABELS,
    ENV_PROD,
    CmdbClient,
    CmdbError,
    Order,
    Task,
    normalize_env,
    split_order_key,
)
from app.domains.cmdb_watch.progress import (
    progress_text,
    watch_event,
    watch_phase_for_notify,
)
from app.domains.cmdb_watch.render import (
    build_order_card,
    build_task_card,
    build_watch_card,
)
from app.domains.cmdb_watch.store import (
    PHASE_DONE,
    PHASE_RUNNING,
    OrderState,
    OrderWatch,
    Subscriber,
    TaskState,
    WatchStore,
)

logger = logging.getLogger("cmdb_watch.watcher")

# fn(chat_id 或 open_id, card) → awaitable：由 main 注入（渠道侧按前缀自动选
# receive_id_type，`oc_` 走 chat_id、`ou_` 走 open_id 直投私聊）
SendCard = Callable[[str, Any], Awaitable[Any]]
# 观测回调：fn(event, payload)，main 里转 TaskRecord
RecordHook = Callable[[str, dict[str, Any]], None]


@dataclass
class WatchStats:
    """一轮扫描的计数（snapshot / 日志用）。"""

    rounds: int = 0
    orders_scanned: int = 0
    orders_full: int = 0          # 本轮走了全量 data 的工单数（成本指标）
    orders_skipped: int = 0       # id 探测落空（该号不存在）的次数，成本侧指标
    orders_new: int = 0
    tasks_started: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    watches_started: int = 0
    watches_done: int = 0
    watches_dropped: int = 0
    cards_sent: int = 0
    cards_failed: int = 0
    errors: list[str] = field(default_factory=list)
    last_round_ts: float = 0.0
    last_round_ms: float = 0.0
    last_full_refresh_ts: float = 0.0
    skipped_rounds: int = 0    # 连续出错被跳过的轮数（客户端熔断计数）

    def summary(self) -> str:
        return (f"轮次 {self.rounds}，扫单 {self.orders_scanned}（全量 {self.orders_full}，"
                f"跳过 {self.orders_skipped}），新单 {self.orders_new}，开始 {self.tasks_started}，"
                f"完成 {self.tasks_done}，失败 {self.tasks_failed}，"
                f"关注开始 {self.watches_started}，关注完成 {self.watches_done}，"
                f"发卡 {self.cards_sent}"
                + (f"（失败 {self.cards_failed}）" if self.cards_failed else ""))


class CmdbWatcher:
    """轮询与事件判定。所有 IO 走 to_thread，不阻塞事件循环。"""

    def __init__(self, config: CmdbWatchConfig, *,
                 client: CmdbClient | None = None,
                 store: WatchStore | None = None,
                 send_card: SendCard | None = None,
                 record_hook: RecordHook | None = None) -> None:
        self._cfg = config
        self._client = client or CmdbClient(base_url=config.base_url,
                                           session_id=config.session_id)
        self.store = store or WatchStore(config.state_path or None)
        # 构造即读档：状态由另一个进程（main 的轮询循环）写，本进程不主动刷新，
        # 不读就意味着「查询状态」这类工具对着空档案回答「你没订阅」「登录态缺失」——
        # 用户上一句刚订阅完，下一句被告知没订阅，看着像功能坏了。
        # 读失败只记日志：空档案等于「还没人订阅」，比启动即崩好。
        try:
            self.store.load()
        except (OSError, ValueError) as e:
            logger.warning("[cmdb_watch] 状态档案读取失败（按空档案继续）: %s", e)
        self._send_card = send_card
        self._record = record_hook
        self.stats = WatchStats()
        # 本轮内「关注工单首次观察」的工单键集合：这些工单要取基线但不发卡
        self._watch_baseline_keys: set[str] = set()
        # 本轮内刚被全量读过的工单键：第二轮轮询跳过它们，避免同一轮问两遍
        self._fresh_keys: set[str] = set()

    @property
    def active(self) -> bool:
        """启用即可跑：订阅者由对话自助建立，配置里不再写死名单。"""
        return bool(self._cfg.enabled)

    def bind_client(self, client: Any) -> None:
        """换掉取数客户端（dsh 的 MCP 子进程用：那里面只做订阅登记，不该真打 CMDB）。

        子进程里就地建的 watcher 只是「看档案」的影子，任何一次网络调用都是白花的
        内网延迟；换成一个不做请求的实现更安全，也避免它在 50 人订阅时再叠一份
        轮询负担（真实的提醒只有 main 进程那一份）。
        """
        self._client = client

    # 连续多少轮全失败后拉长间隔（CMDB 挂了不该每分钟刷一堆同样的错误日志）
    _BACKOFF_ERROR_ROUNDS = 5

    # ── 生命周期 ────────────────────────────────────────────
    async def scan_once(self) -> WatchStats:
        """跑一轮：发现新工单 → 新单全量读 → 跟踪单轻量轮询 → 落盘。"""
        started = time.monotonic()
        stats = self.stats
        stats.rounds += 1
        stats.errors = []
        await asyncio.to_thread(self.store.load)

        if not self.store.subscribers and not self.store.order_watches:
            # 没人订阅（按姓名、按工单都算）：不扫（零网络开销），保留跟踪状态不动
            self._finish_round(stats, started)
            return stats

        relevant = await asyncio.to_thread(self._discover_orders, stats)
        # 「已跟踪」按所有还在跟踪的单算，不按「还能轻量轮询」算：纯 SQL 单不进
        # 每轮预算，但它仍然被跟踪，不能被新单发现当成本轮新单再全量读一遍。
        # 集合里全是 `prod:5778` / `uat:1924` 这种带环境的键（见 client 抬头：
        # 两套编号空间重叠，裸 id 会串台）。
        tracked = {o.key for o in self.store.tracked_orders()}
        poll_keys = {o.key for o in self.store.needs_polling()}
        watched_keys = self.store.watched_order_ids()
        # 被关注但还没入跟踪集合的工单（人先关注、这张单此前没人订阅过任务）
        for key in watched_keys - tracked:
            relevant.add(key)
        # 首次观察被关注工单时「取当前值为基线」——不在订阅那一刻补发历史卡片
        self._watch_baseline_keys = {
            w.envelope for w in self.store.order_watches.values() if not w.phase}
        errors = 0
        attempted = 0

        # ① 新工单：必须全量读（要判断任务创建人），但只读本轮新出现且无人跟踪的
        # 排序键用 (id, env) 而非裸 id：两个环境各有一批新单时要一起排，且顺序稳定
        for key in sorted(relevant - tracked, key=_key_sort, reverse=True):
            env, order_id = split_order_key(key)
            attempted += 1
            try:
                order = await asyncio.to_thread(self._client.fetch_order, order_id, env)
            except CmdbError as e:
                errors += 1
                stats.errors.append(f"{key}: {e}")
                if key in watched_keys:
                    await self._on_watch_error(key, stats)
                continue
            stats.orders_scanned += 1
            stats.orders_full += 1
            self._fresh_keys.add(key)
            await self._process_order(order, stats)
            if key in watched_keys or key in self._watch_baseline_keys:
                await self._process_watches(order, stats)
            self.store.advance_watermark(env, order_id)

        # ② 已跟踪工单：默认只轮询状态码；到点才全量校准一次
        full_refresh = self._need_full_refresh()
        # 被指定关注的工单必须每轮全量读——关注方盯的是「整单有没有开始/发完」，
        # 而任务级状态（尤其 SQL/NACOS）只有 `action=data` 里有。这类工单通常个位数，
        # 成本可控；不用全量轮询覆盖全部跟踪工单，是为了不让「按姓名订阅」的存量
        # 工单数把请求量带起来。
        #
        # 新单（本轮第一次见到的）本轮已经全量读过一次，不再重复问：
        # 这是 1 人订阅时每轮省 1 次、50 人订阅时每轮省最多 `scan_limit` 次的那一刀。
        targets = [k for k in sorted(poll_keys, key=_key_sort, reverse=True)
                   if k not in self._fresh_keys][: max(1, self._cfg.scan_limit)]

        # 取数与判定分开：同一轮的 N 张单并发取（8 个在途），取回来再按顺序判定。
        # 判定里有「同一任务同一事件只发一次」的状态推进，必须串行；取数没有顺序
        # 依赖，串行只会把 N 次 ~50ms 的延迟叠起来——实测 30 张单 1468ms → 219ms。
        # 「还在动的工单」通常只有个位数，但 50 人订阅后出现一次十几张同时在跑
        # 并不稀奇，这一步决定了那种时刻这一轮会不会拖过 120s 的轮询间隔。
        need_full_keys = {k for k in targets if full_refresh or k in watched_keys}
        fetched, fetch_errors = await self._fetch_batch(
            targets, full=(k in need_full_keys for k in targets))
        # 两个列表同源生成长度必然一致，这里只按需对齐
        for key, payload in zip(targets, fetched, strict=False):
            env, order_id = split_order_key(key)
            attempted += 1
            err = fetch_errors.get(key)
            if err is not None:
                errors += 1
                stats.errors.append(f"{key}: {err}")
                if key in watched_keys:
                    await self._on_watch_error(key, stats)
                continue
            stats.orders_scanned += 1
            if key in need_full_keys:
                stats.orders_full += 1
                await self._process_order(payload, stats)
                if key in watched_keys or key in self._watch_baseline_keys:
                    await self._process_watches(payload, stats)
            else:
                await self._process_status(env, order_id, payload, stats)
        if full_refresh:
            stats.last_full_refresh_ts = time.time()
        fresh = self._fresh_keys
        self._fresh_keys = set()

        if self._watch_baseline_keys or errors:
            # 有失败也要落盘：关注工的 `error_rounds` 就靠它累计，不落盘永远停在 1
            await asyncio.to_thread(self.store.save)
        if attempted and errors == attempted:
            # 全失败：不重写 state（内容没变），也不刷屏
            stats.skipped_rounds += 1
            if stats.skipped_rounds % self._BACKOFF_ERROR_ROUNDS == 0:
                logger.warning("[cmdb_watch] 连续 %d 轮全部失败，检查 CMDB 连通性与 session",
                               stats.skipped_rounds)
            self._finish_round(stats, started)
            return stats
        stats.skipped_rounds = 0
        self.store.prune()
        await asyncio.to_thread(self.store.save)
        self._log_round_plan(stats, fresh)
        self._finish_round(stats, started)
        return stats

    # 同一轮最多几张单在途（CMDB 是内网 Django，8 并发实测 30 张单 265ms，
    # 再高收益递减且容易撞上对端限流）
    _FETCH_CONCURRENCY = 8

    async def _fetch_batch(self, order_keys: list[str], *, full: Any) -> tuple[
            list[Any], dict[str, str]]:
        """并发取一批工单：`full` 逐位指示走全量 data 还是轻量 deploystatus。

        `order_keys` 是带环境的键（`prod:5778` / `uat:1924`），环境从这里解出来
        传给 client——不这样传就只能默认生产，UAT 单会被当成不存在。

        返回 (与输入同序的结果, 失败映射)。**单张失败不抛**——一张单拉不到不该让
        这一轮其余几十张都不更新；错误交给调用方累计并走关注单的失败计数。
        """
        sem = asyncio.Semaphore(self._FETCH_CONCURRENCY)
        full_flags = list(full)
        errors: dict[str, str] = {}

        async def one(order_key: str, is_full: bool) -> Any:
            env, order_id = split_order_key(order_key)
            async with sem:
                try:
                    fn = (self._client.fetch_order if is_full
                          else self._client.fetch_status)
                    return await asyncio.to_thread(fn, order_id, env)
                except CmdbError as e:
                    errors[order_key] = str(e)
                    return None

        results = await asyncio.gather(
            *(one(k, flag) for k, flag in zip(order_keys, full_flags, strict=False)))
        return list(results), errors

    @staticmethod
    def _finish_round(stats: WatchStats, started: float) -> None:
        stats.last_round_ms = (time.monotonic() - started) * 1000
        stats.last_round_ts = time.time()

    def _log_round_plan(self, stats: WatchStats, fresh: set[str]) -> None:
        """每轮记一行「这轮打算问谁」，50 人订阅时排查「为什么没提醒」全靠它。

        只在有事件或出错时 main 会打 summary；这条是 debug，常态开销为零。
        """
        logger.debug("[cmdb_watch] 本轮计划：新单 %d 张，待轮询 %d 张（跳过 %d），全量 %s",
                     len(fresh), len(self.store.needs_polling()),
                     stats.orders_skipped, self._need_full_refresh())

    def _need_full_refresh(self) -> bool:
        """距离上次全量校准是否已过一个 `refresh_sec`（首次为 True）。"""
        last = self.stats.last_full_refresh_ts
        return last <= 0 or (time.time() - last) >= self._cfg.refresh_sec

    # ── 发现 ────────────────────────────────────────────────
    def _discover_orders(self, stats: WatchStats) -> set[str]:
        """返回本轮需要全量读的候选工单键（`prod:5778` / `uat:1924`）。

        **两个环境各跑一遍**：生产与 UAT 是两套独立的工单表与 id 序列，
        订阅一次就该两边都盯（用户口径：UAT 的变更同样要提醒）。

        每个环境的发现走两条路径：
        1. **工单列表接口**（要 sessionid）：一次拿最近一批子工单 id（实测 143 张
           / 260ms），增量 = id 大于该环境水位线的那些。首选，比逐一探测便宜得多。
        2. **id 顺序探测**（免 session）：从水位线 +1 起探测 `probe_span` 个。
           order_id 连续递增，不存在的 id 接口会明确拒绝，所以这是列表进不去时
           唯一可行的发现手段（单张 ~50ms）。

        刻意**不做**「按标题里的发起人预筛」：实测 40 张工单里 20 张的任务创建人与
        标题发起人不一致（有人替别人建单），预筛会把订阅者名下的单漏掉。发现阶段
        的开销本来就只有「新单数量」那么多，不值得用正确性去换。
        """
        fresh: set[str] = set()
        for env in ENVS:
            fresh |= self._discover_env(env, stats)
        return fresh

    def _discover_env(self, env: str, stats: WatchStats) -> set[str]:
        """单个环境的发现（口径见 `_discover_orders`）。返回带环境前缀的键。"""
        cfg = self._cfg
        fresh: set[str] = set()
        listed = False
        watermark = self.store.watermark(env)
        try:
            ids = self._client.list_recent_order_ids(env)[:cfg.scan_limit]
            listed = True
            # 冷启动（还没有水位线）时不能把整批历史当成「新工单」去全量拉：
            # 那样一次就几十张 × 125ms，且订阅者根本还没出现。只把水位设到最新，
            # 从下一张新建的开始发现。
            if not watermark:
                self.store.advance_watermark(env, max(ids) if ids else 0)
                logger.info("[cmdb_watch] 冷启动对齐水位 %s #%s，从现在起发现新工单",
                            ENV_LABELS.get(env, env), self.store.watermark(env))
                return fresh
            fresh = {f"{env}:{i}" for i in ids if i > watermark}
            if ids:
                self.store.advance_watermark(env, max(ids))
            logger.debug("[cmdb_watch] %s 列表发现 %d 张（新 %d）",
                         ENV_LABELS.get(env, env), len(ids), len(fresh))
        except CmdbError as e:
            stats.errors.append(f"{ENV_LABELS.get(env, env)}列表入口: {e}")
            if not e.need_session:
                # 网络问题：本轮退回探测也没意义（同一台机器），放弃这一轮发现
                return fresh
            # 登录态过期在常驻进程里是常态。要不要自己登由 CMDB_ALLOW_RELOGIN 决定
            # （默认不登：登录会顶掉同账号的浏览器会话）；续上了本轮就能走列表，
            # 续不上就安静降级为 id 探测。
            renew = getattr(self._client, "renew_session", None)
            if callable(renew) and renew():
                try:
                    ids = self._client.list_recent_order_ids(env)
                except CmdbError as retry_error:
                    logger.debug("[cmdb_watch] 续期后列表重试仍失败: %s", retry_error)
                else:
                    watermark = self.store.watermark(env)
                    fresh.update({f"{env}:{i}" for i in ids if i > watermark})
                    if ids:
                        self.store.advance_watermark(env, max(ids))
                    logger.info("[cmdb_watch] 登录态已续期，列表发现恢复（%d 张）", len(ids))
                    stats.errors.clear()   # 本轮的「未登录」已解决，别再报给 summary
                    return fresh
            logger.debug("[cmdb_watch] 列表入口缺 session，退回 id 探测")
        if not cfg.probe_span:
            return fresh
        start = max(self.store.watermark(env) + 1, 1)
        # 探测步长自适应：列表走通时水位已对齐最新，只需往后看一小段
        # （新工单在几十秒内连建几张是常态）；列表不可用才按 probe_span 全宽扫。
        span = min(cfg.probe_span, cfg.probe_step) if listed else cfg.probe_span
        for order_id in range(start, start + max(0, span)):
            try:
                if self._client.order_exists(order_id, env):
                    fresh.add(f"{env}:{order_id}")
                else:
                    stats.orders_skipped += 1
                self.store.advance_watermark(env, order_id)
            except CmdbError as e:
                stats.errors.append(f"探测 {env} #{order_id}: {e}")
                break   # 网络断了就不再往后扫
        return fresh

    # ── 轻量轮询分支 ─────────────────────────────────────────
    async def _process_status(self, env: str, order_id: int, status: dict[str, Any],
                              stats: WatchStats) -> None:
        """只用 `deploystatus` 的状态码推进订阅者的发布任务。

        任务清单（创建人、名称）首见时已落盘，每轮真正会变的只有状态码；deploy 的
        状态恰好有独立接口（实测 <1KB / ~50ms），而 `action=data` 要把整篇
        `html_content` 拖回来（3~183KB / ~125ms）。

        局限：本分支只覆盖 deploy（`deploystatus` 实测只回 deploy 状态，`NacosApi`
        也没有 status 动作）；SQL / NACOS / 定时任务的迁移由周期性全量刷新
        （`refresh_sec`）兜住，所以它们的提醒最多晚一个刷新周期——SQL 的权威状态
        本来就要另调 ArcheryApi，全量拉也拿不到更准的值，这个延后是可接受代价。

        代价：SQL 已收口、只剩部署在跑的单每轮仍要问一次轻量接口（纯 SQL 单在
        常驻跟踪里不会一直挂着「未执行」，多问几次的量级可忽略）。

        **被指定关注的工单不走这条**：整单阶段要按全部任务判定，而这里只有 deploy，
        所以 `scan_once` 对 `watched_order_ids()` 里的单子强制全量读。
        """
        known = self.store.orders.get(f"{env}:{order_id}")
        if known is None:
            return
        deploy_status = status.get("deploy_status") or {}
        events: list[tuple[Task, str, Subscriber]] = []
        for key, prev in list(known.tasks.items()):
            if not key.startswith("deploy:"):
                continue
            deploy_id = _id_from_key(key)
            pair = deploy_status.get(str(deploy_id)) or deploy_status.get(deploy_id)
            if not isinstance(pair, (list, tuple)) or not pair:
                continue
            raw = str(pair[0])
            state = int(raw) if raw.lstrip("-").isdigit() else 0
            current = Task(kind="deploy", task_id=deploy_id, name=prev.name,
                           creator=prev.creator, state=state,
                           state_label=DEPLOY_STATES.get(state, f"未知({state})"))
            event = _transition(prev, current)
            if not event:
                continue
            known.tasks[key] = _to_state(current)
            events.extend((current, event, sub)
                          for sub in self.store.subscribers_for(
                              {current.creator}, kind=current.kind))
        order = Order(order_id=order_id, order_name=known.order_name, env=env)
        for task, event, sub in events:
            _bump(stats, event)
            await self._notify_task(order, sub, task, event, stats)

    # ── 全量处理 ─────────────────────────────────────────────
    async def _process_order(self, order: Order, stats: WatchStats) -> None:
        watched = self.store.watched_users()
        mine = order.mine(watched)
        known = self.store.orders.get(order.key)

        if not mine:
            # 原本在跟踪、后来订阅者的任务全被删了 → 退出跟踪（不再每轮拉它）
            if known is not None:
                logger.info("[cmdb_watch] %s 已无订阅者的任务，退出跟踪", order.key)
                self.store.orders.pop(order.key, None)
            return

        by_sub = _group_by_subscriber(mine, self.store)
        if known is None:
            # 首次见到：先落基线再发卡。发卡失败时下一轮走「已跟踪」分支重试，
            # 而不是回头再走一次「首次见到」——后者会因 claim 记录还在而发不出去。
            known = self.store.upsert_order(order.order_id, order.order_name,
                                            {_key(t): _to_state(t) for t in mine},
                                            env=order.env)
            if any(t.running or t.done for t in mine):
                logger.info("[cmdb_watch] %s 首次发现但已有执行记录，静默入册"
                            "（%d 条订阅者任务）", order.key, len(mine))
                known.new_notified = [s.open_id for s, _ in by_sub]
                return
            stats.orders_new += 1
            for sub, tasks in by_sub:
                await self._notify_new_order(order, sub, tasks, stats, known)
            return

        # 已在跟踪：给「还没收到过这张单的新卡片」的订阅者补发一次。
        # 两类来源共用这段：① 上一轮发卡失败（网络抖动）；② 用户在工单之后才订阅。
        # 判据是 `new_notified`（每人 open_id 只记一次），所以不会重放历史。
        for sub, tasks in by_sub:
            if sub.open_id in known.new_notified:
                continue
            if any(t.running or t.done for t in tasks):
                # 他的任务已经在跑了：补「新工单」没意义，状态事件会接着发
                known.new_notified.append(sub.open_id)
                continue
            await self._notify_new_order(order, sub, tasks, stats, known)
        known.order_notice_pending = False

        events: list[tuple[Task, str, Subscriber]] = []
        for task in mine:
            prev = known.tasks.get(_key(task))
            event = _transition(prev, task)
            if not event:
                continue
            events.extend((task, event, sub)
                          for sub in self.store.subscribers_for(
                              {task.creator}, kind=task.kind))
        # 先更新基线再发卡：发卡失败只影响那一张，不会让同一个迁移下一轮重判
        known.tasks = {_key(t): _to_state(t) for t in mine}
        known.order_name = order.order_name or known.order_name
        for task, event, sub in events:
            _bump(stats, event)
            await self._notify_task(order, sub, task, event, stats)

    # ── 发卡 ────────────────────────────────────────────────
    async def _notify_new_order(self, order: Order, sub: Subscriber, tasks: list[Task],
                                stats: WatchStats, known: OrderState) -> None:
        """给一个订阅者发「新工单」卡片（只含他自己的任务）。"""
        key = f"order:{order.key}:{sub.open_id}"
        if not self.store.claim_notice(key):
            # 已发过（含上一轮成功）：补进 known，下一轮不再尝试
            if sub.open_id not in known.new_notified:
                known.new_notified.append(sub.open_id)
            return
        card = build_order_card(order, tasks, base_url=self._cfg.base_url,
                                actor=sub.cmdb_user)
        if await self._deliver(sub.target, card, f"新工单 #{order.order_id}", stats):
            known.new_notified.append(sub.open_id)
            logger.info("[cmdb_watch] 新工单提醒 %s → %s（%d 条任务）",
                        order.key, sub.cmdb_user, len(tasks))
        else:
            # 没发出去：回滚额度，下一轮重试（`new_notified` 里没有他 → 会再走一遍）
            self.store.release_notice(key)
            known.order_notice_pending = True
            stats.cards_failed += 1

    async def _notify_task(self, order: Order, sub: Subscriber, task: Task, event: str,
                           stats: WatchStats) -> None:
        # 同一任务同一事件对同一人只发一次（轮询间隔内状态反复跳变时不重复打扰）
        key = f"task:{order.key}:{task.key}:{event}:{sub.open_id}"
        if not self.store.claim_notice(key):
            return
        card = build_task_card(task, order, event, base_url=self._cfg.base_url)
        if await self._deliver(sub.target, card, f"{task.key} {event}", stats):
            logger.info("[cmdb_watch] 任务事件 %s %s %s → %s（→ %s）", event,
                        order.key, task.key, task.state_label, sub.cmdb_user)
        else:
            # 没发出去就别记账：下一轮重试，不让一次网络抖动吃掉一条提醒
            self.store.release_notice(key)

    async def _deliver(self, target: str, card: dict[str, Any], title: str,
                       stats: WatchStats) -> bool:
        """发一张卡片。失败只记错误，不抛——一张卡片发不出去不该中断整轮扫描。"""
        if self._record is not None:
            try:
                self._record("card", {"title": title, "card": card, "chat_id": target})
            except Exception:
                logger.debug("[cmdb_watch] record hook 失败", exc_info=True)
        if self._send_card is None:
            logger.warning("[cmdb_watch] send_card 未注入，卡片未发: %s", title)
            return False
        try:
            await self._send_card(target, card)
        except Exception as e:
            stats.cards_failed += 1
            stats.errors.append(f"发卡 {title}: {e}")
            logger.warning("[cmdb_watch] 卡片发送失败 %s → %s: %s", title, target[:10], e)
            return False
        stats.cards_sent += 1
        return True

    # ── 订阅入口（由 handler 调用）───────────────────────────
    async def add_subscriber(self, open_id: str, cmdb_user: str, *,
                             chat_id: str = "", display_name: str = "",
                             kinds: list[str] | None = None) -> Subscriber:
        """建立/改绑订阅。**只登记，不发卡**——发卡由下一轮扫描自然完成。

        为什么不在这里补发摘要：订阅入口已经要回复一条文字回执，再并发推卡片会让
        用户一次收到两条消息；而「已跟踪工单里有我的未执行任务」下一轮扫描本来
        就会发现（工单已跟踪 + 他的 open_id 不在 `new_notified` + 任务未执行 →
        发新工单卡），不需要在这里抢跑。
        """
        await asyncio.to_thread(self.store.load)
        sub = self.store.subscribe(open_id, cmdb_user, chat_id=chat_id,
                                   display_name=display_name, kinds=kinds)
        await asyncio.to_thread(self.store.save)
        return sub

    async def remove_subscriber(self, open_id: str) -> bool:
        await asyncio.to_thread(self.store.load)
        removed = self.store.unsubscribe(open_id)
        if removed:
            await asyncio.to_thread(self.store.save)
        return removed

    def store_tasks_as_tasks(self, state: OrderState) -> list[Task]:
        """把落盘的 TaskState 还原成 Task（查状态/评估用，不含环境等展示字段）。"""
        return [Task(kind=t.kind, task_id=_id_from_key(k), name=t.name,
                     creator=t.creator, state=t.state, state_label=t.label)
                for k, t in state.tasks.items()]

    # ── 指定工单关注 ─────────────────────────────────────────
    async def fetch_order(self, order_id: int, env: str = ENV_PROD) -> Order:
        """读一张工单（Handler 的订阅回执要念工单名与当前进度，不另建客户端）。

        `env` 决定读哪套接口：生产与 UAT 的 order_id 是两条独立序列，缺省按生产。
        """
        return await asyncio.to_thread(self._client.fetch_order, order_id, env)

    async def add_order_watch(self, order_id: int, open_id: str, *,
                              env: str = ENV_PROD,
                              chat_id: str = "", display_name: str = "",
                              order: Order | None = None) -> OrderWatch:
        """关注某张工单的整单进度。

        `order` 可由调用方先拉一次传进来（订阅回执要念工单名与当前进度），
        传 None 时这里自己拉。**登记即取基线**：关注时这张单已经在跑，就不补发
        「开始执行」——用户刚被告知当前进度，再发一张「已开始」只会显得迟钝。

        环境以 `fetched.env` 为准（调用方传进来的 order 自带），避免「用生产接口
        读到了 uat 单还按生产登记」这种错位。
        """
        await asyncio.to_thread(self.store.load)
        fetched = order
        if fetched is None:
            fetched = await asyncio.to_thread(self._client.fetch_order, order_id, env)
        env = normalize_env(fetched.env or env)
        if not fetched.tasks:
            # 拒绝关注空单是错的（单子可能刚建、任务还没填），但要说清楚
            logger.info("[cmdb_watch] 关注 %s：该单尚无任务", fetched.key)
        watch = self.store.add_order_watch(order_id, open_id, env=env,
                                           order_name=fetched.order_name,
                                           chat_id=chat_id)
        watch.order_name = fetched.order_name or watch.order_name
        phase = watch_phase_for_notify(fetched)
        watch.phase = phase
        # 基线规则：已开始的单子不补发「开始」；已完成的单子把两个事件都标成已发，
        # 否则下一轮会把「完成」当成新事件再喊一次。
        watch.notified_started = phase in (PHASE_RUNNING, PHASE_DONE)
        watch.notified_done = phase == PHASE_DONE
        watch.error_rounds = 0
        await asyncio.to_thread(self.store.save)
        return watch

    async def remove_order_watch(self, order_id: int, open_id: str,
                                 env: str = ENV_PROD) -> bool:
        await asyncio.to_thread(self.store.load)
        removed = self.store.remove_order_watch(order_id, open_id, env)
        if removed:
            await asyncio.to_thread(self.store.save)
        return removed

    async def _process_watches(self, order: Order, stats: WatchStats) -> None:
        """推进关注这张单的所有人：跨阶段才发卡，失败单标红。"""
        # 按**工单键**匹配：只比 order_id 会让 UAT 1924 的关注被生产 1924 的观察推进
        watches = [w for w in self.store.order_watches.values()
                   if w.envelope == order.key]
        if not watches:
            return
        for watch in watches:
            watch.error_rounds = 0     # 这一轮读到了，失败计数清零
            watch.order_name = order.order_name or watch.order_name
            if watch.notified_done:
                continue   # 已结束的单子不再重复评估
            event = watch_event(watch.phase, order)
            if not event:
                watch.phase = watch_phase_for_notify(order)
                continue
            key = f"{event}:{watch.envelope}:{watch.open_id}"
            if not self.store.claim_notice(key):
                watch.phase = watch_phase_for_notify(order)
                continue
            card = build_watch_card(order, event, base_url=self._cfg.base_url,
                                    subscriber=watch.open_id, progress=progress_text(order))
            if await self._deliver(watch.target, card, f"关注 {watch.envelope} {event}",
                                   stats):
                watch.phase = watch_phase_for_notify(order)
                if event == "watch_started":
                    watch.notified_started = True
                    watch.started_at = time.time()
                    stats.watches_started += 1
                else:
                    watch.notified_done = True
                    watch.notified_started = True
                    watch.completed_at = time.time()
                    stats.watches_done += 1
                logger.info("[cmdb_watch] 关注工单 %s → %s（%s）",
                            order.key, event, progress_text(order))
            else:
                # 发卡失败：不推进阶段，下一轮重试同一条事件
                self.store.release_notice(key)

    async def _on_watch_error(self, order_key: str, stats: WatchStats,
                              open_id: str = "") -> None:
        """关注工单拉取失败：累计到阈值自动停止，避免对着一张已删单空转。

        计数只在**失败时**累加，任何一次成功观察都会清零（见 `_process_watches`），
        所以偶发抖动不会攒满阈值，只有真删了/真没权限的单子才会被摘掉。

        `open_id` 非空时只处理这个人的关注：50 人订阅后，一次内网抖动会让所有
        关注单同时报错，按人隔离才不会「一次网络问题把所有人的关注全摘掉」。
        """
        changed = False
        for watch in list(self.store.order_watches.values()):
            if watch.envelope != order_key:
                continue
            if open_id and watch.open_id != open_id:
                continue
            watch.error_rounds += 1
            changed = True
            if watch.error_rounds >= self._cfg.watch_error_limit:
                self.store.remove_order_watch(watch.order_id, watch.open_id, watch.env)
                stats.watches_dropped += 1
                logger.warning("[cmdb_watch] 关注 %s 连续 %d 轮拉取失败，已自动停止"
                               "（工单可能已删除或无权限）", order_key, watch.error_rounds)
        if changed:
            logger.debug("[cmdb_watch] %s 拉取失败计数已更新", order_key)

    # ── 观测 ────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        snap = dict(self.stats.__dict__)
        snap.update({
            "active": self.active,
            "interval_sec": self._cfg.interval_sec,
            "refresh_sec": self._cfg.refresh_sec,
            "session": bool(self._client.session_id),
            "poll_orders": len(self.store.needs_polling()),
            "store": self.store.snapshot(),
        })
        snap["errors"] = list(self.stats.errors)[-5:]
        return snap

    async def send_test_card(self, target: str = "", *, cmdb_user: str = "") -> bool:
        """自检：立刻发一张证明链路通的卡片（不看订阅状态）。"""
        from app.domains.cmdb_watch.render import build_test_card

        to = target or (next(iter(self.store.subscribers.values())).target
                        if self.store.subscribers else "")
        if not to:
            raise CmdbError("没有推送目标：请先在对话里订阅，或显式给一个 open_id/chat_id")
        users = [cmdb_user] if cmdb_user else sorted(self.store.watched_users())
        card = build_test_card(base_url=self._cfg.base_url, users=users, chat_hint=to)
        if self._send_card is None:
            raise CmdbError("channel 未就绪，无法发测试卡片")
        await self._send_card(to, card)
        return True


# ── 判定辅助（纯函数，单测直击）───────────────────────────────

def _group_by_subscriber(tasks: list[Task],
                         store: WatchStore) -> list[tuple[Subscriber, list[Task]]]:
    """按订阅者切分任务：每人一份「自己的」任务列表（保序，空列表不出现）。

    类型过滤在这里生效：只订「应用发布」的人不会因为同一张单里还有他的 SQL 任务
    就收到新工单卡——卡的内容就是「他会看到的那几项」。
    """
    out: list[tuple[Subscriber, list[Task]]] = []
    for sub in store.subscribers.values():
        owned = [t for t in tasks if t.creator.strip().lower() == sub.cmdb_key
                 and sub.wants(t.kind)]
        if owned:
            out.append((sub, owned))
    return out


def _bump(stats: WatchStats, event: str) -> None:
    if event == "started":
        stats.tasks_started += 1
    elif event == "failed":
        stats.tasks_failed += 1
    else:
        stats.tasks_done += 1


def _id_from_key(key: str) -> int:
    """`sql:14161` → 14161（轻量轮询重建 Task 用）。"""
    try:
        return int(key.split(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _key(task: Task) -> str:
    return task.key


def _key_sort(order_key: str) -> tuple[int, str]:
    """工单键排序：按 (id, 环境) 降序取「最新的在前」，两个环境混排时顺序稳定。"""
    env, order_id = split_order_key(order_key)
    return (order_id, env)


def _to_state(task: Task) -> TaskState:
    return TaskState(state=task.state, label=task.state_label,
                     creator=task.creator, name=task.name, kind=task.kind)


def _transition(prev: TaskState | None, task: Task) -> str:
    """返回本任务应发的事件名，空串表示不发。

    - `prev is None`：首次出现。若已经在跑/已结束，说明是「跟踪之前就开始了」，
      不发事件（否则重启后所有进行中的任务都会补喊一遍）；未执行的也不发
      （它属于 order_new 那张卡片的内容，不是状态迁移）。
    - 状态没变：不发，只更新基线。
    """
    if prev is None:
        return ""
    if prev.state == task.state:
        return ""
    if task.running:
        return "started"
    if task.done:
        return "failed" if _is_failure(task) else "done"
    return ""


def _is_failure(task: Task) -> bool:
    if task.kind == "sql":
        return task.state in (4, 5)
    if task.kind in ("nacos", "cron"):
        return task.state == 3
    return task.state in (6, 9, -1)
