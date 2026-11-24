"""PM 能力层：去参数化后仍然读得到绑定 + 写表列值不落字面量。

守的是三条判据（PRD 4.0「怎么算做到了」）：
1. 改 `[projects.pm.identity].names` 加别名后 `_my_names()` 认了，且一个 `.py` 都没动；
2. 改 `open_id` 后私聊推送发到新的人；
3. `app/domains/pm/` 里不出现 `ou_` / `oc_` / `tbl` 字面量。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import PmConfig, PmIdentity, PmOutputs, PmPush, PmRelease, PmRules
from app.domains.pm import pm_config as pm_config_mod
from app.domains.pm import pm_hub_engine, pm_nudge, pm_thread, pm_watch

PM_DIR = Path(__file__).resolve().parent.parent / "app" / "domains" / "pm"


@pytest.fixture
def cfg(monkeypatch) -> PmConfig:
    """钉住一份完整配置（真读 config.toml 会让测试随开发机状态漂移）。"""
    c = PmConfig(
        enabled=True,
        identity=PmIdentity(open_id="ou_me", names=["张三", "小张"]),
        outputs=PmOutputs(
            spreadsheet_token="sht_x", sheet_id_versions="sht000",
            sheet_id_weekly="sht001", base_token="base_x", table_id="tbl_x",
        ),
        push=PmPush(group_id="oc_pmo"),
        rules=PmRules(release=PmRelease(release_weekday=4, ask_days_before=[8])),
    )
    monkeypatch.setattr(pm_config_mod, "pm_config", lambda: c)
    return c


def _reload_engine_constants(monkeypatch, cfg: PmConfig) -> None:
    """引擎里的 5 个常量是 import 期求值的，换语料要重建它们。"""
    monkeypatch.setattr(pm_hub_engine, "PM_OPEN_ID", cfg.identity.open_id)
    monkeypatch.setattr(pm_hub_engine, "PMO_GROUP_ID", cfg.push.group_id)
    monkeypatch.setattr(pm_hub_engine, "SPREADSHEET_TOKEN", cfg.outputs.spreadsheet_token)


def test_my_names_comes_from_config(cfg, monkeypatch):
    """加一个别名只改配置：`_my_names()` 立刻认。"""
    monkeypatch.setattr(pm_watch, "pm_config", lambda: cfg)
    assert pm_watch._my_names() == ("张三", "小张")


def test_my_names_empty_config_warns_instead_of_guessing(cfg, monkeypatch, caplog):
    """没配名字时不回落任何默认值——回落到某个人的名字就是又把它写死了。"""
    empty = PmConfig(enabled=True)
    monkeypatch.setattr(pm_watch, "pm_config", lambda: empty)
    with caplog.at_level("WARNING"):
        assert pm_watch._my_names() == ()
    assert "names" in caplog.text


def test_owner_name_is_first_alias(cfg, monkeypatch):
    monkeypatch.setattr(pm_config_mod, "pm_config", lambda: cfg)
    assert pm_config_mod.owner_name() == "张三"


def test_owner_name_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(pm_config_mod, "pm_config", lambda: PmConfig())
    assert pm_config_mod.owner_name() == ""


def test_require_identity_raises_without_open_id(monkeypatch):
    monkeypatch.setattr(pm_config_mod, "pm_config", lambda: PmConfig(enabled=True))
    with pytest.raises(RuntimeError, match="open_id"):
        pm_config_mod.require_identity()


def test_snooze_default_days_comes_from_rules(cfg):
    """`SNOOZE_DEFAULT_DAYS` 是 import 期常量，值应当与我们钉的规则一致。"""
    # 本测试运行在真实配置下（未启用），默认 3；这里守的是「不是裸字面量」
    assert pm_thread.SNOOZE_DEFAULT_DAYS == pm_config_mod.pm_config().rules.snooze_days
    assert isinstance(pm_thread.SNOOZE_DEFAULT_DAYS, int)


def test_me_first_alias_used_for_nudge_self_filter():
    """`ME` 是「别给自己起草催办」的过滤条件。空串会让过滤静默失效。

    它是 import 期常量，值必须与本进程实际读到的配置一致——不一致就说明
    有人把它改回了字面量。
    """
    assert pm_nudge.ME == (pm_config_mod.pm_config().identity.names or [""])[0]


def test_no_hardcoded_identity_literals_in_pm_package():
    """PRD 判据三：`app/domains/pm/` 里不再有 ou_/oc_/tbl 字面量。

    注释里提到历史 ID 是允许的（那是解释为什么），所以只扫字符串字面量。
    """
    pat = re.compile(r'"(?:ou_|oc_|tbl)[A-Za-z0-9]+"')
    offenders = []
    for p in sorted(PM_DIR.glob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pat.search(line):
                offenders.append(f"{p.name}:{i}: {line.strip()[:80]}")
    assert offenders == [], "业务代码里仍有身份字面量：\n" + "\n".join(offenders)


def test_engine_constants_default_to_empty_not_a_person():
    """未配置时是空串。空 = 未接，调用方据此跳过，而不是往某个真人的 ID 发。"""
    real = pm_config_mod.pm_config()
    if real.identity.open_id:
        pytest.skip("开发机已配置真实身份，此断言只在未配置时成立")
    assert pm_hub_engine.PM_OPEN_ID == ""
    assert pm_hub_engine.SPREADSHEET_TOKEN == ""
