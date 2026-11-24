"""CMDB 工单提醒（cmdb-order-tracker 能力内化）。

原能力在个人 skill `~/.claude/skills/cmdb-order-tracker`：解析工单、盯第一条任务、
在对话里通知。内化后改成**常驻旁路**：轮询 CMDB 生产工单，只要某张工单里有
「关注名单」成员创建的任务，就发飞书卡片（版式对齐用户指定的「授权操作通知」）。

模块分工：client（只读 API）→ store（观察状态落盘）→ watcher（事件判定）→
service（循环启停）；render 是纯渲染，单测直击。

依赖方向：本包不 import 编排层、不 import gateway —— send_card / record_hook
都由 main 注入，两个能力都跑在「消息路由之外」的旁路上。
"""
