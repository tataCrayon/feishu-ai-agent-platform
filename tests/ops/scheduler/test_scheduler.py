"""定时任务调度单测：评估红线 / 状态机 / 调度排期 / 执行统计回写。

运行：python -m pytest tests/ops/scheduler/test_scheduler.py -q
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.ops.scheduler.evaluator import ScheduledTaskEvaluator
from app.ops.scheduler.executor import ScheduledTaskExecutor
from app.ops.scheduler.loop import (
    MIN_SCAN_INTERVAL_SEC,
    ScheduledTaskLoop,
    compute_next_run,
)
from app.ops.scheduler.runs import RunRecorder, TaskRunRecord, run_recorder
from app.ops.scheduler.store import (
    MIN_INTERVAL_MINUTES,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PAUSED,
    STATUS_REJECTED,
    ScheduledTask,
    ScheduledTaskStore,
)

TZ = timezone(timedelta(hours=8))


def _task(store: ScheduledTaskStore, **kw) -> ScheduledTask:
    defaults: dict = dict(
        instruction="总结日报",
        agent="dev-bot",
        target_type="group",
        target_id="oc_test",
        target_name="测试群",
        schedule_kind="every",
        interval_minutes=30,
    )
    defaults.update(kw)
    return store.new_task(**defaults)


# ── 评估器：能力面红线 ────────────────────────────────────────────────


def test_eval_accepts_valid_task(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    report = ScheduledTaskEvaluator(["dev-bot"]).evaluate(t)
    assert report.ok, report.blocks


def test_eval_rejects_interval_below_10min(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store, interval_minutes=5)
    assert t.validate_schedule()  # store 层校验也要拦
    report = ScheduledTaskEvaluator(["dev-bot"]).evaluate(t)
    assert not report.ok
    assert any("10 分钟" in b for b in report.blocks)


def test_eval_rejects_unknown_agent(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store, agent="not-a-real-agent")
    report = ScheduledTaskEvaluator(["dev-bot"]).evaluate(t)
    assert not report.ok


def test_eval_rejects_non_session_target(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store, target_type="", target_id="张三")
    report = ScheduledTaskEvaluator(["dev-bot"]).evaluate(t)
    assert not report.ok
    assert any("oc_" in b for b in report.blocks)


def test_min_interval_constant():
    assert MIN_INTERVAL_MINUTES == 10
    assert MIN_SCAN_INTERVAL_SEC == 600


# ── Store：目录即状态 ─────────────────────────────────────────────────


def test_store_status_transitions(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    # 创建即生效：审批门下线后新档案直接落 approved/，由调度循环拾取
    assert t.status == STATUS_APPROVED

    store.update(t, STATUS_PAUSED)
    assert store.get(t.task_id).status == STATUS_PAUSED

    store.archive(t, "过期归档")
    got = store.get(t.task_id)
    assert got.status == STATUS_DONE
    assert got.eval_reason == "过期归档"


def test_store_reject_migration(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_REJECTED)
    assert (tmp_path / "rejected" / t.task_id / "manifest.json").exists()


def test_store_pause_resume_cancel(tmp_path):
    """生命周期：approved ⇄ paused，cancelled 终态。"""
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_APPROVED)

    store.update(t, STATUS_PAUSED)
    assert (tmp_path / "paused" / t.task_id / "manifest.json").exists()
    assert store.get(t.task_id).status == STATUS_PAUSED

    store.update(t, STATUS_APPROVED)
    assert store.get(t.task_id).status == STATUS_APPROVED

    store.cancel(t, reason="测试取消")
    got = store.get(t.task_id)
    assert got.status == STATUS_CANCELLED
    assert got.eval_reason == "测试取消"
    assert (tmp_path / "cancelled" / t.task_id / "manifest.json").exists()


# ── 调度排期计算 ──────────────────────────────────────────────────────


def test_compute_next_run_every():
    now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
    t = ScheduledTask(task_id="t", schedule_kind="every", interval_minutes=30)
    ts = compute_next_run(t, now)
    assert datetime.fromtimestamp(ts, TZ) - now == timedelta(minutes=30)


def test_compute_next_run_every_catches_up_from_now():
    """停机欠下的轮次不补发：排期落在过去就改成 now + interval。

    按「上次 + interval」一路累加，停机 2 小时的 30 分钟任务会在接下来每轮扫描
    都触发一次，群里看到同一份摘要连刷 4 遍。原生 Schedule 的语义也是
    「只补最近一次」，这里对齐它。
    """
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=TZ)
    t = ScheduledTask(task_id="t", schedule_kind="every", interval_minutes=30)
    t.next_run_ts = (now - timedelta(hours=2)).timestamp()
    ts = compute_next_run(t, now)
    assert datetime.fromtimestamp(ts, TZ) - now == timedelta(minutes=30)


def test_compute_next_run_every_keeps_cadence_when_on_time():
    """没有落后时保持原节拍（不是每轮都从现在起算），否则会越漂越晚。"""
    now = datetime(2026, 9, 14, 10, 20, 0, tzinfo=TZ)
    t = ScheduledTask(task_id="t", schedule_kind="every", interval_minutes=30)
    t.next_run_ts = datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ).timestamp()
    ts = compute_next_run(t, now)
    assert datetime.fromtimestamp(ts, TZ) == datetime(2026, 9, 14, 10, 30, tzinfo=TZ)


def test_compute_next_run_daily_future():
    now = datetime(2026, 9, 14, 8, 0, 0, tzinfo=TZ)
    t = ScheduledTask(task_id="t", schedule_kind="daily", daily_time="09:00")
    ts = compute_next_run(t, now)
    fired = datetime.fromtimestamp(ts, TZ)
    assert (fired - now) == timedelta(hours=1)


def test_compute_next_run_daily_past_rolls_tomorrow():
    now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
    t = ScheduledTask(task_id="t", schedule_kind="daily", daily_time="09:00")
    ts = compute_next_run(t, now)
    fired = datetime.fromtimestamp(ts, TZ)
    assert fired.date() == now.date() + timedelta(days=1)
    assert fired.hour == 9


# ── 调度循环：扫描触发 + 过期归档 ─────────────────────────────────────


class _FakeOrchestrator:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, message: str, **kw) -> str:
        self.calls.append(message)
        return f"done: {message[:10]}"


class _FakeDshGateway:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def handle(self, message: str, chat_id: str, **kw) -> str:
        self.calls.append((message, chat_id))
        return f"dsh: {message[:10]}"


class _FakeChannel:
    def __init__(self, success: bool = True):
        self.sent: list[tuple[str, str]] = []
        self.success = success

    async def send(self, chat_id: str, text: str):
        from lark_channel.channel.types import SendError, SendResult

        self.sent.append((chat_id, text))
        if self.success:
            return SendResult(success=True, message_id="om_test")
        return SendResult(
            success=False,
            error=SendError(code="FORBIDDEN", retryable=False, hint="bot is not in this chat"),
        )


def test_executor_prefers_dsh_over_legacy_orchestrator(tmp_path):
    """dsh 启用后定时任务必须走 dsh：两条链路的工具表与技能目录不同，
    走旧编排器会让「群里手动发」和「到点推送」得到两套结果。"""
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)

    executor = ScheduledTaskExecutor(store)
    orch, dsh, ch = _FakeOrchestrator(), _FakeDshGateway(), _FakeChannel()
    executor.wire(orch, ch, dsh_gateway=dsh)

    assert asyncio.run(executor.execute(t)) is True
    assert dsh.calls and dsh.calls[0][1] == t.target_id
    assert orch.calls == []          # 回落链路一次都没被调
    assert ch.sent[0][1].startswith("dsh:")


def test_executor_falls_back_to_orchestrator_without_dsh(tmp_path):
    """dsh 未启用（回切场景）时仍要能跑，不能因为 gateway 缺席就记失败。"""
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)

    executor = ScheduledTaskExecutor(store)
    orch, ch = _FakeOrchestrator(), _FakeChannel()
    executor.wire(orch, ch, dsh_gateway=None)

    assert asyncio.run(executor.execute(t)) is True
    assert len(orch.calls) == 1


def test_loop_fires_due_task(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_APPROVED)
    # 直接把 next_run_ts 设到过去 → 本次扫描即触发
    store.update(t)
    t.next_run_ts = 1.0
    store.update(t)

    executor = ScheduledTaskExecutor(store)
    orch, ch = _FakeOrchestrator(), _FakeChannel()
    executor.wire(orch, ch)

    loop = ScheduledTaskLoop(store, executor, scan_interval_sec=600)
    asyncio.get_event_loop_policy()
    asyncio.run(loop._scan_once())
    # 给 fire-and-forget 一点时间
    asyncio.run(asyncio.sleep(0.1))

    assert len(orch.calls) == 1
    assert ch.sent and ch.sent[0][0] == "oc_test"
    got = store.get(t.task_id)
    assert got.run_count == 1
    assert got.last_run_ok


def test_loop_skips_task_still_running(tmp_path):
    """上一轮还在跑就别再起一轮：两次执行抢同一个 dsh 会话，产出顺序与语义都不对。"""
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_APPROVED)
    t.next_run_ts = 1.0
    store.update(t)

    started = 0

    class _BlockingExecutor:
        """模拟执行时长超过一个扫描周期：返回的 Task 一直不 done。"""

        def execute_in_background(self, task):
            nonlocal started
            started += 1
            return asyncio.ensure_future(asyncio.sleep(3600))

    loop = ScheduledTaskLoop(store, _BlockingExecutor(), scan_interval_sec=600)

    async def _run():
        await loop._scan_once()          # 第 1 轮：正常触发
        # 排期推进到未来；把它再拨回过去模拟「又到点」，但任务仍在跑
        task = store.get(t.task_id)
        task.next_run_ts = 1.0
        store.update(task)
        await loop._scan_once()          # 第 2 轮：应被 in-flight 拦住
        return started

    fired = asyncio.run(_run())
    assert fired == 1, "同一任务不应并发触发两次"


def test_executor_marks_failure_when_send_rejected(tmp_path):
    """飞书拒收走 `success=False` 不抛异常：不能记成成功。

    只看「没抛」会让机器人被移出群之后，运行历史全绿而群里一个字都没收到。
    """
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)

    executor = ScheduledTaskExecutor(store)
    executor.wire(_FakeOrchestrator(), _FakeChannel(success=False))

    assert asyncio.run(executor.execute(t)) is False
    got = store.get(t.task_id)
    assert got.run_count == 1 and not got.last_run_ok
    assert "FORBIDDEN" in got.last_error


def test_loop_archives_expired(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store, expire_date="2026-01-01")
    store.update(t, STATUS_APPROVED)

    executor = ScheduledTaskExecutor(store)
    executor.wire(_FakeOrchestrator(), _FakeChannel())
    loop = ScheduledTaskLoop(store, executor, scan_interval_sec=600)
    asyncio.run(loop._scan_once())

    assert store.get(t.task_id).status == STATUS_DONE


def test_loop_respects_min_scan_interval(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    loop = ScheduledTaskLoop(store, ScheduledTaskExecutor(store), scan_interval_sec=60)
    assert loop._interval == 600


# ── 执行器：编排未就绪时不丢档案 ──────────────────────────────────────


def test_executor_records_failure_without_wire(tmp_path):
    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_APPROVED)

    executor = ScheduledTaskExecutor(store)
    ok = asyncio.run(executor.execute(t))
    assert not ok
    got = store.get(t.task_id)
    assert got.run_count == 1 and not got.last_run_ok
    assert got.last_error


# ── 执行历史（RunRecorder）────────────────────────────────────────────


def test_run_recorder_write_and_read(tmp_path):
    """逐次执行落盘 + 按 task_id 过滤读取（新→旧）。"""
    rec = RunRecorder(log_dir=tmp_path / "runs")

    rec.write(TaskRunRecord(task_id="t1", outcome="ok", started_at="2026-09-15 09:00:00"))
    rec.write(TaskRunRecord(task_id="t1", outcome="fail", error="超时", started_at="2026-09-15 10:00:00"))
    rec.write(TaskRunRecord(task_id="t2", outcome="ok", started_at="2026-09-15 10:00:00"))

    all_t1 = rec.read_recent(days=7, task_id="t1")
    assert len(all_t1) == 2
    assert all_t1[0]["started_at"] == "2026-09-15 10:00:00"  # 新→旧
    assert all_t1[0]["outcome"] == "fail"
    assert len(rec.read_recent(days=7)) == 3
    # 截断字段生效
    rec.write(TaskRunRecord(task_id="t3", instruction="x" * 500, error="e" * 500))
    got = rec.read_recent(days=7, task_id="t3")[0]
    assert len(got["instruction"]) == 120 and len(got["error"]) == 200


def test_run_recorder_read_missing_dir(tmp_path, monkeypatch):
    """空目录/坏行容错：不抛出返回空。"""
    assert RunRecorder(log_dir=tmp_path / "nonexistent").read_recent(days=1) == []
    rec = RunRecorder(log_dir=tmp_path / "runs")
    assert rec.read_recent(days=1) == []
    d = tmp_path / "runs"
    d.mkdir()
    (d / "2026-09-15.jsonl").write_text(
        "not-json\n{\"task_id\": \"t1\", \"started_at\": \"2026-09-15 00:00:00\"}\n",
        encoding="utf-8",
    )
    # read_recent 按日历日（UTC+8）扫描，日期跨天后回退读「今天」对齐容器的 TZ
    import app.ops.scheduler.runs as _runs_mod

    real_now = _runs_mod.datetime
    class _FixedDT(real_now):
        @classmethod
        def now(cls, tz=None):
            return real_now(2026, 9, 15, 12, 0, 0, tzinfo=TZ)
    monkeypatch.setattr(_runs_mod, "datetime", _FixedDT)
    out = rec.read_recent(days=1)
    assert len(out) == 1 and out[0]["task_id"] == "t1"


def test_executor_writes_run_record(tmp_path, monkeypatch):
    """executor 执行后须落一条 run（成败都落），全局 run_recorder 指向临时目录。"""
    monkeypatch.setattr(run_recorder, "_dir", tmp_path / "runs")

    store = ScheduledTaskStore(tmp_path)
    t = _task(store)
    store.update(t, STATUS_APPROVED)

    executor = ScheduledTaskExecutor(store)
    orch, ch = _FakeOrchestrator(), _FakeChannel()
    executor.wire(orch, ch)
    ok = asyncio.run(executor.execute(t))
    assert ok

    runs = run_recorder.read_recent(days=1, task_id=t.task_id)
    assert len(runs) == 1
    assert runs[0]["outcome"] == "ok"
    assert runs[0]["trace_id"].startswith(f"sched-{t.task_id}")


# ── Handler 生命周期工具：权限 + 状态机 ───────────────────────────────


def _make_handler(store: ScheduledTaskStore):
    from types import SimpleNamespace

    from app.agents.handlers.schedule_handler import ScheduleAgentHandler

    # 绕开基类 __init__（它会建 openai client + 加载 prompt），
    # handler 的工具实现只用到 _config/_service/_system_prompt 三个属性
    handler = ScheduleAgentHandler.__new__(ScheduleAgentHandler)
    handler._config = SimpleNamespace()
    handler._client = None
    handler._system_prompt = ""
    handler._prompt_version = "test"
    handler._service = SimpleNamespace(_store=store)
    return handler


def test_handler_transition_requires_creator_or_l2(tmp_path, monkeypatch):
    """非创建人且非 L2 → 拒绝；创建人本人 → 通过。"""
    from app.orchestrator.context import set_caller_context

    store = ScheduledTaskStore(tmp_path)
    t = _task(store, creator_id="ou_owner")
    store.update(t, STATUS_APPROVED)
    handler = _make_handler(store)

    # 他人（非 L2）暂停 → 拒绝
    set_caller_context({"sender_id": "ou_stranger", "chat_id": "oc_x"})
    r = asyncio.run(handler.execute_tool("pause_task", {"task_id": t.task_id}))
    assert r.error and "L2" in r.error
    assert store.get(t.task_id).status == STATUS_APPROVED

    # 创建人本人暂停 → 通过
    set_caller_context({"sender_id": "ou_owner", "chat_id": "oc_x"})
    r = asyncio.run(handler.execute_tool("pause_task", {"task_id": t.task_id}))
    assert not r.error, r.error
    assert store.get(t.task_id).status == STATUS_PAUSED


def test_handler_transition_state_machine(tmp_path):
    """状态机：resume 只认 paused，cancel 认 approved/paused，终态不可再动。"""
    from app.orchestrator.context import set_caller_context

    store = ScheduledTaskStore(tmp_path)
    t = _task(store, creator_id="ou_owner")
    store.update(t, STATUS_APPROVED)
    handler = _make_handler(store)
    set_caller_context({"sender_id": "ou_owner", "chat_id": "oc_x"})

    # pending 直接恢复 → 拒绝
    r = asyncio.run(handler.execute_tool("resume_task", {"task_id": t.task_id}))
    assert r.error and "恢复" in r.error

    # approve → pause → cancel → 再取消失败
    asyncio.run(handler.execute_tool("pause_task", {"task_id": t.task_id}))
    assert store.get(t.task_id).status == STATUS_PAUSED
    r = asyncio.run(handler.execute_tool("cancel_task", {"task_id": t.task_id}))
    assert not r.error, r.error
    assert store.get(t.task_id).status == STATUS_CANCELLED
    r = asyncio.run(handler.execute_tool("cancel_task", {"task_id": t.task_id}))
    assert r.error  # 终态不可再取消


def test_handler_get_task_and_list_filters(tmp_path, monkeypatch):
    """get_task 返回详情含执行记录；list_tasks 支持 paused/cancelled 过滤。"""

    from app.orchestrator.context import set_caller_context

    monkeypatch.setattr(run_recorder, "_dir", tmp_path / "runs")
    run_recorder.write(TaskRunRecord(
        task_id="task-x", outcome="fail", error="超时了",
        started_at="2026-09-15 09:00:00"))

    store = ScheduledTaskStore(tmp_path)
    t = _task(store, creator_id="ou_owner")
    store.update(t, STATUS_PAUSED)
    handler = _make_handler(store)
    set_caller_context({"sender_id": "ou_owner", "chat_id": "oc_x"})

    r = asyncio.run(handler.execute_tool("get_task", {"task_id": t.task_id}))
    assert not r.error
    assert "已暂停" in r.data
    r = asyncio.run(handler.execute_tool("get_task", {"task_id": "nope"}))
    assert r.error

    r = asyncio.run(handler.execute_tool("list_tasks", {"status": "paused"}))
    assert t.task_id in r.data
    r = asyncio.run(handler.execute_tool("list_tasks", {"status": "cancelled"}))
    assert "没有" in r.data or "当前没有" in r.data


# ── 目标解析（口语化目标）────────────────────────────────────────────


def test_resolve_target_semantics(tmp_path):
    """本群/发给我/oc_/applink/群名 五种形态。"""
    import asyncio

    from app.orchestrator.context import set_caller_context

    store = ScheduledTaskStore(tmp_path)
    handler = _make_handler(store)
    ctx = {"chat_id": "oc_here", "sender_id": "ou_me", "sender_name": "张三"}

    # oc_ 直传
    set_caller_context(ctx)
    r = asyncio.run(handler._resolve_target("oc_explicit", ctx))
    assert r == {}
    # 本群
    r = asyncio.run(handler._resolve_target("本群", ctx))
    assert r == {"target": "oc_here", "target_name": "本群"}
    # 发给我
    r = asyncio.run(handler._resolve_target("发给我", ctx))
    assert r == {"target": "ou_me", "target_name": "张三"}
    # applink → 报错引导
    r = asyncio.run(handler._resolve_target(
        "https://applink.feishu.cn/client/chat/chatter/add_by_link?link_token=x", ctx))
    assert isinstance(r, str) and "applink" in r
    # 空目标 + 私聊 → 默认发给我
    r = asyncio.run(handler._resolve_target("", ctx))
    assert r == {"target": "ou_me", "target_name": "张三"}
    # 群名匹配（mock 群列表）
    async def _fake_find(name):
        return ("oc_found", "测试交流群") if name == "测试交流群" else None
    handler._find_chat_by_name = _fake_find
    r = asyncio.run(handler._resolve_target("测试交流群", ctx))
    assert r == {"target": "oc_found", "target_name": "测试交流群"}


# ── 执行方口径（dsh 链路）───────────────────────────────────────────


def test_executor_runs_instruction_without_agent_name(tmp_path):
    """执行方与执行链路解耦：档案里的 agent 不参与调用。

    实跑过的反例：档案写着 dev-bot（cc-connect 通道的编码 Agent，routable=false），
    而执行走的是 dsh 会话——那条会话里没有这个 Agent，到点只能拿到空回复。
    现在 agent 只作审批卡上的展示，指令原样交给会话。
    """
    from app.ops.scheduler.scheduler import DEFAULT_SCHEDULE_AGENT

    store = ScheduledTaskStore(tmp_path)
    t = _task(store, agent=DEFAULT_SCHEDULE_AGENT)
    executor = ScheduledTaskExecutor(store)
    dsh, ch = _FakeDshGateway(), _FakeChannel()
    executor.wire(None, ch, dsh_gateway=dsh)

    assert asyncio.run(executor.execute(t)) is True
    assert dsh.calls[0][0] == t.instruction


def test_eval_rejects_agent_outside_schedulable_list(tmp_path):
    """能力面判定仍生效：名单外的执行方照样驳回，只是默认名单换成了可路由能力。"""
    from app.ops.scheduler.scheduler import DEFAULT_SCHEDULE_AGENT, SCHEDULABLE_AGENTS

    store = ScheduledTaskStore(tmp_path)
    ok = ScheduledTaskEvaluator(SCHEDULABLE_AGENTS).evaluate(
        _task(store, agent=DEFAULT_SCHEDULE_AGENT))
    assert ok.ok, ok.blocks
    bad = ScheduledTaskEvaluator(SCHEDULABLE_AGENTS).evaluate(
        _task(store, agent="dev-bot"))
    assert not bad.ok
