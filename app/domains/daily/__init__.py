"""Daily Report Module — daily tech news push and subscription management.

数据单源：AI HOT（app/domains/daily/aihot.py），取数即落本地缓存（DailyStore）。
旧 LocalKBase 目录读取（reader/source/template/converter）随单源化下线，
模块保留 reader/source 的历史代码以备回滚，但不再是运行时路径。

Provides:
- AIHOTDailySource: 单源适配（get_daily_pair 缓存优先 + API 兜底）
- DailyStore: 本地缓存（logs/daily/cache.json）
- SubscriptionManager: subscription CRUD and scheduling
- DailyScheduler: periodic push delivery

Usage:
    from app.domains.daily import DailyStore, SubscriptionManager, DailyScheduler
"""

from __future__ import annotations

from app.domains.daily.scheduler import AIHOTDailySource, DailyScheduler
from app.domains.daily.store import DailyStore
from app.domains.daily.subscription import SubscriptionManager

__all__ = [
    "AIHOTDailySource",
    "DailyScheduler",
    "DailyStore",
    "SubscriptionManager",
]
