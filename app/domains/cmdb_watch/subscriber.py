"""CMDB 工单提醒订阅管理 — 订阅者档案 + CMDB 用户名校验。

订阅 = 授权：用户在飞书对话里声明「我的 CMDB 用户名是 xxx」，bot 把
`open_id → cmdb_user` 记进 state.json，之后只把该用户名创建的任务发给他本人。
不支持「订阅别人的工单」——那等于是让别人替你决定推什么，且 CMDB 里
`creator` 是唯一可信的归属依据。

身份归属只认**发送者自己的 open_id**：群聊里谁 @bot 就绑谁，不读卡片回调里
的其他人 ID，避免「A 声明 B 的用户名，结果 B 收到一堆 A 的任务」。
"""

from __future__ import annotations

import re

# CMDB 用户名形如 `devuser`（英文名.姓），也见过纯字母/下划线的老账号。
# 用宽松但有边界的正则：允许字母/数字/点/下划线/连字符，长度 2~40。
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,39}$")


class SubscribeError(Exception):
    """订阅请求不合法（给用户看的理由，不含内部细节）。"""


def normalize_cmdb_user(raw: str) -> str:
    """清洗用户给的 CMDB 用户名：去空白、去常见包裹、去邮箱域名。

    用户最常见的三种写法：
    - `devuser`            → 原样
    - `devuser@example.com` → 取 @ 前（CMDB 用户名就是邮箱前缀）
    - `@devuser` / `「」`   → 去掉装饰
    """
    value = (raw or "").strip().strip("@").strip("「」『』\"'`，,。.")
    if "@" in value:
        value = value.split("@", 1)[0].strip()
    if not value:
        raise SubscribeError("没看到 CMDB 用户名，例如：订阅 CMDB 工单提醒 devuser")
    if not _USERNAME_RE.match(value):
        raise SubscribeError(
            f"「{value}」不像 CMDB 用户名（一般是英文名.姓，如 devuser）")
    return value.lower()
