"""飞书卡片 JSON 的 2.0 兼容处理 —— 一行规则，两个发送出口共用。

主线（`app/gateway/channel/` 的 `BotChannel.send_card`）和旁线（dsh MCP 子进程
里的 `feishu_send.send_card`，纯 HTTP、没有 SDK channel 对象）都必须过这一层：
卡片 JSON 2.0 里带 `value` 的按钮如果没声明 `behaviors=[{type: callback, ...}]`，
飞书**照样把卡片发出去**，只是点下去没有任何回调——客户端提示
「目标回调服务当前未在线」，而发送端一切正常、日志里也没有失败。

这条坑的成本全是「看不出来」：卡片长得完全正确、按钮也在，所以只能靠
「所有出口都走同一个函数」来保证，不能靠每个出口各自记得。

为什么不放回 `channel/__init__.py`：那会让旁线反向依赖网关层（MCP 子进程
只为补一个字段就得 import SDK 客户端）。协议层无 io、无依赖，两边都能用。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ensure_button_behaviors"]


def ensure_button_behaviors(card: dict[str, Any]) -> dict[str, Any]:
    """就地补齐所有 button 的 `behaviors`，返回同一份 card（便于链式传参）。

    两条规则：
    - 没有 `behaviors` → 按顶层 `value` 生成一条 callback；
    - 有 `behaviors` 但首项缺 `value` → 用顶层 `value` 补上。

    顶层 `value` 保留：它是 1.0 卡片的兼容兜底，而回调侧
    （`ops/capability/approval_card._extract_action_value`）两种包装形态都要认。
    """
    def _fix_button(node: dict[str, Any]) -> None:
        value = node.get("value")
        if value is None:
            return
        behaviors = node.get("behaviors")
        if not isinstance(behaviors, list) or not behaviors:
            node["behaviors"] = [{"type": "callback", "value": value}]
        elif isinstance(behaviors[0], dict) and behaviors[0].get("value") is None:
            behaviors[0]["value"] = value

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                _fix_button(node)
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(card)
    return card
