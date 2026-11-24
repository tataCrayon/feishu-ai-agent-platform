"""PM Hub 守护进程入口 — 独立进程，不由 `app.main` 拉起。

为什么独立：PM 的 3 秒轮询 + 每天一次全量扫描（5-7 分钟）与 bot 主循环抢同一份
CPU 和 lark-cli 子进程；放进 `app.main` 的线程里，一次全量扫描能把群消息应答拖到
超时。两者共用 `.threads.jsonl` 与 `.watch_state.json`，但生命周期互相独立：
PM 挂了 bot 照常回话，bot 重启不影响定时推送。

用法：

    python -m app.domains.pm.pm_daemon

（不要 `python app/domains/pm/pm_daemon.py`：包里用的是绝对 import，
直接跑脚本会让 `app` 不在 sys.path 上。）

退出码：`[projects.pm].enabled = false` 时以 2 退出——**不要静默空转**，
否则守护进程看起来在跑、实际什么都没做。
"""

from __future__ import annotations

import logging
import sys

from app.domains.pm.pm_config import pm_config


def main() -> int:
    cfg = pm_config()
    log = logging.getLogger("pm_daemon")
    if not cfg.enabled:
        log.error(
            "[pm] 未启用：config.toml [projects.pm].enabled = false，守护进程不启动"
        )
        return 2
    if not cfg.identity.open_id:
        log.error(
            "[pm] 缺身份：config.toml [projects.pm].identity].open_id 为空，"
            "推送会全部打到空目标，拒绝启动"
        )
        return 2

    # 启动前把配置自检跑一遍并写进日志。这些项失败不会拦住进程（表可以先不配、
    # 版本雷达可以先空着），但**必须留痕**——它们的失败方式都是静默的：
    # 推送发到空目标、感知扫回一堆空、雷达算出 0 个版本，事后查日志才知道。
    from app.domains.pm.pm_setup import render_report

    report = render_report()
    for line in report.splitlines():
        log.info("[pm 自检] %s", line)

    from app.domains.pm.pm_daemon import run_daemon

    run_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
