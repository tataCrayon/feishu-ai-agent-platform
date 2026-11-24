"""pytest 共享引导 — 只做仓库根入 sys.path 一件事。

为什么需要：测试分两处放（`tests/` 是跨面契约测试，`app/**` 是同目录单测），
直接 `python app/domains/daily/test_xxx.py` 时 Python 把脚本目录当
`sys.path[0]`，仓库根不在路径上，`import app.*` 一律 ModuleNotFoundError
（`test_inproc_deadline.py` 的自述跑法就这么失败过）。

pytest 自己的 rootdir 插入只在 `rootdir` 有 ini 文件时生效，独立运行脚本仍然
不成立；这里加一条显式引导，两边都对。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
