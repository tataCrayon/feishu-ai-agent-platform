"""dsh 链路装配的契约测试 — 系统提示词接线、记忆注入、任务档案口径。

这三条都是「不报错但也不生效」的类型：规则没进系统提示词（模型照样乱答）、
记忆召回结果被丢掉（每轮白花一次网关往返）、任务档案没写（控制台显示最近没有
任务）。单测钉住的是接线本身，不是模型表现——后者由 verify_routing.py 真实回归。
"""

from __future__ import annotations

from pathlib import Path

from app.dsh.gateway import _with_memory
from app.dsh.routing import RULES_PATH, build_injection, load_rules
from app.dsh.task_record import record_dsh_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PATCH = PROJECT_ROOT / "deploy" / "dsh" / "agentbiz" / "cordis.patch.yml"


def test_rules_file_is_not_empty_and_has_no_template_braces() -> None:
    """规则正文会被 dsh 当模板插值：出现 `{{x}}` 且 x 未注册时装配直接失败。

    这是「改提示词时手滑写了个举例模板」就会把整条链路打挂的地方，
    所以在规则文件上钉死，而不是等启动时才发现。
    """
    text = load_rules()
    assert len(text) > 1000
    assert "{{" not in text and "}}" not in text


def test_build_injection_returns_rules_verbatim() -> None:
    assert build_injection() == load_rules()
    assert RULES_PATH.is_file()


def test_profile_wires_rules_into_system_prompt() -> None:
    """规则必须由 profile 的 system-prompt 段读进系统提示词。

    退回「首条 user 消息」不会报错，只会让规则被当成别人说过的话——后续轮次
    冲淡它、压缩时裁掉它，用户表现为「它又忘了规矩」。
    """
    patch = PATCH.read_text(encoding="utf-8")
    assert "- id: system-prompt" in patch
    assert "personaPrefix" in patch
    assert "DSH_ROUTING_RULES" in patch
    # 规则文件不叫 AGENTS.md：那个名字会被 agent-instructions 插件当工作区指引
    # 一并载入（该插件在本 profile 里是禁用的，别再把路口开回去）。
    assert "agents-instructions" not in patch or "disabled: true" in patch


def test_memory_is_framed_and_not_collapsed_into_user_request() -> None:
    """召回上下文必须有明确边界，否则模型会把记忆当成用户这轮说的话。"""
    out = _with_memory("今天 AI 有什么新闻", "用户偏好：简短回答")
    assert out.startswith("【长期记忆召回")
    assert "今天 AI 有什么新闻" in out
    assert out.index("召回结束") < out.index("今天 AI 有什么新闻")


def test_empty_memory_leaves_message_untouched() -> None:
    assert _with_memory("hi", "") == "hi"
    assert _with_memory("hi", "   ") == "hi"


def test_task_record_carries_tool_chain_and_reply(tmp_path, monkeypatch) -> None:
    """dsh 任务的 route_chain 来自实际调用的工具（去重、限量）。"""
    from app.observability import task_record as tr

    written: list = []
    replies: list = []
    monkeypatch.setattr(tr.task_recorder, "write", lambda rec: written.append(rec))
    monkeypatch.setattr(tr.reply_store, "write",
                        lambda tid, text, meta=None: replies.append((tid, text, meta)))

    record_dsh_task(
        task_id="t1", chat_id="oc_x", sender_id="ou_1", sender_name="王",
        query="今天 AI 新闻", reply="答复正文",
        tool_names=["pwsh", "pwsh", "mcp__feishu__read_conversation"],
    )

    rec = written[0]
    assert rec.task_id == "t1" and rec.final_agent == "dsh"
    assert rec.route_chain == ["pwsh", "mcp__feishu__read_conversation"]
    assert rec.status == "success"
    assert replies and replies[0][1] == "答复正文"


def test_task_record_marks_empty_reply_as_no_response(tmp_path, monkeypatch) -> None:
    from app.observability import task_record as tr

    written: list = []
    monkeypatch.setattr(tr.task_recorder, "write", lambda rec: written.append(rec))
    monkeypatch.setattr(tr.reply_store, "write", lambda *a, **kw: None)

    record_dsh_task(
        task_id="t2", chat_id="oc_x", sender_id="ou_1", sender_name="王",
        query="x", reply="",
    )
    assert written[0].status == "no_response"
    assert written[0].results_quality == "failed"


def test_task_record_write_failure_never_raises(monkeypatch) -> None:
    """档案是观测面：写失败不能让用户少拿一条回复。"""
    from app.observability import task_record as tr

    def _boom(_rec):
        raise OSError("disk full")

    monkeypatch.setattr(tr.task_recorder, "write", _boom)
    record_dsh_task(task_id="t3", chat_id="c", sender_id="s", sender_name="",
                    query="q", reply="r")


def test_skill_root_points_at_whitelist_output() -> None:
    """技能目录 = 路由候选集，必须指白名单同步产物而不是 bot/skills 源目录。

    源目录混着 demo-guard / x 这类工具型与占位技能，指过去就是让它们和业务
    能力抢分派；`upgrade.py` 的配置断言用的是产物目录，两边必须同口径。
    """
    sup = (PROJECT_ROOT / "app" / "dsh" / "supervisor.py").read_text(encoding="utf-8")
    assert '"deploy" / "dsh" / "agentbiz" / "skills"' in sup
    assert 'PROJECT_DIR / "bot" / "skills"' not in sup
