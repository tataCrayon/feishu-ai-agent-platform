# ADR-001：采用 cc-connect 桥接飞书与 Claude Code

> ADR 编号：`ADR-001` ｜ 状态：**⛔ 被 `ADR-006` 取代**
> 创建 2026-08-14 ｜ 决策者 @张三
> 取代后现状：cc-connect 只保留 `/dev` 维护者通道（`app/protocols/cc_connect/`），
> 消息主链路已走 dsh（见 `ADR-006`）

## 1. 背景

飞书消息只是一条 IM 事件流，本身不含推理能力。要做一个 7×24 在线的群聊助手，
必须有一个常驻桥接进程把「飞书事件」翻译成可长期运行的推理会话并把结果发回去。
同时希望直接复用 Claude Code 已有的 Skills、MCP 工具、`.notes/` 知识资产与文件系统记忆，
而不是从零重写推理层。

## 2. 决策考虑

- **触发因素**：@触发 → 推理 → 回复的闭环不能靠人工进终端驱动。
- **约束条件**：无公网地址（只能长连接）；单机 Windows 部署；零外部基础设施；
  团队已有大量 Claude Code 资产要复用。
- **非目标**：不自研推理引擎；不做多平台接入；不做集中式 APM。

## 3. 决策选项

### 选项 A：cc-connect 桥接

- **描述**：`config.toml` 声明式配置，cc-connect 以长连接订阅飞书事件，
  把消息路由到对应 Claude Code 项目会话，推理结果经 `platforms` 回发。
- **优点**：零推理层重写，Skills / MCP / CLAUDE.md / 记忆全沿用；一个飞书 App 挂多个
  Agent 项目，天然多 Agent 隔离；长连接、卡片、心跳、auto_compress、限流全内置。
- **缺点与代价**：黑盒——配置坑点与默认值随版本变化会**静默失效**；对版本升级敏感；
  不能从 Claude Code 会话内启动（必须独立终端/daemon）。
- **适用场景**：Claude Code 生态已有积累、无公网资源、需要复用 Skills/MCP 的中小群聊机器人。

### 选项 B：自研全链路网关（扩展现有 daemon）

- **描述**：自行调飞书 OpenAPI 收发、自管会话恢复与上下文压缩、自行接 Agent SDK。
- **优点**：行为 100% 可预期，无第三方黑盒。
- **缺点与代价**：事件订阅、卡片回调、会话 checkpointer、压缩、心跳、限流全要自建；
  在无公网约束下等于重做 cc-connect 的一部分；无法直接复用 Skill 渐进加载。
- **适用场景**：需要深度定制消息语义 / 多平台 / 特殊事件类型。

### 选项 C：Lark 官方 Python SDK 自建 bot 服务

- **描述**：`lark-oapi` 建常驻服务，推理走 REST 或 Agent SDK。
- **优点**：官方 SDK 文档全、飞书 API 覆盖最完整、升级风险可控。
- **缺点与代价**：与 B 共享大部分自研代价，且丧失 Claude Code 生态复用；
  「既没有官方桥接的成熟度，也没有自研的全掌控收益」。
- **适用场景**：以飞书为中心、大量定制卡片、不强依赖 Claude Code Skills。

## 4. 决策结论

**选定：选项 A。**

- **核心理由**：① 零推理层重写，避免两套推理栈并存；② 一 App 多 Agent，
  MCP 最小权限隔离与 Agent 职责天然对齐；③ 长连接 + 卡片 + 心跳 + 压缩全内置，
  自研约需数周。
- **放弃其他选项**：B 违背无公网约束且成本高；C 两头都不占。
- **影响范围**：`config.toml`（projects / provider / platform / commands / webhook）、
  飞书开放平台事件订阅、`.agent/` 下的记忆与评估工具链。

## 5. 后果

**正面**：群聊助手 7×24 在线；扩容 = 新增一个 `[[projects]]`；与 Claude Code 生态同构。

**负面 / 需接受**：
- 升级后可能静默失效 —— 升级必跑一遍坑点清单。
- 排查主入口只有 cc-connect 日志，黑盒内部（会话状态、压缩触发、provider 切换）不可细粒度观测。
- 推翻成本约数周（事件订阅 + 卡片回调 + checkpointer + 压缩四块要重做）。

**必须配套的约束**：
1. 配置坑点沉淀成清单再升级（`admin_from` 必须写 `[[projects]]` 层级、
   开放平台必须同时订阅 `card.action.trigger`、`reset_on_idle_mins` 默认 30min 等）。
   清单现存 `.notes/patterns/troubleshooting/cc-connect-pitfalls.md`。
2. 飞书消息只许一个消费者：同一 App 双长连接会竞争消费（2026-09-14 实测
   `/help` 透出 cc-connect 内置帮助、档案断流）。

## 6. 相关文档

- 被 `ADR-006` 取代（执行面迁到 dsh）
- 存续部分的坑点清单：`.notes/patterns/troubleshooting/cc-connect-pitfalls.md`
- 投递可靠性（task:tag + 迟到结果守望）：`docs/features/cc-connect-delivery.md`
