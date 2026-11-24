# 路由意图识别（Router 三态出口）— 行为契约

> Spec ID：`SPEC-20260912-router-intent` ｜ 创建 2026-09-12
> **适用面**：仅回切链路（`[projects.dsh].enabled = false` 时的 `app/orchestrator/`）。
> dsh 链路的路由契约在 `SPEC-20261104-dsh-migration` R2 + `docs/features/routing-dispatch.md`。
> 验收执行器：`python app/eval/verify_spec.py [--with-llm]`（AC 断言在脚本里，本文件是它的口径来源）

## 一、要解决的问题

旧路由是 function calling 二态输出（选 agent / 自答），由此带来四个缺陷：
无置信度、无澄清出口（拿不准也硬选）、决策不可审计（散在 logger 文本里）、
测试不闭环（golden 只验提示词里有没有关键词，不验真实消息进来选了什么）。

## 二、用户操作时序

```gherkin
actor: 飞书用户（L0/L1/L2 皆可）
goal: 一句话说清需求，要么办对，要么反问确认，绝不猜错方向沉默执行

场景: 清晰意图直达
1. Given 我在群里说"查一下这张表的结构"
   When  消息发出后
   Then  [用户看到] 去向回执含 agent 显示名，随后收到结论

场景: 模糊意图反问
1. Given 我在群里说"更新一下"（说不清算什么）
   When  消息发出后
   Then  [用户看到] 反问一句确认（含候选方向），而不是猜一个方向乱执行

| 输出物 | 形态 | 必含信息 |
|--------|------|---------|
| 去向回执 | 飞书消息 | agent 显示名 |
| 澄清反问 | 飞书消息 | 反问文本（含候选方向） |
```

## 三、需求与验收

### R1 路由工具 schema 扩展 → 支撑回执质量

所有路由工具（派生的 `route_to_xxx` + 内置 `direct_answer` / `route_to_help`）
统一追加两个可选参数：`confidence`（0-1）与 `intent_summary`（意图复述）。

- **AC-1.1** 派生工具全部含 `confidence` + `intent_summary`
- **AC-1.2** `RouterResult` 承接自报字段

### R2 澄清路由：三态出口之一 → 模糊场景

`route_to_clarify` 是路由工具之一（不是 agent），走 `clarify_node`。

- **AC-2.1** `route_to_clarify` 在工具表且不在 agent 表
- **AC-2.2** graph 含 `clarify_node` 且条件边可达
- **AC-2.3** `clarify_node` 输出非空且不触发结构化门禁

### R3 低置信度降级 → 反问的触发机制

`confidence` 低于 `clarify_threshold`（默认 0.6）时把目标改写为 clarify，原目标保留在 `original_target`。

- **AC-3.1** `confidence=0.3` 降级为 clarify 且 `original_target` 保留
- **AC-3.2** `confidence=0.9` 保持原路由
- **AC-3.3** `clarify_threshold` 可配为 0（永不降级）

### R4 路由决策日志 → 事后可回答「当时为什么路由到 A」

TaskRecord 与独立决策日志 `logs/decisions/{date}.jsonl` 双写，同 `task_id` 关联。

- **AC-4.1** TaskRecord 含 `intent_summary` / `confidence` / `demoted` / `clarify_question`
- **AC-4.2** 决策日志落盘且字段齐备
- **AC-4.3** 通过决策日志可重建 `query → tool → confidence → demoted` 链路

### R5 执行日志补 `query_preview` → 分派给 agent 的实际文本

agent 执行 span 增加 `query_preview`（最终文本前 120 字，含改写）。

- **AC-5.1** agent span 的 `query_preview` 非空

### R6 golden 套件 v2：行为级回归 → 时序走查的可回归载体

`bot/prompts/_golden/router-intent.json`：覆盖每个 agent 的清晰正例（≥2/agent）、
自答正例（≥3）、模糊用例（≥3，期望 clarify）、多轮指代（≥2）。
执行器 `app/eval/router_intent_eval.py` 真实调 LLM，断言实际 tool 选择，`--min-score` 门禁。

- **AC-6.1** 执行器可运行并输出逐条结果
- **AC-6.2** clarify 用例在真实 LLM 下命中
- **AC-6.3** 总分达门槛时退出码 0，否则非 0

### R7 验收脚本 → 本 spec 的执行器

`app/eval/verify_spec.py` 内置本 spec 可静态/冒烟验证的 AC；LLM 项由 `--with-llm` 开关控制。

- **AC-7.1** 输出 ≥12 条 AC 断言
- **AC-7.2** `--with-llm` 时含 R6 结果

### R8 三道闸不回退

- **AC-8.1** ruff 改动域 0 错
- **AC-8.2** mypy 不超基线
- **AC-8.3** `prompt_eval` 全量通过

## 四、不做

- 不引入嵌入模型 / 分类器（置信度用 LLM 自报，够用且零依赖）。
- 不改 judge/recover（clarify 是路由出口，与纠错闭环正交；judge 已于 2026-10-12 删除）。
- 不做多候选并行试探（同一条消息发两个 agent 择优——成本高）。

## 五、验收流程

1. `python app/eval/verify_spec.py --with-llm` 出 PASS/FAIL/SKIP 报告。
2. 对 §二 逐场景做时序走查，填 `docs/checklist/user-journey-walkthrough.md`。
3. 任一 FAIL 必须修复后重跑全套，不接受部分通过即交付。
