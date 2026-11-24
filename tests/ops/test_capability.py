"""能力热插拔回归测试：静态扫描 / zip-slip 防御 / 部署与回滚 / registry 原子重载。

运行：python -m pytest tests/ops/test_capability.py -q
（无 pytest-asyncio，同步逻辑用 ThreadPoolExecutor 模拟 deploy 线程池路径）
"""

import json
import zipfile
from pathlib import Path

import pytest

from app.agents.registry import AgentDef, AgentRegistry
from app.ops.capability.reloader import HotReloader
from app.ops.capability.scanner import CapabilityScanner, safe_extract_zip
from app.ops.capability.store import CapabilityRecord, CapabilityStore

VALID_SKILL_MD = """---
name: demo-skill
description: 测试技能：回归验证用
---

# demo-skill
正文
"""

VALID_AGENT_YAML = """name: demo-agent
display_name: 测试 Agent
description: 测试 agent.yaml 包：回归验证用
transport: handler
"""


def _write_skill_pkg(root: Path, name: str = "demo-skill", description: str = "测试技能：回归验证用") -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return pkg


# ── Scanner：凭据与危险内容 ──────────────────────────────────────────


def test_scan_valid_skill_low_risk(tmp_path):
    pkg = _write_skill_pkg(tmp_path)
    report = CapabilityScanner().scan(pkg)
    assert report.parse_ok
    assert report.capability_type == "skill"
    assert report.capability_name == "demo-skill"
    assert report.capability_description == "测试技能：回归验证用"
    assert not report.blocked


def test_scan_credential_assign_blocked(tmp_path):
    pkg = _write_skill_pkg(tmp_path)
    (pkg / "config.txt").write_text('password = "hunter2secret"', encoding="utf-8")
    report = CapabilityScanner().scan(pkg)
    assert report.blocked
    assert any(f.rule == "credential-assign" for f in report.findings)


def test_scan_missing_description_blocked(tmp_path):
    pkg = tmp_path / "bad-skill"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text("---\nname: bad-skill\n---\n", encoding="utf-8")
    report = CapabilityScanner().scan(pkg)
    assert report.blocked
    assert not report.parse_ok


def test_scan_env_placeholder_not_flagged(tmp_path):
    """${ENV} 占位符是凭据红线的正确形态，不得误报。"""
    pkg = tmp_path / "mcp-agent"
    pkg.mkdir()
    (pkg / "agent.yaml").write_text(
        "name: mcp-agent\n"
        "description: MCP 隔离测试\n"
        "mcp_servers:\n"
        "  demo:\n"
        "    command: npx\n"
        "    env:\n"
        "      EXAMPLE_TOKEN: ${EXAMPLE_TOKEN}\n",
        encoding="utf-8",
    )
    report = CapabilityScanner().scan(pkg)
    assert report.parse_ok
    assert not any(f.rule == "credential-assign" for f in report.findings)


# ── zip-slip 防御 ────────────────────────────────────────────────────


def test_safe_extract_zip_rejects_traversal(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../app/main.py", "pwned")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ValueError):
        safe_extract_zip(evil, dest)
    # 越界目标不得被创建
    assert not (tmp_path / "app" / "main.py").exists()


def test_safe_extract_zip_accepts_normal(tmp_path):
    ok = tmp_path / "ok.zip"
    with zipfile.ZipFile(ok, "w") as zf:
        zf.writestr("pkg/SKILL.md", VALID_SKILL_MD)
    dest = tmp_path / "out"
    dest.mkdir()
    extracted = safe_extract_zip(ok, dest)
    assert (dest / "pkg" / "SKILL.md").exists()
    assert extracted == ["pkg/SKILL.md"]


# ── HotReloader：部署 / 覆盖 / 回滚 ──────────────────────────────────


class _FakeOrchestrator:
    """rebuild 回调替身：可编程成功/失败。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def rebuild(self) -> tuple[bool, str]:
        self.calls += 1
        if self.fail:
            return False, "simulated graph failure"
        return True, "1 agents, 5 nodes"


def _make_reloader(tmp_path) -> tuple[HotReloader, CapabilityStore]:
    store = CapabilityStore(inbox_dir=tmp_path / "inbox")
    registry = AgentRegistry()
    return HotReloader(registry=registry, store=store), store


def _make_rec(store: CapabilityStore, name: str, ctype: str = "skill") -> CapabilityRecord:
    return store.new_record(
        capability_type=ctype, name=name,
        sender_id="u1", sender_name="tester", message_id="m1",
    )


def test_deploy_installs_skill(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("app.ops.capability.reloader.SKILLS_DIR", skills_dir)
    reloader, store = _make_reloader(tmp_path)
    rec = _make_rec(store, "demo-skill")
    pkg = _write_skill_pkg(tmp_path / "pkgs")

    result = reloader.deploy(rec, pkg)
    assert result.ok
    assert (skills_dir / "demo-skill" / "SKILL.md").exists()
    # manifest 不应被复制进正式目录
    assert not (skills_dir / "demo-skill" / "manifest.json").exists()
    # 覆盖式部署不应在 skills 目录残留 staging
    assert not list(skills_dir.glob(".staging-*"))


def test_pending_card_shows_routing_surface(tmp_path):
    """审批卡片必须展示路由注入面（工具名 + description 原文 code_block）。"""
    from app.ops.capability.approval_card import build_pending_card

    store = CapabilityStore(inbox_dir=tmp_path / "inbox")
    rec = _make_rec(store, "demo-skill")
    rec.risk_level = 0
    card = build_pending_card(rec, ["✅ 静态扫描未发现风险项"], description="测试技能：回归验证用")
    # 卡片序列化后应包含工具名与 description 原文
    flat = json.dumps(card, ensure_ascii=False)
    assert "route_to_demo_skill" in flat
    assert "测试技能：回归验证用" in flat


def test_card_buttons_declare_callback_behavior(tmp_path):
    """卡片 JSON 2.0 兼容：带 value 的 button 必须补 behaviors=[{type: callback, value: ...}]，
    否则点击不触发 card.action.trigger 回调（审批按钮点了没反应的根因）。
    且 behaviors[].value 必须携带回调数据（2.0 的数据位）。"""
    from app.gateway.channel import _ensure_button_behaviors
    from app.ops.capability.approval_card import build_pending_card
    from app.ops.scheduler.cards import build_rejected_card
    from app.ops.scheduler.evaluator import EvalReport
    from app.ops.scheduler.store import ScheduledTask


    class _TaskStub:
        task_id = "task-stub"
        instruction = "stub-instruction"
        agent = "dev-bot"
        creator_name = ""
        creator_id = "ou_stub"
        status = "approved"
        next_run_ts = 0.0

        def describe(self):
            return "stub"

    cards = [
        build_pending_card(_make_rec(CapabilityStore(inbox_dir=tmp_path / "inbox"), "demo-skill"),
                           ["✅ 静态扫描未发现风险项"], description="d"),
        # 定时任务卡片已无按钮（审批门下线），这里只要求它可以安全过垫片
        build_rejected_card(
            ScheduledTask(task_id="task-stub", instruction="stub", agent="dev-bot"),
            EvalReport(ok=False, reasons=["间隔低于 10 分钟"]),
        ),
        # 嵌套结构（column_set 内按钮）+ 无 value 按钮不加 behaviors
        {
            "schema": "2.0",
            "body": {"elements": [
                {"tag": "button", "value": {"cap": "c1", "act": "approve"}},
                {"tag": "column_set", "columns": [
                    {"tag": "column", "elements": [
                        {"tag": "button", "value": {"cap": "c2"}},
                    ]},
                ]},
                {"tag": "button"},
                {"tag": "markdown", "content": "x"},
            ]},
        },
    ]
    for card in cards:
        fixed = _ensure_button_behaviors(card)
        # 每个 value 按钮一个 behaviors，且 behaviors[].value 与顶层 value 等值
        btns = []

        def _collect(node, _btns=btns):
            if isinstance(node, dict):
                if node.get("tag") == "button" and "value" in node:
                    _btns.append(node)
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(fixed)
        for b in btns:
            assert b.get("behaviors"), f"button 缺 behaviors: {b}"
            assert b["behaviors"][0]["type"] == "callback"
            assert b["behaviors"][0]["value"] == b["value"]


def test_extract_action_value_from_callback_shapes():
    """回调 value 提取须兼容两种包装形态：SDK 解包直取 / raw.behaviors 兜底；
    且按 key 认领（cap=能力卡片，task=定时任务卡片），不是自己的卡片返回 None。"""
    from types import SimpleNamespace

    from app.ops.capability.approval_card import _extract_action_value

    # 形态一：action.value 直取（含历史误包一层）
    ev = SimpleNamespace(action=SimpleNamespace(value={"cap": "c1", "act": "approve"}), raw={})
    assert _extract_action_value(ev, "cap") == {"cap": "c1", "act": "approve"}
    ev = SimpleNamespace(action=SimpleNamespace(
        value={"tag": "button", "value": {"cap": "c1", "act": "approve"}}), raw={})
    assert _extract_action_value(ev, "cap") == {"cap": "c1", "act": "approve"}
    # 形态二：2.0 数据在 raw.event.action.behaviors[].value
    ev = SimpleNamespace(
        action=SimpleNamespace(value={}),
        raw={"event": {"action": {"behaviors": [
            {"type": "callback", "value": {"cap": "c2", "act": "reject"}}
        ]}}},
    )
    assert _extract_action_value(ev, "cap") == {"cap": "c2", "act": "reject"}
    # key 认领：task 卡片不会被 capability handler 认领（修「未找到定时任务档案 None」）
    ev_task = SimpleNamespace(action=SimpleNamespace(value={"task": "t1", "act": "approve"}), raw={})
    assert _extract_action_value(ev_task, "cap") is None
    assert _extract_action_value(ev_task, "task") == {"task": "t1", "act": "approve"}
    # 取不到 → None（handler 静默忽略非审批卡片）
    ev = SimpleNamespace(action=SimpleNamespace(value={}), raw={})
    assert _extract_action_value(ev, "cap") is None


def test_deploy_overwrite_restores_old_on_graph_failure(tmp_path, monkeypatch):
    """覆盖同名能力后 graph 重建失败：旧版必须原位恢复。"""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("app.ops.capability.reloader.SKILLS_DIR", skills_dir)
    reloader, store = _make_reloader(tmp_path)

    old_pkg = _write_skill_pkg(tmp_path / "old", name="demo-skill", description="旧版")
    rec1 = _make_rec(store, "demo-skill")
    assert reloader.deploy(rec1, old_pkg).ok
    old_marker = skills_dir / "demo-skill" / "SKILL.md"
    assert "旧版" in old_marker.read_text(encoding="utf-8")

    orch = _FakeOrchestrator(fail=True)
    reloader._rebuild = orch.rebuild
    new_pkg = _write_skill_pkg(tmp_path / "new", name="demo-skill", description="新版")
    rec2 = _make_rec(store, "demo-skill")
    result = reloader.deploy(rec2, new_pkg)

    assert not result.ok
    assert "旧版" in old_marker.read_text(encoding="utf-8")
    # 新版与临时暂存不残留
    assert "新版" not in old_marker.read_text(encoding="utf-8")
    assert not list(skills_dir.glob(".staging-*"))
    assert not list((store.backup_root()).glob("*-target"))


def test_deploy_copytree_midway_failure_keeps_old(tmp_path, monkeypatch):
    """模拟 copytree 中途崩溃（在 move 上位前抛异常）：旧版目录不受影响。"""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("app.ops.capability.reloader.SKILLS_DIR", skills_dir)
    reloader, store = _make_reloader(tmp_path)

    old_pkg = _write_skill_pkg(tmp_path / "old", name="demo-skill", description="旧版")
    rec1 = _make_rec(store, "demo-skill")
    assert reloader.deploy(rec1, old_pkg).ok

    rec2 = _make_rec(store, "demo-skill")
    # 让 staging copytree 一次性失败（读源目录时抛错）
    reloader._fail_copytree_once = True
    import shutil as _shutil
    orig_copytree = _shutil.copytree

    def exploding_copytree(*args, **kwargs):
        raise OSError("simulated disk full during copytree")

    monkeypatch.setattr(_shutil, "copytree", exploding_copytree)
    result = reloader.deploy(rec2, old_pkg)
    monkeypatch.setattr(_shutil, "copytree", orig_copytree)

    assert not result.ok
    assert "旧版" in (skills_dir / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert not list(skills_dir.glob(".staging-*"))


def test_deploy_staging_structure_check(tmp_path, monkeypatch):
    """staging 缺能力入口文件（SKILL.md/agent.yaml）时拒绝占位正式目录。"""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("app.ops.capability.reloader.SKILLS_DIR", skills_dir)
    reloader, store = _make_reloader(tmp_path)
    rec = _make_rec(store, "empty-pkg")
    pkg = tmp_path / "empty-pkg"
    pkg.mkdir()
    (pkg / "README.md").write_text("no entry file", encoding="utf-8")

    result = reloader.deploy(rec, pkg)
    assert not result.ok
    assert not (skills_dir / "empty-pkg").exists()


# ── AgentRegistry：原子 swap 语义 ────────────────────────────────────


def test_registry_reload_swaps_atomically(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "a1").mkdir()
    (agents_dir / "a1" / "agent.yaml").write_text(VALID_AGENT_YAML, encoding="utf-8")
    monkeypatch.setattr("app.agents.registry.AGENTS_DIR", agents_dir)

    registry = AgentRegistry()
    registry.load()
    assert registry.get("demo-agent") is not None
    snapshot = registry.list()

    # 新增第二个 agent 后 reload：新表含两者，旧 snapshot 不受影响
    (agents_dir / "a2").mkdir()
    (agents_dir / "a2" / "agent.yaml").write_text(
        VALID_AGENT_YAML.replace("demo-agent", "demo-agent-2"), encoding="utf-8"
    )
    registry.reload()
    assert registry.get("demo-agent-2") is not None
    assert len(snapshot) == 1  # 旧引用保持旧表内容（不可变快照语义）
    assert registry.get("demo-agent") is not None


def test_registry_reload_drops_removed_agent(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    d1 = agents_dir / "a1"
    d1.mkdir()
    (d1 / "agent.yaml").write_text(VALID_AGENT_YAML, encoding="utf-8")
    monkeypatch.setattr("app.agents.registry.AGENTS_DIR", agents_dir)

    registry = AgentRegistry()
    registry.load()
    assert registry.get("demo-agent") is not None

    # 目录删除后 reload：agent 从表中消失（目录即状态）
    import shutil as _shutil
    _shutil.rmtree(d1)
    registry.reload()
    assert registry.get("demo-agent") is None


def test_register_survives_reload_conflict(monkeypatch, tmp_path):
    """register 的合成 Agent 在下次 load 后被目录发现覆盖/清除（现状语义）。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr("app.agents.registry.AGENTS_DIR", agents_dir)
    registry = AgentRegistry()
    registry.load()
    registry.register(AgentDef(name="skill-x", display_name="X", description="合成"))
    assert registry.get("skill-x") is not None
    registry.reload()
    assert registry.get("skill-x") is None  # load 重建表，合成 Agent 不保留
