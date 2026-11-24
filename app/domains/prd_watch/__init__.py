"""PRD 变更监听（easyprd）— 群讨论/妙记 → 变更点 → PRD 评论 → 决策闭环。

不变量与格式契约：docs/features/easyprd-prd-watch.md
接续档案：.agent/context/1102_TASK_feishu-agent-platform能力优化-easyprd实施/

模块分工：models（字段契约）→ extract（识别+置信度分带）→ anchor（段落块定位）→
render（评论/卡片文案）→ ledger（台账读写与降级）→ watcher（攒窗与主流程）→
decisions（回复轮询闭环）→ minutes（妙记通道）→ service（总装 + 定时循环）。

依赖方向：本包不 import 编排层，也不 import gateway——旁路在 main.on_message 分流进来，
观测回写走 record_hook 注入（main 提供）。这样「监听消息流」不会被塞进按意图分派的
路由里（那条路覆盖不到不@人的消息）。
"""
