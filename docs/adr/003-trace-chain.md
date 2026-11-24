# ADR-003：Agent 调用链路追踪（Trace Chain）

> ADR 编号：`ADR-003` ｜ 状态：`已实现`（**部分过时**，见下方①）
> 创建 2026-08-14 ｜ 决策者 @devuser
>
> ① 落地后 `dispatcher.py` 与 `app/edc/` 已随架构演进消失（前者 2026-10 重构移除，
> 后者迁到 `app/eval/edc/`）。**存续的是 `app/observability/` 的 TraceContext /
> AgentTrace 与 `logs/agents/trace.jsonl`**，其余实现细节看代码不要看本文。

## 1. 背景

多 Agent 拆分（`ADR-002`）后，一条用户消息可能触发单 Agent 或跨 Agent 链式调用。
此前诊断只有散落的 `logger.info`：没有全局唯一标识把日志/审计/业务结果串成一条线，
没有调用树（谁是根、谁是子），也没有给评估层消费的结构化指标源。

## 2. 决策考虑

- **触发因素**：Agent 数量增长 + 链式编排上线后，单次请求无法端到端回溯。
- **约束条件**：单进程 asyncio，零外部基础设施；cc-connect 是外部进程，
  **无法向其注入埋点**，跨进程传播做不到；既有日志体系是文件 + JSONL。
- **非目标**：不做跨进程分布式 trace；不接集中式 APM / 链路查询 UI。

## 3. 决策选项

### 选项 A：自研轻量 TraceContext + JSONL 落盘

- **描述**：每请求一个 `trace_id`，每次 Agent 调用一个 `span_id`，`parent_span_id` 串成树；
  `tracer.trace()` 追加写 `logs/agents/{date}.jsonl`，链式调用另写 `.chain.jsonl`；
  评估层读这些文件聚合出 per-agent 指标。
- **优点**：零第三方依赖（`uuid`/`json`/`pathlib` 够用）；JSONL 追加 + 按日分片，
  与既有 `logs/` 体系一致；写盘异常全部吞掉不阻断业务；字段模型对齐 OTel，将来好迁。
- **缺点与代价**：无链路查询 UI，回溯靠 grep `trace_id`；span 树只覆盖 Python 编排层；
  分位数为排序近似；文件需定期清理（未落地）。
- **适用场景**：单进程、多 Agent 调用、零依赖方针下的轻量可观测。

### 选项 B：OpenTelemetry / Jaeger 标准分布式链路

- **优点**：标准协议、生态成熟、可对接 APM。
- **缺点与代价**：引入 exporter/SDK 破坏零依赖；cc-connect 是外部进程无法自动埋点，
  跨进程链路照样断——核心诉求没解决；要额外部署 collector + 存储。
- **适用场景**：多服务分布式架构、已有 APM 基础设施。

### 选项 C：不落结构化 trace，只强化 logging（「什么都不做」基准）

- **优点**：改动最小。
- **缺点与代价**：无结构化字段，采集仍需解析文本日志；无 span 树；打点靠自觉，易漏。
- **适用场景**：单 Agent 且无指标评估需求。

## 4. 决策结论

**选定：选项 A。**

- **核心理由**：① B 的跨进程传播在本架构下根本落不了地，而诉求只是「编排层内的调用树
  + 指标数据源」，A 恰好覆盖；② 零新增依赖，与 `logs/` 体系和评估工具链无缝衔接；
  ③ C 提供不了稳定的指标源。
- **影响范围**：新增 `app/observability/`；`on_message` 每请求建根 `TraceContext`，
  流水线各环节共用一个 `trace_id`，日志打 `[{trace_id}]` 前缀；
  新增 `logs/agents/`、`logs/audit/`、`logs/health/`、`logs/edc/`。

## 5. 后果

**正面**：单次请求端到端可回溯（按 `trace_id` 过滤即一条线）；链式调用树可见；
评估闭环拿到稳定结构化数据源。

**负面 / 需接受**：
- JSONL 持续增长，需要保留周期（**至今未落地**，由心跳巡检承接仍是待办）。
- **已知缺口：`tracer.audit()` 没有 `trace_id` 参数**（内部固定空串），
  审计与 trace 目前未打通——想回答「这次变更的审计记录对应哪条链路」做不到。
  补这一步要单独决策，不要顺手改签名。
- cc-connect 子进程内部不可见；p50/p95/p99 是排序近似值。

**必须配套的约束**：
1. 每条入站消息必须建唯一根 `TraceContext`，请求内所有 span 复用它（`trace_id` 不变）。
2. 链式调用每步用 `root_ctx.child()`，保证 `parent_span_id` 成树、无环。
3. `input_text` / `output_text` 截断到 200 字符；**禁止**把完整 SQL、审批/发布/配置变更内容写进 trace。
4. trace 写盘异常不得冒泡到业务。

## 6. 相关文档

- 存续实现：`app/observability/__init__.py`、`app/observability/task_record.py`
- 数据源消费方：`app/observability/dashboard_service.py`（控制台）、`app/eval/edc/`
- 血统相关的后续决策：`ADR-005`（评估层如何消费这些指标）、`ADR-006`（迁 dsh 后数据源要换成 session 事件流）
