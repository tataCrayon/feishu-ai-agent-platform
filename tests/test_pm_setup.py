"""`/pm` 自助配置命令 — 在飞书对话里完成接入自检，不用改文件重启。

守的是三条：配置只读不写回（会吃掉注释）、自检不发消息不写表（纯只读）、
身份以 lark-cli 登录态为准（不问用户「你是谁」）。
"""

from __future__ import annotations

import pytest

from app.config import PmConfig, PmIdentity, PmOutputs, PmPush
from app.domains.pm import pm_setup


@pytest.fixture
def ident(monkeypatch):
    """钉住该用户的登录态，避免测试依赖开发机真实凭据目录。"""
    monkeypatch.setattr(
        pm_setup, "lark_user_identity",
        lambda cfg=None: {"openId": "ou_me", "userName": "张三", "tokenStatus": "valid"},
    )


def test_help_shows_three_things_to_configure():
    text = pm_setup.handle_command("/pm 帮助")
    assert "授权" in text and "表" in text and "09:00" in text


def test_check_reports_each_missing_item(monkeypatch, ident):
    """未配置时逐项列出「修什么」，不是一句「未配置」。"""
    monkeypatch.setattr(pm_setup, "pm_config", lambda: PmConfig())
    report = pm_setup.render_report()
    assert "已启用" in report
    assert "身份已配置" in report
    assert "表已配置" in report
    assert "推送目标" in report


def test_identity_mismatch_is_the_loudest_failure(monkeypatch):
    """你说你是 A、lark-cli 登录的是 B：会「推给 A、读 B 的会话」，必须拦住。"""
    cfg = PmConfig(enabled=True, identity=PmIdentity(open_id="ou_other", names=["李四"]))
    monkeypatch.setattr(pm_setup, "pm_config", lambda: cfg)
    monkeypatch.setattr(pm_setup, "lark_user_identity",
                        lambda cfg=None: {"openId": "ou_me", "userName": "张三", "tokenStatus": "valid"})
    check = pm_setup.check_identity(cfg)
    assert check.ok is False
    # 必须给出「怎么改」，而不是只说「不一致」
    assert "/pm 退出" in check.fix and "/pm 授权" in check.fix


def test_identity_command_never_writes_config(ident):
    """`/pm 我是` 只回「该填什么」，绝不回写 config.toml。"""
    out = pm_setup.handle_command("/pm 我是 张三", "ou_me")
    assert "ou_me" in out
    assert "不会替你改文件" in out


def test_identity_command_warns_when_name_differs(ident):
    """自报的名字和登录态不一致时要说出来，而不是照单全收。"""
    out = pm_setup.handle_command("/pm 我是 李四", "ou_me")
    # 登录态是「张三」，他说「李四」——必须指出不一致
    assert "张三" in out
    assert "注意" in out


def test_token_from_link_parses_sheets_and_base():
    assert pm_setup._token_from_link(
        "https://x.feishu.cn/sheets/shtcnExampleToken0000000000?sheet=sht000"
    ) == "shtcnExampleToken0000000000"
    assert pm_setup._token_from_link(
        "https://x.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable001"
    ) == "bascnExampleToken0000000000"
    assert pm_setup._token_from_link("不是链接") == ""


def test_sheet_command_gives_exact_key_names():
    out = pm_setup.handle_command(
        "/pm 表 https://x.feishu.cn/sheets/shtcnExampleToken0000000000?sheet=sht000")
    assert "spreadsheet_token" in out and "sheet_id_versions = \"sht000\"" in out


def test_unknown_pm_subcommand_falls_back_to_guide():
    out = pm_setup.handle_command("/pm 随便写的")
    assert "接入指引" in out


def test_non_pm_text_returns_none():
    """不是 `/pm` 的消息必须返回 None，交给后面的路由——不能吞掉正常提问。"""
    assert pm_setup.handle_command("今天有哪些事") is None


def test_push_target_reports_private_only_when_group_missing(monkeypatch):
    cfg = PmConfig(enabled=True, identity=PmIdentity(open_id="ou_me", names=["张三"]),
                   push=PmPush(group_id=""))
    check = pm_setup.check_push_target(cfg)
    assert check.ok is True
    assert "只发私聊" in check.detail


def test_outputs_check_without_token_is_not_a_crash(monkeypatch):
    """未配表时返回「要修」，不是抛异常——自检本身不能挂。"""
    check = pm_setup.check_outputs(PmConfig(enabled=True))
    assert check.ok is False
    assert "spreadsheet_token" in check.detail
