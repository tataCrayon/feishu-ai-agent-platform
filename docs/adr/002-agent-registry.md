# ADR-002：Agent 注册表（声明式 Agent 定义 + 配置自动生成）

> ADR 编号：`ADR-002` ｜ 状态：**⛔ 被 `ADR-006` 取代**
> 创建 2026-08-13 ｜ 决策者 @devuser
> 取代后现状：`bot/agents/*/agent.yaml` 仍在，但已收敛为能力元数据
> （name / display_name / description / permission_level / enabled / mcp_servers），
> 不再承担路由工具表派生；`agents.yaml` 单文件、`dispatcher.py`、`app/edc/` 均已不存在。

## 1. 背景

bot 早期用 `app/main.py` 里 5 个硬编码 handler 处理所有消息，intent 识别写死在静态正则里。
Agent 从 1 个长到 4 个（rwa-bot / db-agent / deploy-agent / config-agent）后，
新增一个 Agent 要在 5 处手工改动，且同一份 system_prompt 要在 `config.toml` 与 Agent 目录
两处同步——已经开始漂移（`config.toml` 的 rwa-bot prompt 与 `agents.yaml` 不一致）。

## 2. 决策考虑

- **触发因素**：Agent 数量增长 + system_prompt 双处维护已实际漂移。
- **约束条件**：cc-connect 的 `[[projects]]` 只能写在 `config.toml`，绕不开；
  改动必须对既有 Agent 平滑过渡；Windows 单机。
- **非目标**：不做运行时插件热加载；不改 cc-connect 本身。

## 3. 决策选项

### 选项 A：维持硬编码 handler

- **优点**：直观，grep 即可定位；1-2 个 Agent 的阶段够用。
- **缺点与代价**：Agent ≥3 时每增一个改 5 个文件跨 4 个模块；prompt 双处维护必然漂移；
  MCP 隔离 / 权限 / model tier 无统一声明。
- **适用场景**：Agent 数量固定且 ≤2。

### 选项 B：YAML 单一数据源 + 代码生成器

- **描述**：`agents.yaml` 声明全部 Agent 属性；`registry.py` 读成 `AgentDef`；
  `generator.py` 生成 `config.toml` 段、各 Agent 的 `.claude/settings.json` 与 `CLAUDE.md`；
  `dispatcher.py` 按 intent 分派并接 trace。
- **优点**：新增 Agent = 加一条 YAML + 跑一次生成，零 Python 改动；
  路由/权限/MCP/超时全部声明式；MCP 按 Agent 隔离到独立 settings.json。
- **缺点与代价**：多一层抽象；生成器**不做反向合并**，手工编辑会与生成物再次漂移；
  生成的 CLAUDE.md 仅在文件不存在时写，后续改动不覆盖。
- **适用场景**：Agent 持续增长、需要统一治理。

### 选项 C：动态插件加载（importlib / entry points）

- **优点**：不依赖生成步骤，改完即可用；Agent 可带自定义逻辑。
- **缺点与代价**：要求 Agent 作者写 Python，门槛高于 YAML；`[[projects]]` 仍要落到
  `config.toml`，绕不开生成或手工配置；动态 import 增加启动路径复杂度。
- **适用场景**：Agent 需要运行时专属钩子时，作为 B 的补充而非替代。

## 4. 决策结论

**选定：选项 B。**

- **核心理由**：① 新增 Agent 的改动量从「5 文件 / 4 模块」收敛到「1 条 YAML + 1 次生成」；
  ② 消除 prompt 双处维护漂移的根因；③ MCP 最小权限隔离声明式落地
  （db 只挂 MySQL、deploy 只挂 CMDB、config 只挂 Nacos）。
- **放弃其他选项**：A 无法承载扩展；C 需引入动态加载复杂度且仍绕不开 config 生成。
- **影响范围**：`app/agents/`（registry / generator / dispatcher）、`agents.yaml`、
  `app/main.py` 分派入口、`config.toml` 生成段。

## 5. 后果

**正面**：新增 Agent 成本降到「一条 YAML + 一次命令」；路由、权限、MCP 隔离、模型档位统一声明。

**负面 / 需接受**：
- 排障需理解 registry / generator / dispatcher 三件套。
- 生成器与手改会漂移——手工段在下次生成时与 YAML 并存。
- 推翻成本低（删除生成物、恢复手写段）。

**必须配套的约束**：
1. `AgentRegistry.load()` 对缺 `name` 的条目告警跳过，不能让一条坏数据打挂全部。
2. 禁止绕过 registry 在 `config.toml` 手写 `[[projects]]` —— 生成器不做反向合并，
   手工段必然漂移（这条是当年最常见的坑）。
3. 每个 Agent 的 `mcp_servers` 只声明它需要的，不继承主项目的 MCP 全集。

## 6. 相关文档

- 被 `ADR-006` 取代（Agent 元数据只保留描述面，路由归 dsh 技能目录）
- 现状单源：`bot/agents/*/agent.yaml`；能力语义如何影响路由见 `docs/features/routing-dispatch.md`
