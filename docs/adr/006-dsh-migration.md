# ADR-006：feishu-agent-platform 技术底座迁移到 DeepSeek Harness

> 架构决策记录（Architecture Decision Record）
> ADR 编号：`ADR-006`
> 状态：`已接受`
> 创建人：@devuser（AI 起草）
> 创建日期：2026-11-04
> 决策者：@张三（项目负责人）
> 关联项目/模块：全仓（编排层 / 执行通道 / 技能 / 记忆 / 控制台）
> 行为契约：`docs/specs/20261104-dsh-migration.md` ｜ 实测证据：`docs/design/20261104-dsh-full-migration-spike.md`
> 取代：`ADR-001`（cc-connect 桥接）、`ADR-002`（自建 Agent Registry）

## 1. 背景

feishu-agent-platform 用自研编排（LangGraph + 进程内 4 轮 ReAct + cc-connect 桥）承载飞书机器人。
2026-08-24 ~ 2026-10-20 的 246 条任务档案暴露三类结构性短板，且这些能力每修一处都是自建成本：

| 现象 | 实测数据 |
|---|---|
| 执行太浅 | `MAX_TOOL_ROUNDS = 4`；>2min 任务 33 条、最长 1100s |
| 技能用不起来 | 19/246 走 skill（7.7%）；cc-connect `max_sessions = 2` |
| 多轮不连续 | `history_max_turns = 6`；12 个会话里 4 个只有 1 条消息 |

同时诉求从「执行层弱」升级为「整体能力落后豆包」——技能利用、飞书连通、流畅度、可持续对话全面落后。

核心判断：**这些能力不该由我们持续建设，应该由底层 harness 提供。**

## 2. 决策考虑

- 触发因素：自研编排层持续膨胀（`app/` 约 3.7 万行 Python），但用户感知质量不升反降；上游 dsh 已提供同类能力且 MIT 开源。
- 约束条件：
  - 现有能力（17 个 Agent、7 个 Skill、热插拔审批、飞书长连接、权限矩阵、golden 门禁）**不得丢失**
  - 不得依赖个人全局配置（`~/.agents/skills`、`~/.claude/`）——线上 bot 必须自包含
  - 技能准入仍归 feishu-agent-platform（L2 审批 + 静态扫描），不可因换底座而放宽
  - 凭据只放 `.env`，配置里只留 `${ENV}` 占位符
- 非目标：
  - 不做多租户隔离（dsh 无租户模型，继续由 feishu-agent-platform 前置）
  - 不暴露 dsh Web UI（loopback 单用户，与自建控制台冲突）
  - 不改飞书接入方式（长连接 + debounce 保留）
  - 不放弃跨会话记忆（capture/recall 保留，改为 MCP 内嵌，见 P5）
  - 不自建插件生态（直接用 dsh 插件市场与安装链路）

## 3. 决策选项

### 选项 A：全量迁移，dsh 接管执行面 + 技能面 + 路由

- **描述**：飞书 → Aggregator → dsh-adapter（ACP stdio）→ dsh 进程。路由、工具循环、subagent、压缩、会话、技能全部归 dsh。feishu-agent-platform 只留飞书接入、debounce、并发闸、审批、审计、控制台。
- **优点**：
  - 执行能力跟随上游升级，不再自建
  - 技能零改写：dsh 的 `SKILL.md` 格式与现有 `bot/skills/` 同构（实测 dsh 直接发现并加载 `elk-log-query`）
  - 会话常驻 → 多轮天然连续，`history_max_turns` 硬伤消失
  - 路由实测可达标（真实部署形态 94.7%，见实测证据 §2；97.2% 是手拼提示词的实验室数字，不作基线）
  - 飞书能力下沉 MCP 工具层后，三红线从「提示词约束」变「工具层硬拒」
- **缺点 / 代价**：引入预发布依赖；约 3 万行代码退役；dsh 配置成为新知识面
- **适用场景**：本决策

### 选项 B：仅换执行层（`transport=dsh`），路由留自研

- **描述**：9-18 草稿的方案。保留 `app/orchestrator/`，只把 handler 循环换成 dsh 会话。
- **优点**：改动面小，风险低，可逐 agent 回切
- **缺点 / 代价**：双栈维护（技能目录 + agent.yaml 元数据两套事实源）；路由层的短板（多轮丢失、clarify 后上下文断裂）不解决；dsh 侧路由在真实部署形态下已达标，保留自研路由无收益
- **适用场景**：执行层是唯一痛点、且不具备迁移条件时
- **否决理由**：两套事实源必然漂移；收益不足以覆盖维护成本

### 选项 C：保留自研，加深 ReAct 轮数与并发

- **描述**：`MAX_TOOL_ROUNDS` 4→40，cc-connect `max_sessions` 2→8，加自研压缩。
- **优点**：零架构变更
- **缺点 / 代价**：subagent、上下文压缩、沙箱、会话持久化仍要自建；cc-connect 桥接瓶颈不变（进程外、无中间事件）
- **适用场景**：团队规模极小、不打算长期投入时
- **否决理由**：只是把自建成本延迟，不解决「持续建设」的根本问题

### 选项 D：改用 Dify / FastGPT / RAGFlow

- **描述**：换成开源 LLM 应用编排平台
- **否决理由**：是应用编排平台而非 agent runtime；无技能文件体系、无 subagent 委派、无进程内工具循环，且需额外部署与运维

## 4. 决策

**采用选项 A。**

分层边界：

| 面 | 归属 | 内容 |
|---|---|---|
| 用户面 | feishu-agent-platform | 飞书收发、debounce、进度卡渲染、ack 回执 |
| 治理面 | feishu-agent-platform | 并发闸、L2 审批、静态扫描、权限矩阵、golden 门禁、审计 |
| 执行面 | dsh | 多轮工具循环、subagent、上下文压缩、会话持久化、沙箱 |
| 技能面 | dsh（目录）+ feishu-agent-platform（准入） | 技能发现/热加载归 dsh；谁能进目录归审批流 |

## 5. 后果

**正面**：见选项 A 优点。

**负面 / 需接受**：

- 引入预发布依赖（当前 `0.2.1-rc.2`，无兼容承诺）→ **pin 版本 + 升级必走 golden 门禁**
- 约 3 万行 Python 退役，dsh 侧 `cordis.patch.yml` 成为新的知识面
- 单人维护，文档必须与代码同 commit 更新，否则配置漂移无人发现

**必须配套的约束**（否则本 ADR 的收益会被反噬）：

1. 线上 profile 必须关掉默认技能根（`includeDefaultRoots: false` + 显式 `customSkillDirs`），否则个人全局技能会进生产提示词——实测 dsh 会加载 `~/.agents/skills/` 并引用其中行号
2. 安全规则必须下沉到 MCP 工具层硬约束；提示词层实测只有 75%（12 例 golden 中 3 例口径不符）

## 6. 取代关系

- 取代 `ADR-001`（cc-connect 桥接飞书与 Claude Code）：cc-connect 通道退役
- 取代 `ADR-002`（自建 Agent Registry）：`bot/agents/*/agent.yaml` 收敛为能力元数据，不再承担路由工具表派生
- 未取代但受影响：`ADR-003`（trace）、`ADR-005`（评估指标）的数据源在迁移后要改成 dsh 会话事件流，见迁移 spec R10
