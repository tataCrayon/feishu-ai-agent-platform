"""多身份隔离 — 每个 PM 用户一套 lark-cli 凭据目录，互不覆盖。

守的是四条：目录按 open_id 分、未配置时不启用隔离、外部上下文标记要摘掉、
目录被别的账号占用时必须拦住（否则「推给 A、读 B 的会话」）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domains.pm import pm_identity


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """把凭据根挪到临时目录，测试不碰真实的 ~/.lark-cli。"""
    monkeypatch.setattr(pm_identity, "PM_HOME_ROOT", tmp_path / "pm")
    return tmp_path / "pm"


def test_auth_home_is_per_open_id(home):
    a = pm_identity.auth_home("ou_a")
    b = pm_identity.auth_home("ou_b")
    assert a != b
    assert a.name == "ou_a" and b.name == "ou_b"
    # 只算路径，不创建——空目录会让 lark-cli 报「未配置」而不是「未登录」
    assert not a.exists()


def test_empty_open_id_is_rejected():
    with pytest.raises(ValueError):
        pm_identity.auth_home("")


def test_user_env_points_at_own_dir(home):
    env = pm_identity.user_env("ou_a")
    assert env["LARKSUITE_CLI_CONFIG_DIR"] == str(home / "ou_a")


def test_user_env_clears_external_context(home):
    """hermes/openclaw 这类标记判「存在与否」，留空串仍算有上下文，必须删掉。"""
    env = pm_identity.user_env("ou_a")
    for name in ("HERMES_HOME", "OPENCLAW_HOME", "LARK_CHANNEL"):
        assert env.get(name) == ""

    cleaned = pm_identity._clean_env(env)
    for name in ("HERMES_HOME", "OPENCLAW_HOME", "LARK_CHANNEL"):
        assert name not in cleaned


def test_empty_open_id_means_machine_default(home):
    """没配 PM 身份时返回空 env：走机器级默认身份，其他功能行为不变。"""
    assert pm_identity.user_env("") == {}


def test_seed_config_creates_unlogged_config(home, monkeypatch):
    """铺配置但 users 为空＝「有 app、还没人登录」，auth login 才能发起 device flow。"""
    from app.config import FeishuConfig

    class FakeCfg:
        feishu = FeishuConfig(app_id="cli_test", app_secret="x")

    monkeypatch.setattr("app.config.load_config", lambda *a, **k: FakeCfg())
    target = home / "ou_a"
    pm_identity._seed_config(target)

    import json
    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    app = cfg["apps"][0]
    assert app["appId"] == "cli_test"
    assert app["users"] == []
    # 凭据只有一份：沿用 keychain 引用，不复制明文 secret
    assert app["appSecret"]["source"] == "keychain"


def test_seed_config_is_idempotent(home, monkeypatch):
    """已有配置不覆盖：二次调用不能把用户的登录态冲掉。"""
    target = home / "ou_a"
    target.mkdir(parents=True)
    (target / "config.json").write_text('{"apps": [{"users": [{"keep": 1}]}]}',
                                        encoding="utf-8")

    pm_identity._seed_config(target)

    import json
    cfg = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert cfg["apps"][0]["users"] == [{"keep": 1}]


def test_ensure_ready_fails_when_dir_missing(home):
    ok, why = pm_identity.ensure_ready("ou_a")
    assert ok is False
    assert "/pm 授权" in why


def test_ensure_ready_rejects_empty_id(home):
    ok, why = pm_identity.ensure_ready("")
    assert ok is False
    assert "open_id" in why


def test_ensure_ready_detects_wrong_account(home, monkeypatch):
    """目录里登录的是别人 —— 这是最危险的一种，必须说不。"""
    (home / "ou_a").mkdir(parents=True)
    monkeypatch.setattr(pm_identity, "whoami", lambda key: {
        "onBehalfOf": {"openId": "ou_other", "userName": "别人"},
        "tokenStatus": "ready",
    })
    ok, why = pm_identity.ensure_ready("ou_a")
    assert ok is False
    assert "ou_other" in why


def test_ensure_ready_accepts_matching_account(home, monkeypatch):
    (home / "ou_a").mkdir(parents=True)
    monkeypatch.setattr(pm_identity, "whoami", lambda key: {
        "onBehalfOf": {"openId": "ou_a", "userName": "张三"},
        "tokenStatus": "ready",
    })
    ok, why = pm_identity.ensure_ready("ou_a")
    assert ok is True
    assert "张三" in why


def test_list_users_reports_not_ready_entries(home, monkeypatch):
    """建过目录但没登录成功的也要列出来（⚠️），不能静默消失。"""
    (home / "ou_a").mkdir(parents=True)
    (home / "ou_b").mkdir(parents=True)
    monkeypatch.setattr(pm_identity, "whoami", lambda key: (
        {"onBehalfOf": {"openId": "ou_a", "userName": "张三"}} if key == "ou_a" else {}
    ))

    users = {u["open_id"]: u for u in pm_identity.list_users()}
    assert users["ou_a"]["ready"] is True
    assert users["ou_b"]["ready"] is False
    assert users["ou_b"]["user_name"] == ""


def test_list_users_empty_when_root_missing(home):
    assert pm_identity.list_users() == []


def test_start_login_refuses_without_open_id(home):
    with pytest.raises(ValueError):
        pm_identity.start_login("")


def test_finish_login_refuses_without_code(home):
    ok, why = pm_identity.finish_login("ou_a", "")
    assert ok is False
    assert "device_code" in why


def test_finish_login_is_idempotent_when_already_logged_in(home, monkeypatch):
    """已经登录好了就别再跑 --device-code：那会报「设备码已使用」，看着像失败。"""
    (home / "ou_a").mkdir(parents=True)
    monkeypatch.setattr(pm_identity, "whoami", lambda key: {
        "onBehalfOf": {"openId": "ou_a", "userName": "张三"},
        "tokenStatus": "ready",
    })
    called = []
    monkeypatch.setattr(pm_identity, "run_cli",
                        lambda *a, **k: called.append(a) or {})

    ok, who = pm_identity.finish_login("ou_a", "some-code")
    assert ok is True
    assert "张三" in who
    assert called == [], "已登录时不该再调 lark-cli"


def test_finish_login_survives_nonzero_exit_after_token_landed(home, monkeypatch):
    """lark-cli 在非交互环境会以 exit≠0 回「请确保 timeout>=600s」，
    但 token 可能已经落盘——必须再判一次，不能直接报失败。"""
    (home / "ou_a").mkdir(parents=True)
    state = {"calls": 0}

    def fake_whoami(key):
        state["calls"] += 1
        # 第一次（进函数时的幂等检查）说没有，第二次（失败后复查）说有了
        if state["calls"] == 1:
            return {}
        return {"onBehalfOf": {"openId": "ou_a", "userName": "张三"}}

    def fake_run(*a, **k):
        raise RuntimeError("lark-cli 执行失败: [AI agent] 此命令最长阻塞约 10 分钟")

    monkeypatch.setattr(pm_identity, "whoami", fake_whoami)
    monkeypatch.setattr(pm_identity, "run_cli", fake_run)

    ok, who = pm_identity.finish_login("ou_a", "some-code")
    assert ok is True
    assert "张三" in who


def test_wait_ready_returns_true_immediately_when_ready(home, monkeypatch):
    (home / "ou_a").mkdir(parents=True)
    monkeypatch.setattr(pm_identity, "whoami", lambda key: {
        "onBehalfOf": {"openId": "ou_a", "userName": "张三"},
    })
    assert pm_identity.wait_ready("ou_a", timeout=1, interval=0.1) is True


def test_wait_ready_times_out_when_not_logged_in(home):
    (home / "ou_a").mkdir(parents=True)
    assert pm_identity.wait_ready("ou_a", timeout=0.3, interval=0.1) is False
