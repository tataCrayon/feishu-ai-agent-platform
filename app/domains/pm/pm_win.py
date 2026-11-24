"""Windows 子进程静默执行。

为什么需要这个文件
------------------
daemon 用 DETACHED_PROCESS 启动，自己不持有控制台。此后凡是被拉起的 console
子进程 —— cmd.exe（shell=True）、powershell.exe、lark-cli.cmd、taskkill ——
Windows 都会给它新分配一个 conhost 窗口。屏幕上的表现就是：一个黑框一闪就没。

CREATE_NO_WINDOW 让子进程拿到不可见控制台：stdout 管道、退出码、timeout
全都不变，只是不画窗口。非 Windows 平台这里给 0，调用方不用判平台。
"""

from __future__ import annotations

import os
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0