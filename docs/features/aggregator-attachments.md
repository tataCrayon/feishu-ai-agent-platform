# 功能简报：连发消息合并与附件链路（aggregator）

> 改这个功能前读这一页即可动手，不必预读实现。与代码矛盾时以代码 + commit 为准并回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：docs/specs/_feature-brief-template.md

## 1. 一句话

把用户连发的多条消息（文本+文件混发）合并成一个批次再交给编排层，保证「先发文件后发指令」这类多轮输入不丢附件、不拆意图。

## 2. 主链路

```
飞书 WS on_message (app/main.py)
  → Aggregator.merge (app/gateway/aggregator.py)   debounce 窗口内合并（messaging.debounce_sec）
  → main._process_batch                     批次=文本列表+附件列表（挂末条消息）
  → crew agent 节点检测 Handler.run_with_message → 聚合分派（app/orchestrator/crew.py）
  → Handler 侧按 r._source_message_id 下载附件 → 业务处理
```

## 3. 期望行为

- 连发 N 条在 debounce 窗口内 → 合成一批，只触发一次编排
- 批次内含文件时：文件与最后的指令文本同批到达 Handler（先发 3 个 .msg 再发「解析这个」→ 引擎收到 3 附件+1 指令）
- 附件跨消息下载必须成功（用原消息 id，不因合并而失效）
- 用户用飞书「回复」引用上一条时：本条正文后附「（引用的上一条：…）」，路由能看到被引用的工单原文，而不是只看到「你确认一下」（2026-09-29）
- 引用正文是**异步拉取**的（`main._text_with_quote`）：拉完必须挂回 `msg._prefetched_text`，
  aggregator 合并时 `extract_text` 优先读它——不同步挂回，算好的引用正文会被 worker 丢掉（2026-09-30 串单事故）
- 被引用消息 id 取 `msg.reply.message_id`，为空再兜底 `raw.parent_id`：lark_channel 只在
  `parent_id != root_id` 时建 ReplyRef，「回复」话题根消息时 reply 是 None（2026-09-30）

## 4. 约束与不变量（改前必读）

- 批次附件必须合并挂到**末条消息**：末条常是无附件的指令文本，附件挂首条会随「先文件后指令」场景丢失（证据：commit `c84f47a`，2026-09-18）
- 每个附件必须记**来源 message_id**（`r._source_message_id`）：飞书 message-resource 端点按原消息校验，用合并后的合成 id 下载必失败（证据：commit `7526194`）
- **「不改」清单**：批次结构（messages + 附件挂末条 + 来源 id 属性）是 crew 聚合分派与 Handler 的契约；debounce 窗口合并语义勿改成逐条直通
- 改动若触及聚合分派侧，先读 `docs/features/routing-dispatch.md`（透传用户原文不变量）

## 5. 单源与派生物

- 单源：`app/gateway/aggregator.py` + `config.toml [projects.messaging].debounce_sec`
- 派生物：无

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-09-18 | 批次附件合并到末条消息——修复「先发文件后发指令」附件丢失 | commit `c84f47a` |
| 2026-09-18 | 合并附件带来源 message_id——跨消息下载须用原始消息 id | commit `7526194` |
| 2026-09-30 | 引用正文挂回 prefetched + raw.parent_id 兜底——修复 DATA2026093002 被串成 DATA2026092903 出单（引用正文丢失 → LLM 拿历史旧工单顶替） | 见 git log `fix(aggregator` / `fix(main)` |

## 7. 已知坑

- debounce_sec 调小 → 用户手速慢时一批拆多批、意图上下文断裂；调大 → 响应延迟。调整前先确认现值与事故背景

## 8. 演练记录（spec-driven 修改实验）

| 日期 | 需求 | 未预读实现？ | 一次通过？ | 返工原因 |
|---|---|---|---|---|
