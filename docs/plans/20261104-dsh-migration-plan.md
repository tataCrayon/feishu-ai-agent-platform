# dsh 迁移实施计划

> 对应 spec：`docs/specs/20261104-dsh-migration.md`（需求与 AC）
> 对应决策：`docs/adr/006-dsh-migration.md`
> 开发规范：`docs/conventions.md`
> 状态此刻只认本文——各阶段的完成情况在文末「进度」一节，别处不重复。

## 一、先决条件（开工前必须闭合）

| # | 事项 | 谁定 | 阻塞谁 |
|---|---|---|---|
| Q1 | bot 的 `DSH_HOME` 用独立目录（建议 `D:\Users\devuser\.dsh-bot`）与桌面版隔离 | 已定：独立 | P1 |
| Q2 | 飞书 MCP server 语言：Python（复用 `lark_cli.py`） | 已定：Python | P3 |
| Q3 | `agent.yaml` 保留字段：`name/display_name/description/permission_level/enabled` | 已定 | P2 |
| Q4 | 迁移窗口：允许停机 10 分钟/阶段 | 已定 | 各阶段切换 |
| Q5 | memory 与 bot 一体（不再独立进程） | 已定 | P5 |
| Q6 | 并行冲突：`app/domains/prd_watch`、`cmdb_watch` 由另一方持续开发 | 需确认边界 | P3/P5 |

**Q6 边界**：`app/domains/{prd_watch,cmdb_watch,daily,webportal}` 属**业务域**，
本次重构只改它们的**调用入口**（handler → MCP 工具），不改域内逻辑。
域内代码由另一方继续演进，双方通过 `app/mcp/` 的薄封装解耦。

## 二、阶段划分

每阶段独立可回滚；上一阶段通过出口判据才进下一阶段。

### P0 验证（已完成）

| 项 | 结果 |
|---|---|
| ACP 全链闭环 | initialize / session/new / set_config_option / prompt / cancel 全通 |
| 工具事件透出 | `tool_call` / `tool_call_update` / `agent_message_chunk` / `usage_update` |
| 路由 golden | 70/72 = **97.2%**，3×3 一致性全稳定 |
| 技能零改写 | dsh 直接发现 `bot/skills/elk-log-query`，`skill` 工具加载正文正确 |
| MCP 挂载 | Python MCP server 挂载并调用成功；三红线可工具层硬拒 |
| 多轮连续 | 同 session 二轮准确复述首轮 |
| 安全门禁 | 9/12（3 例为判定口径问题，需重写为可测断言） |

产物：`docs/design/20261104-dsh-full-migration-spike.md` + `D:\Projects\MyProjects\dsh-acp-spike\`（探针脚本）

### P1 骨架（3-5 天）

| 交付 | 路径 | 说明 |
|---|---|---|
| ACP 客户端 | `app/dsh/adapter.py` | spawn / 握手 / session 生命周期 / 报文→内部事件；**全仓唯一触碰协议**（L2） |
| 进程托管 | `app/dsh/supervisor.py` | 健康探测、崩溃自愈、优雅关停；仿 `app/memory/lifecycle.py` 双平台分支 |
| 事件翻译 | `app/dsh/events.py` | session 事件 → TaskRecord span / 进度卡 payload / cost |
| bot profile | `deploy/dsh/agentbiz/cordis.patch.yml` | 独立 `DSH_HOME`；关默认技能根；显式 `customSkillDirs` |
| 试点接入 | `app/agents/handlers/merge_check_handler.py` 加 `transport=dsh` 分支 | 只读、低风险 |

**出口判据**：merge-check 端到端通过；`router_intent_eval.py` 不回退；dsh 进程崩溃能自愈。

### P2 路由迁移（5-7 天）

| 交付 | 说明 |
|---|---|
| 能力描述注入 | 17 个 `agent.yaml` 的 `description` → dsh 工具/技能目录 |
| 删除自研路由 | `app/orchestrator/{crew,router,direct,reroute_rules}.py` 删除 |
| 澄清迁移 | `route_to_clarify` → `ask_user_question`（同会话阻塞等待，不重新路由） |
| golden 迁移 | `router-intent.json` 72 例在 dsh 侧跑通 ≥95% |

**出口判据**：AC-2.1 ~ AC-2.4 全过。

### P3 飞书与业务能力下沉 MCP（5-7 天）

| MCP server | 工具 | 来源 |
|---|---|---|
| `mcp/feishu` | `send_message` / `read_doc` / `reply` | `gateway/channel/` + `lark_cli.py` |
| `mcp/memory` | `recall` / `capture` / `session_end` | 见 P5 |
| `mcp/domains` | webportal / daily / merge_check / schedule / skill_catalog / prd_watch / cmdb_watch / digital_oracle | `app/domains/`（薄封装，不加业务逻辑） |

**三红线**：从提示词约束改为`mcp/feishu` 工具层硬拒（AC-3.2）。

**出口判据**：AC-3.1 ~ AC-3.3；`safety-rules.json` 改写后 100%。

### P4 技能与插件（3 天）

| 交付 | 说明 |
|---|---|
| 技能目录 | `bot/skills/` → dsh `customSkillDirs`（软链或同步，零改写 SKILL.md） |
| 默认根关闭 | `includeDefaultRoots: false`（AC-5.1）——防止个人 `~/.agents/skills` 进线上 |
| 热插拔落点 | `app/ops/capability/reloader.py` 的 `SKILLS_DIR` 改指 dsh 技能目录 |
| 插件安装 | `dsh plugin --profile agentbiz add` 走 L2 审批（AC-7.1/7.2） |
| 技能开关 | 管理端启停 ↔ frontmatter `disable-model-invocation`（AC-5.2/5.3） |

**出口判据**：AC-4.x / AC-5.x / AC-6.x / AC-7.x。

### P5 memory 一体（3-4 天）

| 决策 | 说明 |
|---|---|
| 形态 | **MCP server 内嵌**，不再是独立 HTTP 服务 + 独立进程 |
| 数据 | 复用现有 MemoryCore 的存储与检索逻辑；`~/IdeaProjects/tdai-agent-memory` 作为依赖引入或复制核心模块 |
| 接口 | `recall(query, session_key, user_id)` / `capture(...)` / `session_end(...)`，与 `app/memory/client.py` 现有契约一致 |
| 移除 | `app/memory/lifecycle.py`（进程托管）、`config.toml [projects.memory.lifecycle]` |

**出口判据**：AC-9.x；时序 2 两场景通过；跨会话用户画像可用。

### P6 可观测（3 天）

| 交付 | 说明 |
|---|---|
| TaskRecord 数据源 | session JSONL → `app/observability/task_record.py` |
| 进度卡 | `app/gateway/progress_card.py`：消费 `session/update`，单卡原地更新，节流 |
| 控制台 | `/api/stats/*` 四个 tab 数据源切换，功能不回退 |

**出口判据**：AC-10.x。

### P7 退役（3 天）

删除：`app/orchestrator/`、`app/agents/handlers/`、`app/protocols/cc_connect/`、`app/memory/`、
`config.toml [projects.cc_connect]`、cc-connect daemon 引用。

同步：`docs/project-index.md` 收敛、`CLAUDE.md` 目录地图更新、mypy 豁免段删除。

**出口判据**：AC-11.x；`python -c "import app.main"` 通过；全量门禁绿。

## 三、里程碑与依赖

```
P0 ✅
 └→ P1 ─┬→ P2 ─┐
        └→ P3 ─┼→ P4 → P6 → P7
           P5 ─┘
```

P2 与 P3 可并行（P2 只动路由，P3 只加 MCP）。
P4 依赖 P1（技能目录）与 P3（热插拔落点）。
P5 依赖 P3（MCP 框架）。
P6 依赖 P1（事件流）与 P2（任务档案结构）。

## 四、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| dsh 0.2.1-rc.2 预发布，无兼容承诺 | 升级可能破坏协议 | pin 版本；升级前跑全量 golden + 契约测试 |
| 桌面版与 bot 共享 `.credentials.yaml` | 凭据互相覆盖 | 独立 `DSH_HOME`（Q1） |
| 个人全局技能污染线上 | 生产行为不可控 | `includeDefaultRoots: false`（AC-5.1） |
| 与另一方业务域并发开发冲突 | 合并冲突 | 只改调用入口，不改域内（Q6 边界） |
| 单人维护 | 断档风险 | 每阶段产物含文档，不攒到最后 |
| MemoryCore 内嵌后能力退化 | 跨会话记忆变差 | P5 出口必须验时序 2 + 用户画像场景 |

## 五、进度

> 只在此处记录，别处不重复。

| 阶段 | 状态 | 完成日期 | 证据 |
|---|---|---|---|
| P0 | ✅ 完成 | 2026-11-04 | `docs/design/20261104-dsh-full-migration-spike.md` |
| P1 | ⬜ 未开始 | — | — |
| P2 | ⬜ 未开始 | — | — |
| P3 | ⬜ 未开始 | — | — |
| P4 | ⬜ 未开始 | — | — |
| P5 | ⬜ 未开始 | — | — |
| P6 | ⬜ 未开始 | — | — |
| P7 | ⬜ 未开始 | — | — |
