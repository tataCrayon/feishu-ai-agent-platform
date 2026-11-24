"""PM 工作台能力层 — 事项账本、感知、排期闸门与卡片投递。

分层归属：`app/domains/pm/`，与 `prd_watch` / `cmdb_watch` 同级。

模块分工（数据流顺序）：

    pm_watch    采集：user token 轮询所有会话，筛出「跟我有关」的消息
    pm_thread   事项账本：append-only 事件日志 → fold 成「一件事」视图
    pm_daily_triage  分档：action / done / fyi / irrelevant
    pm_hub_engine    排期：G1-G7 闸门倒排，纯函数
    pm_daemon   调度：09:00 早报 / 17:30 复盘 / proactive 推送
    pm_send     外发：以 PM 本人身份发文本（唯一出口）
    pm_confirm_card  卡片：发送 + 按钮回调

依赖方向：本包不 import `app.orchestrator` / `app.gateway`。投递由 `app/api/pm_capabilities.py`
的 HTTP 路由触发，调度由调用方拉起，包内不自行启动循环。

身份与配置：`pm_hub_engine` 里的 `PM_OPEN_ID` 等常量是**兼容层**，值来自
`config.toml [projects.pm]`（`app/config.py::_load_pm_config`），换人改配置不改代码。
"""
