# feishu-agent-platform × dsh 迁移 — 需求规格说明

> Spec ID：`SPEC-20261104-dsh-migration`
> 创建人：@devuser（AI 起草）
> 创建日期：2026-11-04
> 状态：**执行中**（P0-P2 已完成，P3/P6 进行中；阶段进度见 `docs/project-index.md`「dsh 迁移阶段进度」）
> 关联设计：`docs/design/20261104-dsh-full-migration-spike.md`（实测数据）
> 关联决策：`docs/adr/006-dsh-migration.md`（为什么换、备选与代价）
> 取代：`SPEC-20260918-dsh-refactor`（9-18 草稿，结论已被实测推翻，见 `docs/design/20261104-dsh-full-migration-spike.md` §2）

---

## 一、背景与目标

把 bot 的技术底座换成 DeepSeek Harness：执行、技能、路由、会话、记忆全部由 dsh 提供，
feishu-agent-platform 只保留**用户面**（飞书收发、进度可见）与**治理面**（审批、权限、审计、门禁）。

目标不是「换库」，而是：**这些能力不再由我们持续建设，bot 直接就具备**。
同时**现有能力一项不丢**——17 个 Agent、7 个 Skill、热插拔审批、三红线、golden 门禁全部平移。

AI 实现者读完应能复述：用户看到的东西不变（甚至更流畅），但底下那 3 万行自研编排没了，
换成一个 dsh 进程 + 一层 MCP 工具 + 一套技能目录 + 一层飞书网关。

---

## 二、用户操作时序

### 时序 1：普通用户 — 复杂任务跑得动、看得见

```gherkin
actor: 飞书用户（L0/L1）
goal: 发一个多步任务，机器人真的做完，且中途能看见在干什么

场景: 多步任务带进度
1. Given 群里有人 @机器人 说"把这份材料处理了"（附 3 个附件）
   When  消息发出
   Then  [用户看到] 秒回 ack「已接收 1 条，正在处理」        #走查①④
2. When  dsh 开始执行、第一次调用工具
   Then  [用户看到] 一张进度卡，标题是意图复述，卡内逐条追加
         「🔧 已调用 xxx（第 N 步）」，同一张卡原地更新不刷屏  #走查②③④
3. When  执行完成
   Then  [用户看到] 最终答案并入同一张卡收尾，尾注「🤖 由 XX 处理」  #走查④

场景: 中途叫停
1. Given 任务已跑 2 分钟
   When  用户发"停"
   Then  [用户看到] 进度卡标注「已中断」，已完成步骤保留可见  #走查④
```

### 时序 2：普通用户 — 多轮对话连续

```gherkin
actor: 飞书用户
goal: 同一个话题连着问，不用每句都把上下文再讲一遍

场景: 连续追问
1. When  用户问"06 和 07 目录有点重叠，怎么重构"
   Then  [用户看到] 具体建议
2. When  用户紧接着说"那就按第二个方案，先做 06"
   Then  [用户看到] 直接接着做，不反问"你说的是哪个方案"    #走查③④

场景: 澄清后接着答
1. When  用户说"更新一下"（信息不足）
   Then  [用户看到] 一条反问，列出 2-3 个具体选项
2. When  用户回"nacos 那个"
   Then  [用户看到] 直接去查 nacos，不重新泛泛反问           #走查③④
```

### 时序 3：管理员 — 发技能包，审批后立即可用

```gherkin
actor: L2 管理员
goal: 发一个技能包，点一下就能用上，不重启服务

1. Given 管理员私聊发送技能 zip
   When  文件到达
   Then  [用户看到] 扫描结果卡（名称/风险等级/摘要/审批按钮）  #走查②
2. When  管理员点「接入」
   Then  [用户看到] 回执「已接入」
3. When  管理员下一条消息触发该技能
   Then  [用户看到] 技能直接生效，不需要重启、不需要新开会话  #走查④
```

### 时序 4：管理员 — 装社区插件

```gherkin
actor: L2 管理员
goal: 想加个新能力，能从插件市场选着装

1. Given 管理员在设置页打开插件市场
   When  选中一个插件点安装
   Then  [用户看到] 安装进度与结果
2. When  安装完成
   Then  [用户看到] 新能力出现在列表里，下一轮对话可用        #走查④
```

### 时序 5：异常 — 执行服务挂了

```gherkin
actor: 飞书用户
goal: 服务不可用时，要知道发生了什么、下一步做什么

场景: dsh 进程崩溃
1. Given 用户发了一个任务
   When  dsh 进程在执行中崩溃
   Then  [用户看到] 「执行服务重启中，本轮已中断，请重发」+ 已跑步骤保留
         不允许静默无响应                                     #走查④
2. When  用户重发
   Then  [用户看到] 正常执行（不需要用户做任何额外操作）
```

### 用户能看到什么（输出物清单）

| 输出物 | 触发步骤 | 形态 | 必含信息 |
|---|---|---|---|
| ack 回执 | 时序1-1 | 飞书文本 | 收到条数 + 正在处理 |
| 进度卡 | 时序1-2/3 | 飞书卡片（原地更新） | 意图复述 + 步骤事件（≤10 条）+ 终态 |
| 澄清反问 | 时序2-澄清 | 飞书文本 | 2-3 个具体选项，不是开放式提问 |
| 审批卡 | 时序3-1 | 飞书卡片 | 名称/风险等级/摘要/按钮 |
| 插件市场 | 时序4 | 设置页 | 插件列表 + 安装状态 |
| 崩溃提示 | 时序5-1 | 飞书文本 | 原因 + 已保留内容 + 下一步 |

### 时序 6：老用户 — 记得上一次

```gherkin
actor: 飞书用户（次日再聊）
goal: 不用把昨天说过的事重讲一遍

场景: 跨会话事实复用
1. Given 用户昨天让 bot 处理过某件事、并说过"以后默认用测试环境"
   When  今天用户说"还是那件事，再查一次"
   Then  [用户看到] 直接按测试环境 + 原对象查，不反问"哪个对象/哪个环境"   #走查③④
```

---

## 三、需求分解

### R1 执行面迁移 → 时序1、时序2

dsh 进程（ACP stdio）接管全部 agent 执行。会话按 `chat_id` 常驻，上下文天然连续。

**AC-1.1**：同 `chat_id` 连续两条消息，第二条不重复上下文即可被正确理解（实测基线：同 session 二轮准确复述首轮问题）。

**AC-1.2**：单任务工具调用轮次不设人为上限，由 dsh 的 agent loop 决定。

**AC-1.3**：`session/update` 事件能透出 `tool_call` / `agent_message_chunk` / `usage_update`（实测已通）。

### R2 路由迁移 → 时序1、时序2

现有 17 个 Agent 的 `description` 作为能力描述进 dsh，路由由 dsh 完成；`confidence` 自报，低于阈值转澄清。

**AC-2.1**：路由在**真实部署形态**（技能目录自动发现，非手拼提示词）下，76 例 golden 通过率 ≥92%（实测基线 94.7%，见 spike §3.2.1）。

**AC-2.2**：同一输入连跑 3 次，路由结果一致（实测 3×3 全稳定）。

**AC-2.3**：澄清由 `ask_user_question` 在同会话内阻塞等待，用户答复后**不重新路由**直接继续。

**AC-2.4**：原 `app/orchestrator/`（`crew.py`/`router.py`/`direct.py`/`reroute_rules.py`）全部退役。

**AC-2.5**：direct-answer / clarify 边界用例单独统计，不计入 AC-2.1 总分（属提示词问题，非路由问题）。

**AC-2.6**：路由边界规则（能力盘点双出口、能力是非题等）以会话首条前置消息注入，单源为一份规则文件，可被 golden 回归验证。

### R3 飞书能力下沉 MCP → 时序1、时序5

`app/gateway/channel/` 与 `lark_cli.py` 的能力暴露为 MCP 工具，由 dsh 调用。

**AC-3.1**：`send_message` / `read_doc` 等工具可从 dsh 会话调用成功（实测已通）。

**AC-3.2**：三红线（chat_id 必须 `oc_`/`ou_` 开头等）在**工具层硬拒**，不依赖提示词（实测工具返回 `REJECT:`）。

**AC-3.3**：`safety-rules.json` 12 例中，可二值判定的项 100% 拦截；行为标签类（如「自动脱敏」）重写为可测断言。

### R4 技能体系 → 时序3

`bot/skills/` 迁移到 dsh 技能目录，**零改写**（格式同构，实测 dsh 直接发现并加载）。

**AC-4.1**：现有 7 个 skill（aihot / demo-guard / digital-oracle / elk-log-query / news-sql-generator / x / @fireflywwj）按白名单分层：业务能力进 dsh 技能目录，工具技巧不进（判据与实测依据见 `docs/design/20261104-dsh-full-migration-spike.md` §2、`docs/features/routing-dispatch.md` §4）。

**AC-4.2**：技能正文按需加载，不预载进每轮提示词（实测 `skill` 工具加载后才读正文）。

**AC-4.3**：新增/修改/删除技能文件**免重启生效**（Chokidar，实测 dsh 原生支持）。

### R5 技能可控 → 时序3、时序4

技能必须能开关、能隔离个人资产。

**AC-5.1**：线上 profile 设 `includeDefaultRoots: false` + 显式 `customSkillDirs`，**不加载** `~/.agents/skills` 与 `~/.claude/skills`（实测默认会加载并引用其中行号）。
同一条口径覆盖**会话指令**：`dsh-agent-instructions` 默认注入 `~/.agents/AGENTS.md`（个人全局工作流）与会话 cwd 链上的 `AGENTS.md`/`CLAUDE.md`（本仓那份是开发约定），bot 必须整插件禁用并另设 `DSH_AGENTS_HOME`，指令唯一来源是 `app/dsh/routing.py`。

**AC-5.2**：单个技能可用 frontmatter 的 `disable-model-invocation: true` 从模型可见目录摘除，`user-invocable: false` 从人工命令摘除（dsh 原生）。

**AC-5.3**：管理端可整体启停一个技能，行为与 AC-5.2 等价。

### R6 热插拔保留 → 时序3

**AC-6.1**：capability ingest 的扫描（凭据/危险指令/zip-slip，high 自动拒）+ L2 审批卡流程不变。

**AC-6.2**：审批通过后写 `bot/skills/` + 追加白名单 + 触发同步，**不重启**即可路由到新技能。

**AC-6.3**：覆盖同名词旧版备份进 `bot/inbox/archive/` 的行为保留。

**AC-6.4**：技能目录内每个 SKILL.md 都必须能在白名单找到（脚本校验，防止绕过审批直接落盘）。

### R7 插件安装能力 → 时序4

**AC-7.1**：可安装社区 dsh 插件（`dsh plugin --profile <name> add <pkg>`），安装后下一轮可用。

**AC-7.2**：插件操作走 L2 审批，与技能准入同一道门。

### R8 现有 Agent 保留 → 时序1

17 个 agent 的能力全部保留，按 transport 重新归类：

| 类别 | Agent | 迁移方式 |
|---|---|---|
| 确定性引擎（保留 Python 逻辑） | digital-oracle-agent、schedule-agent、skill-catalog-agent | 包成 MCP server，工具层调用 |
| 纯能力型 | 外部数据源类 agent | 由对应 MCP server 提供工具，agent 定义简化为 preset |
| 维护者通道 | dev-bot | 保留 `/dev` 直达，`routable: false` 不变 |
| skill 合成 agent | skill-aihot、skill-demo-guard、skill-elk-log-query、skill-news-sql-generator、skill-x | 不再合成 agent，直接是 dsh 技能 |

**AC-8.1**：每个 agent 至少 1 条真实输入端到端跑通，输出与迁移前可比。

**AC-8.2**：`bot/agents/*/agent.yaml` 保留为**能力元数据**（显示名/权限/示例问法），不再派生路由工具表。

### R9 记忆一体 → 时序2、时序6

**AC-9.1**：MemoryCore 的**独立进程与 HTTP 服务形态移除**（`app/memory/lifecycle.py`、`config.toml [projects.memory.lifecycle]`）。

**AC-9.2**：capture/recall 的能力**保留**，以 MCP 工具形态内嵌进 bot，与 bot 一体部署；检索与存储逻辑复用 MemoryCore 现有实现。

**AC-9.3**：dsh 会话持久化承担同会话多轮（R1），memory MCP 承担跨会话用户画像与事实沉淀。

**AC-9.4**：迁移前的多轮质量不回退——时序 2 两个场景必须通过；跨会话画像场景可用。

**AC-9.5**：`recall` 走 turn 开始前的自动注入（hook），不依赖模型主动调用；`capture` 走 turn 结束后的自动落盘。

**AC-9.6**：L0 原始对话以 dsh session JSONL 为单源；memory 只存抽取后的事实（L1-L3）。`conversation_search` 不迁移。

**AC-9.7**：`session_end` **不实现**为独立接口。MemoryCore 已有两个兜底（每 `everyNConversations=5` 轮自动抽取、`l1IdleTimeoutSeconds=600` 空闲 flush），显式 flush 属冗余；若后续发现抽取延迟影响画像质量，再以 hook 形式补。

**AC-9.8**：`session_key` 统一为 `feishu:{chat_id}`（会话维度），用户维度走 `user_id=sender_id`；现状三处口径不一（`main.py:555` 裸 chat_id、`main.py:709` 裸 chat_id、`crew.py:315` `feishu:{chat_id}:{sender_id}`）必须收敛。

### R10 可观测与控制台 → 全部时序

**AC-10.1**：TaskRecord 数据源改 dsh session JSONL（逐事件，比现状 tool 级不落盘更完整）。

**AC-10.2**：控制台「任务档案」「费用统计」「报告」tab 功能不回退。

**AC-10.3**：一次任务可回放完整事件流（含工具调用）。

### R11 评估能力 → 全部时序

现有 9 个评估脚本必须全部存活，且迁移后仍能跑：

| 脚本 | 职责 | 迁移后归属 |
|---|---|---|
| `prompt_eval.py` | golden 静态检查（提示词是否编码了期望行为） | 保留，适配新提示词体系 |
| `prompt_index.py` | prompt INDEX 一致性 | 保留（提示词单源仍在 `bot/prompts/`） |
| `router_intent_eval.py` | 真实调用路由，比对 golden 72 例 | **改写**：路由改由 dsh 完成，评测目标不变 |
| `verify_spec.py` | 按 spec AC 逐条断言 | 扩展本 spec 的 AC |
| `e2e_functional_test.py` | 端到端整链路（不经飞书） | 保留，主链路改为 dsh |
| `user_auth_e2e.py` | per-sender 授权 Device Flow 全链 | 保留不动 |
| `comment_lint.py` | 注释纪律（变更日志式注释拦截） | 保留不动 |
| `edc/` | EDD 指标 | 保留，数据源改 session JSONL |
| `context_budget_replay.py` | 上下文预算回放（一次性实验） | 保留归档 |

**AC-11.1**：9 个脚本全部可运行，无一因迁移而失效。

**AC-11.2**：`router_intent_eval.py` 改写后，72 例 golden 通过率 ≥95%（与 P0 实测一致）。

**AC-11.3**：`verify_spec.py` 覆盖本 spec 全部 AC。

### R12 外部 Agent 对接 → 时序1

外部能力可以独立仓库承载（A2A server 或 stdio MCP server），本仓只做两种投影之一：

- **MCP 投影优先**：工具契约一致，dsh 原生挂载 stdio MCP，`[projects.mcp.<name>]`
  里写 `command/args/cwd` 即可；
- A2A 投影：留给需要多轮聚合与文件透传的能力，客户端壳留在 Handler。

**AC-12.1**：dsh 会话能挂载并调通外部 MCP server 的至少一个工具。

**AC-12.2**：Handler 只做协议壳，判定逻辑全在外部服务，本侧不重复实现。

### R13 退役清理

**AC-13.1**：`app/orchestrator/`、`app/agents/handlers/`、`app/protocols/cc_connect/` 删除；`app/memory/` 改为 MCP 内嵌后删除进程托管部分。

**AC-13.2**：`cc-connect` daemon 与 `~/.cc-connect/cc-exec.toml` 不再被使用。

**AC-13.3**：`docs/project-index.md` 同步收敛，无指向已删模块的行。

---

## 四、验收流程约定

1. **时序走查**：时序 1-5 逐场景真实执行，断言 `[用户看到]` 的内容。无法自动化的（飞书客户端点按钮、卡片渲染）输出人工走查清单，单条 ≤2 分钟。
2. **AC 断言**：`app/eval/verify_spec.py` 扩展本 spec 的 11 组 AC，逐条 PASS/FAIL/SKIP + 证据；任一 FAIL 修复后重跑全套。
3. **golden 门禁**：`router-intent.json` 72 例 ≥95%、`safety-rules.json` 改写后可测项 100%。
4. **回滚验证**：每个阶段结束时验证上一阶段仍可回切（`transport` 开关或 git revert）。

---

## 五、不做（Out of Scope）

| 事项 | 原因 | 后续 |
|---|---|---|
| 多租户隔离 | dsh 无租户模型；继续由 feishu-agent-platform 前置（会话映射 + 并发闸） | 用户量 >10x 再议 |
| 暴露 dsh Web UI | loopback 单用户 GUI，与自建控制台定位冲突 | 不做 |
| 多 dsh 进程/水平扩展 | 团队规模用不到 | 后续 |
| 嵌入模型/分类器强化路由 | dsh + 现有模型已达 97.2% | 独立议题 |
| 飞书以外的渠道 | 本期只飞书 | 后续 |

---

## 六、待确认（阻塞项）

| # | 事项 | 状态 |
|---|---|---|
| Q1 | bot 的 `DSH_HOME` 用独立目录，与桌面版隔离 | ✅ 已定：独立 |
| Q2 | 飞书 MCP server 用 Python（复用 `lark_cli.py`） | ✅ 已定：Python |
| Q3 | `agent.yaml` 保留 `name/display_name/description/permission_level/enabled` | ✅ 已定 |
| Q4 | 迁移窗口：允许每阶段停机 10 分钟 | ✅ 已定 |
| Q5 | memory 与 bot 一体（MCP 内嵌，不独立进程） | ✅ 已定 |
| Q6 | 业务域（`app/domains/*`）由能力 Owner 持续开发，本重构只改调用入口 | ✅ 已定 |

实施顺序与阶段进度见 `docs/project-index.md`「dsh 迁移阶段进度」。
