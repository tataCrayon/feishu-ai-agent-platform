# 功能简报：路由与分派语义（routing-dispatch）

> 改这个功能前读这一页即可动手，不必预读实现。与代码矛盾时以代码 + commit 为准并回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：docs/specs/_feature-brief-template.md

## 1. 一句话

谁来决定「这条消息交给谁」这件事已经移交给 dsh 内核：**路由靠技能目录的语义匹配，
执行靠 MCP 工具**。本页守的是两者交界处的语义——能力名必须落在目录里、原文必须
原样到执行方、失败要如实说。

## 2. 主链路

```
main.on_message → RuntimeGuard → DshGateway.handle(text, chat_id, caller=…)
  → dsh 会话：按**技能目录**（deploy/dsh/agentbiz/skills，白名单同步产物）语义匹配
      → 首行写能力名，然后调会话内 MCP 工具
      ├─ capabilities → 进程内 Handler（webportal / digital-oracle / cmdb-watch /
      │                  prd-watch / schedule / skill-catalog）
      │                  提供 run_with_message 的 Handler 走聚合分派（多轮连发+附件，透传原文）
      └─ feishu / domains / agent.yaml 声明的数据源
  → 答复正文回飞书；TaskRecord + 记忆捕获
```

回切链路（`[projects.dsh].enabled = false`）仍是
`router（app/orchestrator/router.py）→ crew agent 节点 → gather`，语义要点相同，
细节见本页 §6 的历史锚点。

## 3. 期望行为

- 用户多轮补充（改写指令+附件）合并后，执行方收到的是**用户原话**，不是路由中间产物
- 路由不确定 → 模型如实说明边界，不编、不塞给兜底 Agent
- **能力对上但工具失败 → 首行仍写该能力名**，正文说清失败原因；写成
  `direct-answer` / 「做不到」会让用户以为能力不存在
- 读会话历史（`read_conversation`）与附件解析是路由判断的一部分，不是「先查一下」

## 4. 约束与不变量（改前必读）

- **能力名必须来自技能目录**：`aihot` / `webportal-agent` / `schedule-agent` /
  `digital-oracle-agent` / `cmdb-watch-agent` / `prd-watch-agent` + `clarify` /
  `direct-answer` / `help`。目录里没有的名字等于这段回复没有归属
- **技能目录 = 路由候选集**：新增能力只加 `bot/agents/*/agent.yaml` 不跑
  `sync_skills.py`，模型看不见它；反过来，工具型技能塞进目录会与业务能力抢路由
- **不做全局加载**：`includeDefaultRoots: false`，个人 `~/.agents/skills` 一律不进候选集
- **聚合分派必须透传用户原文**：路由改写 / intent_summary 不得替代原文传给 Handler——
  改写文本会污染工单解析（证据：commit `9cc23e5`，2026-09-18）
- **工单意图的路由身份是 webportal-agent handler**：skill-order-sql 合成 Agent 已下线勿恢复
  ——它会抢路由（证据：commit `7d5eb6d`，2026-09-18）
- **定时任务的执行面跟消息链路同一条**：`ScheduledTaskExecutor` 优先 `DshGateway`，
  否则到点产出与手动提问会得到两套结果
- **「不改」清单**：不要在 Python 里手写一份能力清单副本；dsh 侧规则改动须过
  `prompt_eval` + `deploy/dsh/verify_routing.py` 双层门禁

## 5. 单源与派生物

- 单源：`bot/agents/*/agent.yaml`（description=能力语义）+ `deploy/dsh/agentbiz/skills.allowlist`
  （进路由候选集的名单）+ `bot/prompts/router/dsh_routing_rules.md`（系统提示词，
  profile 的 `system-prompt.personaPrefix` 读入；`app/dsh/routing.py` 只做读取与启动校验）
- 派生物：`deploy/dsh/agentbiz/skills/`（`sync_skills.py` 生成，gitignore）；
  回切链路的工具表（registry 派生）

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-11-05 | 消息链路整体迁到 dsh：路由从「本仓 RouterLLM + 工具表」改为「dsh 按技能目录语义匹配」。旧链路保留为回切保险 | `app/dsh/` + `deploy/dsh/agentbiz/`（迁移行为契约 `docs/specs/20261104-dsh-migration.md`） |
| 2026-11-06 | 规则从「首条 user 消息」搬进 system prompt（`personaPrefix` 段），并删掉首行能力名契约：该契约全仓无解析方，只让模型多输出一行；同时规则被当普通对话参与压缩。判分从「读首行标签」改为「看实际调了什么工具」——golden 6 例抽样 100% | `deploy/dsh/agentbiz/cordis.patch.yml`（system-prompt 段）+ `dsh_routing_rules.md` v2.0.0 + `verify_routing.py::_infer_label` |
| 2026-11-05 | 补规则「先定能力，再看工具」：实测模型会因「工具可能失败」把首行改写成 `direct-answer`，CMDB/PRD 类 golden 掉到 50%；加规则后回到 100% | `dsh_routing_rules.md` v1.1.0 |
| 2026-11-06 | 定时任务执行面接到 dsh：之前走 CrewOrchestrator，dsh 启用后到点产出与群里手动提问不一致 | `app/ops/scheduler/executor.py::_run_instruction` |
| 2026-11-02 | 进程内通道分派加两级时限：>90s 转后台直投、>`agent.yaml` 的 `timeout_sec` 才放弃。**根因**：`timeout_sec` 自 2026-08 起只被解析、全仓无消费者，长任务（观测最长 1100s）一路占住并发闸额度 → 后续消息排队到 `queue_timeout` 回「系统繁忙」 | `crew._inproc_with_deadline` + `app/orchestrator/test_inproc_deadline.py`（回切链路，dsh 侧由 ACP 超时接管） |
| 2026-09-18 | 工单聚合分派透传用户原文，隔离路由改写污染 | commit `9cc23e5` |
| 2026-09-18 | 工单意图回归 webportal-agent handler，skill-order-sql 合成 Agent 下线 | commit `7d5eb6d` |
| 2026-11-24 | 定时任务三处执行可靠性修补：投递判 `SendResult.success`（拒收不抛异常，只看「没抛」会把没送到的记成成功）；`compute_next_run` 落后就改 `now+interval`（否则停机欠下的轮次每轮扫描补一次、连刷到追平）；调度器加 `_in_flight` 去重（执行超过扫描周期时会并发起两次、抢同一个 dsh 会话）。另补 `ScheduledTaskService.on_agents_reloaded` 接线——此前全仓无调用点 | `scheduler/executor.py::execute`、`scheduler/loop.py::{compute_next_run,_scan_once}`、`main.py` 启动段 |
| 2026-10-12 | 删除 judge+recover 节点：预路由与后判定的双模型割裂拆掉，判定内联回执行方；178 条档案实测 recover 救回率 0.6% / 占 24.5% 成本 / 慢一倍 | 见 `bot/prompts/CHANGELOG.md` 2026-10-12 |
| 2026-10-12 | 目录重构为 agent-platform 分层（gateway/orchestrator/agents/memory/protocols/eval/guardrails/observability/ops/domains + 共享内核留 app/ 根） | 见 CLAUDE.md「目录地图」 |

## 7. 已知坑

- dsh 侧改路由规则后先跑 `python deploy/dsh/verify_routing.py`（真拉起进程 + 真技能目录）；
  `router_intent_eval.py` 现在测的是回切链路，两者不能互相当作对方的门禁
- 新增能力忘记 `sync_skills.py` → 模型完全看不见（表现是「这个我做不了」，不是报错）
- `session/new` 直接失败 = 某个 MCP 子进程起不来，表现是「bot 完全不回话」；
  先逐个 `enabled=false` 定位，不要怀疑模型
- 网关 IP 未加白时 golden 全落 `direct-answer`（403）：这是环境问题，先与 HEAD 基线比数量

## 8. 演练记录（spec-driven 修改实验）

| 日期 | 需求 | 未预读实现？ | 一次通过？ | 返工原因 |
|---|---|---|---|---|
