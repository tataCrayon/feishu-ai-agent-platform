"""PM 守护进程与主链的「谁答」分界。

守的是一条事故：两个进程连同一个飞书应用，`app.main` 和 `pm_run` 都收得到
同一条私聊，各答一次 —— 用户看到两条互相矛盾的回复。分界规则全在这两个
纯函数里（`_pm_owns_message` / `_pm_daemon_alive`），`_process_batch` 只做接线。
"""

from __future__ import annotations

import pytest

from app.main import _pm_daemon_alive, _pm_owns_message


PM_OPEN_ID = "ou_pm_boss"
PM_GROUP = "oc_pm_pmo"


class _Sender:
    def __init__(self, open_id: str, name: str = "") -> None:
        self.open_id = open_id
        self.display_name = name


class _Convo:
    def __init__(self, chat_id: str, chat_type: str) -> None:
        self.chat_id = chat_id
        self.chat_type = chat_type


class _Msg:
    def __init__(self, chat_id: str, chat_type: str, sender: str) -> None:
        self.conversation = _Convo(chat_id, chat_type)
        self.sender = _Sender(sender)


class _PmCfg:
    enabled = True

    class identity:
        open_id = PM_OPEN_ID

    class push:
        group_id = PM_GROUP


class _RootCfg:
    pm = _PmCfg()


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    """配置读真实 config.toml 会让测试随部署漂移，钉成固定值。"""
    monkeypatch.setattr("app.main.load_config", lambda *a, **k: _RootCfg())


def test_pm_private_chat_is_yielded():
    """PM 本人私聊整个让：那是 daemon 的产品面，主链答了拿不到它的状态。"""
    msg = _Msg("oc_p2p", "p2p", PM_OPEN_ID)
    assert _pm_owns_message(msg, "11.26 提测情况怎么样") is True


def test_other_people_private_chat_is_not_yielded():
    """同事私聊不归 PM —— 让出去就没人应答了。"""
    msg = _Msg("oc_p2p", "p2p", "ou_colleague")
    assert _pm_owns_message(msg, "11.26 提测情况怎么样") is False


def test_group_chat_is_not_yielded_even_for_pm():
    """群里是主链地盘：daemon 的群通道只在配了 group_id 时才开。"""
    msg = _Msg("oc_group", "group", PM_OPEN_ID)
    assert _pm_owns_message(msg, "/pm 检查") is False


def test_pm_group_only_yields_pm_commands():
    """配了 PMO 群时，群里只有 `/pm*` 让给 daemon，其余照常主链答。"""
    at_pm = _Msg(PM_GROUP, "group", PM_OPEN_ID)
    assert _pm_owns_message(at_pm, "/pm 授权") is True
    assert _pm_owns_message(at_pm, "帮我查下 11.26 的排期") is False


def test_disabled_pm_yields_nothing(monkeypatch):
    class Off(_RootCfg):
        class pm:
            enabled = False

    monkeypatch.setattr("app.main.load_config", lambda *a, **k: Off())
    msg = _Msg("oc_p2p", "p2p", PM_OPEN_ID)
    assert _pm_owns_message(msg, "11.26 什么情况") is False


def test_missing_pm_config_yields_nothing(monkeypatch):
    class Bare:
        pm = None

    monkeypatch.setattr("app.main.load_config", lambda *a, **k: Bare())
    msg = _Msg("oc_p2p", "p2p", PM_OPEN_ID)
    assert _pm_owns_message(msg, "11.26 什么情况") is False


def test_config_error_is_treated_as_not_owning(monkeypatch):
    """配置读不出来时不能让主链哑掉 —— 宁可多答一句。"""
    def boom(*a, **k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("app.main.load_config", boom)
    msg = _Msg("oc_p2p", "p2p", PM_OPEN_ID)
    assert _pm_owns_message(msg, "11.26 什么情况") is False


def test_daemon_probe_returns_bool_even_on_failure(monkeypatch):
    """探测失败必须是 False 而不是抛异常：调用点在消息主链上。"""
    import subprocess

    def boom(*a, **k):
        raise OSError("no wmic")

    monkeypatch.setattr(subprocess, "run", boom)
    assert _pm_daemon_alive() is False


def test_dev_command_in_pm_private_chat_is_not_yielded():
    """`/dev` 走主链：daemon 没有 cc-connect 客户端，接走只会回「不认识」。"""
    msg = _Msg("oc_p2p", "p2p", PM_OPEN_ID)
    assert _pm_owns_message(msg, "/dev 改一下 pm_nudge 的催办话术") is False
    # 其余私聊照旧让给 daemon
    assert _pm_owns_message(msg, "11.26 提测情况") is True
