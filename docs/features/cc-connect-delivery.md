# 功能简报：cc-connect 投递可靠性（task:tag 关联 + 超时转后台守望）

> 改这个功能前读这一页即可动手，不必预读实现。与代码矛盾时以代码 + commit 为准并回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：docs/specs/_feature-brief-template.md

## 1. 一句话

让 Claude Code 通道（cc-connect）的投递-回复精准关联、慢任务不丢结果：超时转后台守望，迟到产出自动送达用户，而不是静默吞掉。

## 2. 主链路

```
crew agent 节点 → send_and_wait (app/protocols/cc_connect/client.py)
  投递: prompt 注入 [task:tag] 标记
  轮询: session 历史按 [task:tag] FIFO 关联回复（前面未答复任务先认领；跳过 NO_REPLY/pong）
  ├─ _POLL_TIMEOUT(90s) 内有回复 → 返回文本
  └─ 超时且挂 on_reply → 返回 LATE_DELIVERY 哨兵 + 后台守望（_late_reply_watcher，
       上限 _LATE_GRACE_SEC=1200s）→ 迟到结果经回调直达原会话
       （回调由 main.set_late_delivery_sender 接线 channel.send）
crew (app/orchestrator/crew.py): 哨兵 → 「⏳ 已转后台，完成后自动送达」回执
  + late_delivery 标记（decompose/并行路径就地转文案）
```

## 3. 期望行为

- 并发多个任务时回复不串台：每个回复回到发起它的任务
- 快任务 90s 内正常回复；慢任务用户立即看到「已转后台」回执，完成后再收到结果
- 守望期内无产出 → 记日志收尾（late watcher expired），不重复分派
- late_delivery 回执即终态，不再分派

## 4. 约束与不变量（改前必读）

- **超时不得静默丢弃**：send_and_wait 超时且挂了 on_reply 必须返回 LATE_DELIVERY 并起守望——17 分钟完整产出曾被 90s 轮询吞掉，用户只收到「已尝试过仍无法满足」（事故 2026-09-18 10:05 新闻SQL；证据：commit `b81e8a0`）
- **LATE_DELIVERY 哨兵转回执后即终态，禁止再分派**：重派=双跑，烧 CC 会话额度（机制原为 judge 短路，2026-10-12 删 judge 后由编排层在 `_execute_agent` 就地转回执实现；证据：commit `b81e8a0`）
- 回复关联按 [task:tag] **FIFO 认领**：旧的 base_len 位置基线在并发投递下会把别人的回复当自己的（串台）（证据：commit `b81e8a0`）
- 回调 sender 的 chat_id 闭包捕获必须对应原会话（错配=迟到结果发错群）（证据：commit `b81e8a0` main 接线）
- **「不改」清单**：[task:tag] 注入格式与历史解析互为契约；NO_REPLY/pong 跳过规则；90s/1200s 两个数值有事故背景，调整须附新证据

## 5. 单源与派生物

- 单源：`app/protocols/cc_connect/client.py`（投递/轮询/守望）+ `app/orchestrator/crew.py`（哨兵→回执/标记）+ `app/main.py`（set_late_delivery_sender 接线）
- 配置：`config.toml [projects.cc_connect]`（max_sessions 并发额度）+ `~/.cc-connect/cc-exec.toml`（执行通道专用，仓库外）

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-09-18 | 新闻SQL 事故：17 分钟产出被 90s 超时静默吞掉 → 全套 tag 关联 + 后台守望机制 | commit `b81e8a0` |
| 2026-09-14 | 同 app 双长连接竞争消费飞书消息（/help 透出内置帮助、档案断流）→ 飞书消息只许 feishu-agent-platform 独占消费 | docs/project-index.md cc-connect 行 |

## 7. 已知坑

- `~/.cc-connect/cc-exec.toml` 的飞书 platform 是占位 app_id（cc-connect v1.4.1 强制每 project 一个 platform，disabled_platforms 实测不生效）——不是配置错误，别「修复」它
- 会话复用由 cc-connect 侧 session_key resume 保证，client 侧不管理会话生命周期

## 8. 演练记录（spec-driven 修改实验）

| 日期 | 需求 | 未预读实现？ | 一次通过？ | 返工原因 |
|---|---|---|---|---|
