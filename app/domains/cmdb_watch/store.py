"""CMDB 工单提醒的订阅与观察状态落盘（logs/cmdb_watch/state.json）。

两件事共用一份文件，因为它们的生命周期是绑在一起丢的：

1. **谁在订阅**（`subscribers`）：飞书 open_id → CMDB 用户名 + 推送目标。
   订阅是用户自助建立的（对话里声明自己的 CMDB 用户名），属于「配置」而不是
   「运行时状态」——丢了用户会莫名其妙收不到提醒，所以用原子写保护。
2. **观察到哪儿了**（`scanned_max_id` / `orders` / `notified`）：一张工单只拉一次
   状态、同一事件只提醒一次，重启不能把旧提醒重发一遍。

```
{
  "scanned_max": {"prod": 5917, "uat": 1932},   # 每个环境各自的水位（增量起点）
  "subscribers": {                    # open_id → 订阅记录
     "ou_...": {"cmdb_user": "devuser", "chat_id": "ou_...",
                "since": 1762..., "display_name": "张三",
                "kinds": ["sql", "deploy"]}   # 空/缺省 = 四类全收
  },
  "orders": {                         # 只记「至少一个订阅者命中」的工单
     "prod:5778": {"env": "prod", "order_name": "...", "first_seen": 1762...,
                   "tasks": {"sql:14161": {"state": 1, "creator": "devuser"}}}
  },
  "notified": {"order:prod:5778:ou_...": 1762...,
               "task:uat:1924:sql:3910:done:ou_...": 1762...},
  "order_watches": {                  # 指定工单关注（按单号盯整单进度）
     "prod:5778:ou_...": {"env": "prod", "order_id": 5778, "open_id": "ou_...",
                          "phase": "running", "notified_started": true}
  }
}
```

**所有键都带环境前缀**（`prod:` / `uat:`）：生产与 UAT 是两套独立的 order_id
自增序列，同号的两张单毫无关系。键里不带环境，UAT 1924 会覆盖生产 1924 的
跟踪状态，并把「我的工单」提醒推给盯错单的人。老档案（键里没有冒号）按生产读入。

`notified` 的 key 带订阅者 open_id（任务事件）或工单键（新单事件）：同一张工单
可能命中多个人的任务，每个人都要收到，但每个人只收到一次。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.domains.cmdb_watch.client import ENV_PROD, ENVS, split_order_key
from app.domains.cmdb_watch.client import TASK_KINDS as _KNOWN_KINDS

logger = logging.getLogger("cmdb_watch.store")

PROJECT_DIR = Path(__file__).resolve().parents[3]  # app/domains/cmdb_watch/ -> feishu-agent-platform/
DEFAULT_STATE_PATH = PROJECT_DIR / "logs" / "cmdb_watch" / "state.json"

# 通知记录与工单跟踪的保留期：超过即清（state.json 不该无限长大）
NOTIFIED_KEEP_SEC = 7 * 86400
ORDER_KEEP_SEC = 14 * 86400

# 订阅人数上限：防止误操作（比如脚本刷订阅）把每轮发卡量放大到不可控
MAX_SUBSCRIBERS = 200


def _mask_user(cmdb_user: str) -> str:
    """订阅者用户名脱敏（`devuser` → `zi***g.wang`）：状态快照不是通讯录。"""
    name = (cmdb_user or "").strip()
    if len(name) <= 4:
        return name[:1] + "***"
    return name[:2] + "***" + name[-4:]


@dataclass
class Subscriber:
    """一个订阅者。`chat_id` 是推送目标（open_id 直投私聊，或 oc_ 群）。

    `kinds` 是「按类型过滤」：空集合 = 四类都推（默认，与旧行为一致）；
    非空 = 只推这些类型（sql / deploy / nacos / cron）。按类型过滤落在**推送判定**
    而不是取数上——任务清单反正已经在手上，少发一张卡比少拉一张单安全得多
    （漏拉会导致「状态迁移看不出来」，少推只是少打扰）。
    """

    open_id: str
    cmdb_user: str
    chat_id: str = ""
    display_name: str = ""
    since: float = field(default_factory=time.time)
    kinds: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        """推送目标：显式 chat_id 优先，否则直投私聊（open_id）。"""
        return self.chat_id or self.open_id

    @property
    def cmdb_key(self) -> str:
        """CMDB 用户名比较键（大小写不敏感全等匹配用）。"""
        return self.cmdb_user.strip().lower()

    def wants(self, kind: str) -> bool:
        """这条任务类型要不要推给他（空 kinds = 全收）。"""
        return not self.kinds or kind in self.kinds


@dataclass
class TaskState:
    """上一轮见到的任务状态（用于判定迁移事件）。"""

    state: int = 0
    label: str = ""
    creator: str = ""
    name: str = ""
    kind: str = ""

    @property
    def done(self) -> bool:
        """是否已到终态（与 Task.done 同口径，见 client.classify）。

        判定从「上一轮基线」里读：一旦任务收口，它就不必再进每轮轮询预算。
        """
        from app.domains.cmdb_watch.client import classify

        return classify(self.kind or "sql", self.state) == "done"


@dataclass
class OrderState:
    """一张被跟踪工单的观察记录。"""

    order_id: int
    order_name: str = ""
    env: str = ENV_PROD
    first_seen: float = field(default_factory=time.time)
    tasks: dict[str, TaskState] = field(default_factory=dict)
    # 新工单卡片没发出去（网络/飞书抖动）。保留工单在跟踪集合里、下一轮重试；
    # 不标这个标记的话发卡失败就只能等下辈子——候选集合不会再回看它。
    order_notice_pending: bool = False
    # 已经发过「新工单」卡片的 open_id 集合（同一张单命中多人时各发各的，
    # 但每个人只发一次；发失败的从集合里剔除，下一轮重试）
    new_notified: list[str] = field(default_factory=list)

    def open_tasks(self) -> list[TaskState]:
        """还没到终态的任务（「这张单还值得每轮问一次吗」的判据）。"""
        return [t for t in self.tasks.values() if not t.done]

    @property
    def key(self) -> str:
        """跨环境唯一键 `prod:5784` / `uat:1924`（落盘与去重都用它）。"""
        return f"{self.env or ENV_PROD}:{self.order_id}"

    def poll_worthy(self) -> bool:
        """是否还值得每轮问一次：有任务没收口（或新单卡还没发出去）。

        判据按「任务级未结束」而不是「工单 lock」或「deploy 未结束」：
        - 用 lock：SQL 单会一直被算成活着（lock 只对 deploy 有意义）；
        - 用 deploy：纯 SQL 单在 SQL 还没跑时就退出预算，完成提醒只能等
          `refresh_sec` 校准，用户感知是「提醒比人慢半拍」。
        代价是纯 SQL 单在 SQL 未结束期间每轮多问一次 <1KB 的轻量请求，可忽略。

        这条过滤是「请求数不随订阅人数线性爆炸」的主要支点：订阅涨到 50 人，
        跟踪的工单会长，但**同时还有事没做完**的工单不会跟着涨。
        """
        return self.order_notice_pending or bool(self.open_tasks())


# 工单整体执行阶段（按任务状态聚合，见 watcher.order_phase）
PHASE_PENDING = "pending"
PHASE_RUNNING = "running"
PHASE_DONE = "done"


@dataclass
class OrderWatch:
    """一个人对**某张指定工单**的关注（「帮我盯一下 5778 这张单」）。

    与 `Subscriber` 的分工：`Subscriber` 是「按姓名长期关注我自己的任务」，
    这里是「按单号一次性关注整张单的执行进度」，两者可以同时存在——
    同一个人对同一张单可能既收到「我的任务开始」也收到「整单完成」。
    """

    order_id: int
    open_id: str
    order_name: str = ""
    chat_id: str = ""
    env: str = ENV_PROD
    added_at: float = field(default_factory=time.time)
    # 上一轮观察到的整单阶段；None 表示还没观察过（首次轮询时直接取当前值做基线）
    phase: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    # 已发过的事件标记：开始/完成各只发一次，重启不重发
    notified_started: bool = False
    notified_done: bool = False
    # 连续拉取失败次数（工单被删/CMDB 抖动）：超过阈值自动停止关注
    error_rounds: int = 0

    @property
    def envelope(self) -> str:
        """工单键 `prod:5778` / `uat:1924`（与 OrderState.key 同形）。"""
        return f"{self.env or ENV_PROD}:{self.order_id}"

    @property
    def key(self) -> str:
        """`prod:5778:ou_...`——环境必须在键里，否则 UAT 1924 与生产 1924 互撞。"""
        return f"{self.envelope}:{self.open_id}"

    @property
    def target(self) -> str:
        return self.chat_id or self.open_id


class WatchStore:
    """state.json 读写。同步 IO（调用方在 to_thread 里调），单线程使用。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_STATE_PATH
        # 每个环境一条独立水位线：两套 order_id 空间互不相干，共用一个水位会让
        # UAT 的 1932 把生产的 5917 水位拉回去（或相反），之后每天全量重扫。
        self.scanned_max: dict[str, int] = {env: 0 for env in ENVS}
        # 键是 `prod:5784` / `uat:1924`（见文件抬头：两套编号空间重叠）
        self.orders: dict[str, OrderState] = {}
        self.notified: dict[str, float] = {}
        self.subscribers: dict[str, Subscriber] = {}
        # key = f"{环境}:{order_id}:{open_id}"：同一张单可被多人关注，同一人可关注多张单
        self.order_watches: dict[str, OrderWatch] = {}

    # ── 水位线（按环境）────────────────────────────────────
    @property
    def scanned_max_id(self) -> int:
        """生产水位（历史调用方的简称，等价 `scanned_max[ENV_PROD]`）。"""
        return self.scanned_max.get(ENV_PROD, 0)

    @scanned_max_id.setter
    def scanned_max_id(self, value: int) -> None:
        self.scanned_max[ENV_PROD] = int(value)

    def watermark(self, env: str = ENV_PROD) -> int:
        return self.scanned_max.get(env, 0)

    def advance_watermark(self, env: str, order_id: int) -> None:
        """只抬不降：并发/乱序拿到旧批次时不把水位线拉回去。"""
        self.scanned_max[env] = max(self.scanned_max.get(env, 0), int(order_id))

    def reset_watermarks(self) -> None:
        """清空所有水位（重扫历史用；`_seed` 之外的调用方不该碰）。"""
        self.scanned_max = {env: 0 for env in ENVS}

    # ── 读写 ────────────────────────────────────────────────
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            # 状态坏了就重来：宁可多发一次提醒，也不能因为一个坏文件永久停摆
            logger.warning("[cmdb_watch] 状态文件损坏，重建: %s", e)
            return
        if not isinstance(raw, dict):
            return
        # 水位：新版是 {"prod": N, "uat": M}；老档案是裸整数 scanned_max_id（只统计生产）
        scanned = raw.get("scanned_max")
        if isinstance(scanned, dict):
            self.scanned_max = {env: int(scanned.get(env) or 0) for env in ENVS}
        else:
            self.scanned_max = {env: 0 for env in ENVS}
            self.scanned_max_id = int(raw.get("scanned_max_id") or 0)
        self.subscribers = {}
        for open_id, item in (raw.get("subscribers") or {}).items():
            if not isinstance(item, dict):
                continue
            cmdb_user = str(item.get("cmdb_user") or "").strip()
            if not cmdb_user:
                continue
            self.subscribers[str(open_id)] = Subscriber(
                open_id=str(open_id),
                cmdb_user=cmdb_user,
                chat_id=str(item.get("chat_id") or ""),
                display_name=str(item.get("display_name") or ""),
                since=float(item.get("since") or 0),
                kinds=[str(k) for k in (item.get("kinds") or [])
                       if str(k) in _KNOWN_KINDS],
            )
        self.orders = {}
        for raw_key, item in (raw.get("orders") or {}).items():
            if not isinstance(item, dict):
                continue
            env, oid = split_order_key(raw_key)
            try:
                order = OrderState(order_id=oid,
                                   env=env,
                                   order_name=str(item.get("order_name") or ""),
                                   first_seen=float(item.get("first_seen") or 0),
                                   order_notice_pending=bool(
                                       item.get("order_notice_pending") or False),
                                   new_notified=[str(x) for x in
                                                 (item.get("new_notified") or [])])
            except (TypeError, ValueError):
                continue
            for key, task in (item.get("tasks") or {}).items():
                if not isinstance(task, dict):
                    continue
                order.tasks[str(key)] = TaskState(
                    state=int(task.get("state") or 0),
                    label=str(task.get("label") or ""),
                    creator=str(task.get("creator") or ""),
                    name=str(task.get("name") or ""),
                    kind=str(task.get("kind") or key.split(":", 1)[0]),
                )
            self.orders[order.key] = order
        self.notified = {str(k): float(v) for k, v in (raw.get("notified") or {}).items()
                         if isinstance(v, (int, float))}
        self.order_watches = {}
        for item in (raw.get("order_watches") or {}).values():
            if not isinstance(item, dict):
                continue
            try:
                watch = OrderWatch(
                    order_id=int(item.get("order_id") or 0),
                    open_id=str(item.get("open_id") or ""),
                    order_name=str(item.get("order_name") or ""),
                    chat_id=str(item.get("chat_id") or ""),
                    env=str(item.get("env") or ENV_PROD),
                    added_at=float(item.get("added_at") or 0),
                    phase=str(item.get("phase") or ""),
                    started_at=float(item.get("started_at") or 0),
                    completed_at=float(item.get("completed_at") or 0),
                    notified_started=bool(item.get("notified_started") or False),
                    notified_done=bool(item.get("notified_done") or False),
                    error_rounds=int(item.get("error_rounds") or 0),
                )
            except (TypeError, ValueError):
                continue
            if watch.order_id and watch.open_id:
                self.order_watches[watch.key] = watch

    def save(self) -> None:
        """原子写：先写临时文件再 replace，进程中途被 kill 也不会留半截 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scanned_max": dict(self.scanned_max),
            "subscribers": {open_id: asdict(sub)
                            for open_id, sub in self.subscribers.items()},
            "orders": {o.key: {
                "order_id": o.order_id,
                "env": o.env,
                "order_name": o.order_name,
                "first_seen": o.first_seen,
                "order_notice_pending": o.order_notice_pending,
                "new_notified": list(o.new_notified),
                "tasks": {k: asdict(v) for k, v in o.tasks.items()},
            } for o in self.orders.values()},
            "notified": self.notified,
            "order_watches": {k: asdict(w) for k, w in self.order_watches.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 订阅 ────────────────────────────────────────────────
    def subscribe(self, open_id: str, cmdb_user: str, *, chat_id: str = "",
                  display_name: str = "",
                  kinds: list[str] | None = None) -> Subscriber:
        """建/改订阅。同一 open_id 重复订阅 = 改绑 CMDB 用户名（幂等）。

        `kinds=None` 表示「这次不改过滤条件」（改绑用户名不该把过滤重置掉）；
        `kinds=[]` 明确表示「全收」。传了非法类型直接丢弃，不留看不见的脏值。
        """
        if open_id not in self.subscribers and len(self.subscribers) >= MAX_SUBSCRIBERS:
            raise ValueError(f"订阅人数已达上限 {MAX_SUBSCRIBERS}，请联系管理员清理")
        existing = self.subscribers.get(open_id)
        sub = Subscriber(
            open_id=open_id,
            cmdb_user=cmdb_user.strip(),
            chat_id=chat_id.strip() or (existing.chat_id if existing else ""),
            display_name=display_name.strip() or (existing.display_name if existing else ""),
            since=existing.since if existing else time.time(),
            kinds=([k for k in kinds if k in _KNOWN_KINDS]
                   if kinds is not None else (list(existing.kinds) if existing else [])),
        )
        self.subscribers[open_id] = sub
        return sub

    def unsubscribe(self, open_id: str) -> bool:
        return self.subscribers.pop(open_id, None) is not None

    def watched_users(self) -> set[str]:
        """所有订阅的 CMDB 用户名（小写比较键）——拉单时用它做任务过滤。"""
        return {s.cmdb_key for s in self.subscribers.values() if s.cmdb_key}

    def subscribers_for(self, creators: set[str], *, kind: str = "") -> list[Subscriber]:
        """→ 这些 CMDB 用户名对应的订阅者（按 open_id 去重）。

        一个 CMDB 用户名可能被多个飞书账号订阅（换号、同事代收），都要发。
        `kind` 给定时再按订阅者的类型过滤剪一遍（`Subscriber.kinds`）。
        """
        wanted = {c.strip().lower() for c in creators if c.strip()}
        return [s for s in self.subscribers.values()
                if s.cmdb_key in wanted and (not kind or s.wants(kind))]

    # ── 指定工单关注 ────────────────────────────────────────
    def add_order_watch(self, order_id: int, open_id: str, *, env: str = ENV_PROD,
                        order_name: str = "", chat_id: str = "") -> OrderWatch:
        """关注某张工单（同一人重复关注 = 幂等，只更新推送目标）。

        `env` 必须与「读这张单时用的环境」一致——两套编号空间重叠，同号不同单。
        """
        key = f"{env or ENV_PROD}:{order_id}:{open_id}"
        existing = self.order_watches.get(key)
        if existing is not None:
            if chat_id:
                existing.chat_id = chat_id
            if order_name:
                existing.order_name = order_name
            return existing
        watch = OrderWatch(order_id=order_id, open_id=open_id, env=env or ENV_PROD,
                           order_name=order_name, chat_id=chat_id)
        self.order_watches[key] = watch
        return watch

    def remove_order_watch(self, order_id: int, open_id: str,
                           env: str = ENV_PROD) -> bool:
        return self.order_watches.pop(
            f"{env or ENV_PROD}:{order_id}:{open_id}", None) is not None

    def watched_order_ids(self, open_id: str = "") -> set[str]:
        """关注中的工单键 `prod:5778` / `uat:1924`（可按人过滤）。

        返回**带环境的键**而不是裸 id：调用方要拿它去 fetch，猜环境就等于查错单。

        全局口径（不传 open_id）给轮询用：谁关注的单都得每轮全量读。
        按人口径在通道出错时用：只摘掉**这个人**的关注，别人的关注照样有效——
        内网 CMDB 是同一台机器在访问，一次网络抖动不该把所有人的关注一起清掉。
        """
        return {w.envelope for w in self.order_watches.values()
                if not w.notified_done and (not open_id or w.open_id == open_id)}

    def order_watches_for(self, open_id: str) -> list[OrderWatch]:
        return [w for w in self.order_watches.values() if w.open_id == open_id]

    # ── 通知去重 ─────────────────────────────────────────────
    def claim_notice(self, key: str) -> bool:
        """尝试占用一条通知额度。已发过返回 False（本轮不再发）。"""
        if key in self.notified:
            return False
        self.notified[key] = time.time()
        return True

    def release_notice(self, key: str) -> None:
        """发卡失败时回滚额度，让下一轮能重试。"""
        self.notified.pop(key, None)

    # ── 跟踪集合 ─────────────────────────────────────────────
    def tracked_orders(self) -> list[OrderState]:
        return list(self.orders.values())

    def needs_polling(self) -> list[OrderState]:
        """还值得每轮轮询的工单：deploy 还没到终态的，以及「新工单卡还没发出去」的。

        轻量接口只回 deploy 状态（实测），所以纯 SQL / NACOS / 定时任务单每轮问它
        一次是纯浪费——那些单的完成提醒本来就走 `refresh_sec` 全量校准，两处口径
        必须一致。已收口的工单更不必问：状态不会再变。

        这条过滤是「请求数不随订阅人数线性爆炸」的主要支点：订阅人数从 1 涨到 50，
        跟踪的工单会长，但**同时还有 deploy 在跑**的工单几乎不涨。
        """
        return [o for o in self.orders.values() if o.poll_worthy()]

    def all_open_order_keys(self) -> set[str]:
        """所有「还有未结束任务」的工单键（新单发现时的碰撞过滤用）。

        比 `needs_polling` 宽：纯 SQL 单虽然不进轻量轮询，但它仍然在被跟踪，
        发现阶段不该把它当成「新工单」再全量读一遍。
        """
        return {o.key for o in self.orders.values() if o.poll_worthy()}

    def upsert_order(self, order_id: int, order_name: str,
                     tasks: dict[str, TaskState],
                     env: str = ENV_PROD) -> OrderState:
        key = f"{env or ENV_PROD}:{order_id}"
        state = self.orders.get(key)
        if state is None:
            state = OrderState(order_id=order_id, env=env or ENV_PROD,
                               order_name=order_name)
            self.orders[key] = state
        state.order_name = order_name or state.order_name
        state.tasks.update(tasks)
        return state

    def prune(self, now: float | None = None) -> None:
        """清过期工单与通知记录（保留期见常量）。

        `order_watches` 不清：那是用户显式下的关注指令，过期自动消失等于「默默不提醒」。
        结束条件只有两个——用户自己退订，或工单连续拉取失败到阈值（watcher 里处理）。
        """
        now = now or time.time()
        stale = [oid for oid, o in self.orders.items()
                 if now - (o.first_seen or 0) > ORDER_KEEP_SEC]
        for oid in stale:
            self.orders.pop(oid, None)
        self.notified = {k: v for k, v in self.notified.items()
                         if now - v <= NOTIFIED_KEEP_SEC}
        if stale:
            logger.info("[cmdb_watch] 清理过期跟踪工单 %d 张", len(stale))

    def snapshot(self) -> dict[str, Any]:
        return {
            "scanned_max": dict(self.scanned_max),
            "tracked_orders": len(self.orders),
            "tracked_by_env": {env: sum(1 for o in self.orders.values()
                                       if (o.env or ENV_PROD) == env) for env in ENVS},
            "notified_records": len(self.notified),
            "order_watches": len(self.order_watches),
            # 订阅者明细按人脱敏：控制台/状态回执只该看到「谁订了什么」，
            # 不该把整份 open_id 名单摊给调用方（50 人之后这就是一份通讯录）。
            "subscribers": [{"open_id": s.open_id[-6:], "cmdb_user": _mask_user(s.cmdb_user),
                             "display_name": s.display_name, "since": s.since,
                             "kinds": list(s.kinds)}
                            for s in self.subscribers.values()],
            "orders": [{"order_id": o.order_id, "env": o.env or ENV_PROD,
                        "order_name": o.order_name,
                        "first_seen": o.first_seen, "tasks": len(o.tasks),
                        "notice_pending": o.order_notice_pending}
                       for o in self.orders.values()],
            "watches": [{"order_id": w.order_id, "env": w.env or ENV_PROD,
                         "open_id": w.open_id[-6:],
                         "order_name": w.order_name, "phase": w.phase,
                         "notified_done": w.notified_done}
                        for w in self.order_watches.values()],
        }
