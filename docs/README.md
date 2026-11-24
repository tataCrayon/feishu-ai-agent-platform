# 文档中心

> **改功能先查 [project-index.md](project-index.md)**（功能域 → 单源文件 → 生效方式）。
> 项目介绍与使用指南见 [项目 README](../README.md)。
>
> 文档哲学：**文档是负债，不是资产**。每条事实只写一处；写不进「读者 + 具体信息」两问的，
> 不写。制度见 [fresh-loop](specs/20260924-fresh-loop.md)。

## 目录分层（按「读者要用它解决什么」分，不按文件结构分）

| 层 | 目录 | 回答什么 | 何时读 |
|---|---|---|---|
| 功能索引 | [project-index.md](project-index.md) | 这个功能要碰哪些文件、怎么生效 | 动手前第一站 |
| 功能简报 | [features/](features/) | 这个功能怎么跑、什么不能动 | 改某个已有功能 |
| 需求规格 | [specs/](specs/) | 什么算做对了（可测断言） | 写验收 / 判断完成 |
| 决策记录 | [adr/](adr/) | 为什么选 A 不选 B | 质疑设计时 |
| 实测证据 | [design/](design/) | 当时测到了什么数字 | 怀疑结论是否仍成立时 |
| 结构图 | [diagrams/](diagrams/) | 拓扑长什么样 | 讲给别人听时 |
| 验收清单 | [checklist/](checklist/) | 用户座位上的走查步骤 | 发布前 |

## 功能简报（改功能先读这一页）

> 每个高频改动域一页纸：主链路 + 期望行为 + 不变量（带证据锚点）+ 已知坑。
> 模板：[specs/_feature-brief-template.md](specs/_feature-brief-template.md)。

| 域 | 守住什么 |
|---|---|
| [routing-dispatch.md](features/routing-dispatch.md) | 能力名必须来自技能目录；技能目录 = 路由候选集 |
| [aggregator-attachments.md](features/aggregator-attachments.md) | debounce 合并 + 附件归并 |
| [session-context.md](features/session-context.md) | 会话常驻、chat→session 映射、上下文从哪来 |
| [capabilities-mcp.md](features/capabilities-mcp.md) | 能力目录 vs 执行入口两条线 |
| [capability-hotplug.md](features/capability-hotplug.md) | 管理员飞书自助接入 + D1-D4 红线 |
| [skill-catalog.md](features/skill-catalog.md) | 团队技能目录 + 规范状态机 |
| [cc-connect-delivery.md](features/cc-connect-delivery.md) | cc-connect 投递与迟到结果 |

## 需求规格（Spec）

| Spec | 适用面 |
|---|---|
| [20261104-dsh-migration.md](specs/20261104-dsh-migration.md) | **当前主链路**：dsh 迁移 R1-R13 + AC，执行中 |
| [20260924-fresh-loop.md](specs/20260924-fresh-loop.md) | 制度：文档与代码同 commit 更新 |
| [20260912-router-intent.md](specs/20260912-router-intent.md) | 仅回切链路（`dsh.enabled=false` 时的 `app/orchestrator/`） |
| [_template.md](specs/_template.md) | Spec 模板（用户操作时序 §二 必填） |
| [_feature-brief-template.md](specs/_feature-brief-template.md) | 功能简报模板 |

验收脚本：`python app/eval/verify_spec.py [--with-llm]` 按 spec AC 逐条 PASS/FAIL。

## 架构决策记录（ADR）

> 一条决策一个文件，只记背景 / 考量 / 备选项 / 结论 / 后果。
> **accepted 后不改**，被取代时新建并链接。**不写实施手册、不写变更记录、不写复盘表**——
> 那些分别属于 `features/`、git log、下一个 ADR。

| 编号 | 标题 | 状态 |
|---|---|---|
| 006 | [技术底座迁移到 DeepSeek Harness](adr/006-dsh-migration.md) | ✅ 已接受（取代 001/002） |
| 005 | [EDD 评估层集成](adr/005-edd-integration.md) | ✅ 已实现（采集器现位于 `app/eval/edc/`） |
| 004 | [Agent 健康监控与自愈](adr/004-health-monitor.md) | ✅ |
| 003 | [Agent 调用链路追踪](adr/003-trace-chain.md) | ✅ 已实现（部分落点已迁移） |
| 002 | [Agent 注册表](adr/002-agent-registry.md) | ⛔ 被 006 取代 |
| 001 | [cc-connect 桥接](adr/001-cc-connect.md) | ⛔ 被 006 取代（`/dev` 通道仍在使用） |
| — | [_template.md](adr/_template.md) | 📝 模板 |

## 实测证据

`design/` 只保留**不能再测一次**的数字（预发布版本上的对照实验）。

| 文档 | 保留理由 |
|---|---|
| [20261104-dsh-full-migration-spike.md](design/20261104-dsh-full-migration-spike.md) | 技能目录三组对照（80.3% / 89.5% / 94.7%）——`includeDefaultRoots: false` 与白名单的依据 |

## 结构图

`diagrams/` 每张图只有两个文件：`.archify.json`（源，单源）与 `.html`（archify deliver 的生成物，
控制台「架构视图」内嵌）。**不放截图，不放 visual-check 产物**。

| 图 | 控制台入口 |
|---|---|
| [feishu-agent-platform-runtime.archify.json](diagrams/feishu-agent-platform-runtime.archify.json) | `/arch/runtime.html` |
| [feishu-agent-platform-request-lifecycle.archify.json](diagrams/feishu-agent-platform-request-lifecycle.archify.json) | `/arch/lifecycle.html` |

## 验收清单

[checklist/user-journey-walkthrough.md](checklist/user-journey-walkthrough.md)
是**唯一一份要每次填的清单**：面向用户的功能验收，先填它（用户座位）再跑 AC 断言（系统座位）。
内含认知走查四问 + AI 走查表 + 人工走查表，直接复制填写，不再单设模板。

## 其他

- [conventions.md](conventions.md) — 分层与依赖方向、错误处理、测试、门禁（重构期适用）
- [feishu-platform-setup.md](feishu-platform-setup.md) — 飞书自建应用 8 步配置
- [../.notes/archive/architecture-20260816.md](../.notes/archive/architecture-20260816.md) — 2026-08 架构快照（已过时，勿据此实现）

## 新增功能该写什么

1. **改的是已有功能** → 只更新 `docs/features/{域}.md` + `project-index.md` 对应行
2. **选型 / 架构取舍**（选 A 不选 B，有代价）→ 写 `docs/adr/`，一条一个文件
3. **新功能要定验收** → 写 `docs/specs/`（用户操作时序先行 + R 需求 + AC）
4. **新功能但没取舍也没验收争议** → **什么都不写**，代码 + commit message 就是记录
5. 结构图只在**拓扑真的变了**才改，改 `.archify.json` 源再 deliver 出 `.html`

> 不要往 `docs/design/` 加新文件。那一层正在退役：取舍归 ADR、流程归 features、契约以代码为事实源。
