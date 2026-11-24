# CLAUDE.md — feishu-agent-platform（飞书 AI Agent 平台）

> 本仓是脱敏后的公开版本：主机名、租户、文档 token、人名一律为占位符
> （`example.com` / `ou_admin000…` / `张三`）。本地跑之前先改 `.env` 与 `config.toml`。

> 单源事实：`bot/agents/*/agent.yaml`（Agent）+ `bot/prompts/`（提示词）+ `config.toml`（通道/模型）。
> 改任何一层都不要在 Python 里硬编码副本；本文件只讲结构与工作流。
> **功能级索引（改功能先查这里）**：`docs/project-index.md`。

## 一句话架构

飞书 WebSocket → MessageAggregator（debounce 合并连发）→ DshGateway（chat→session）→ dsh 内核（ACP，语义路由 + agent loop）→ 会话内 MCP 工具打回进程内 Handler → 答复回飞书。

```
飞书 (WebSocket, app/main.py)
    │
MessageAggregator ── 连发消息 debounce 合并（messaging.debounce_sec）
    │
RuntimeGuard ── 全局并发闸 + 有界排队
    │
DshGateway (app/dsh/gateway.py) ── chat_id → dsh session，同会话串行 + 空闲 TTL
    │
dsh 内核 (ACP over stdio, 仓库内 node_modules 钉版本)
    ├─ 路由：按**技能目录**（白名单同步产物）语义匹配选能力，首行输出能力名
    ├─ 取数：调会话内的 MCP 工具；拿不到就如实说边界，不编
    └─ 多轮：会话常驻，chat 粘同一个 session
    │
MCP 工具面（每个会话各拉一份子进程，身份经 env 固化）
    ├─ capabilities —— 进程内 Handler 的 tools 原样投影：数字先知 / 定时任务 /
    │   Skill 目录（**执行入口**，不挂就等于没能力）
    ├─ feishu —— 发/回消息、读文档、read_conversation（当前会话最近消息）
    ├─ domains —— 日报、Skill 目录、定时任务只读
    └─ agent.yaml 声明的数据源（同名只挂一次）
    │
Handler（app/agents/handlers/，约定式自动发现）
    digital-oracle-agent / schedule-agent / skill-catalog-agent
    │
回复飞书（进度卡 → 正文）＋ 记忆捕获 ＋ trace/TaskRecord
```

**旁路服务不进路由**：消息订阅与定时调度在 `main.py` 侧独立跑；授权群的
非 @ 消息只进监听窗口，避免把同事闲聊送去作答。定时任务的**执行面与消息链路同一条**
（`ScheduledTaskExecutor` 优先走 `DshGateway`），否则到点产出会与手动提问得到两套结果。

旧编排链路（`CrewOrchestrator` + LangGraph，`app/orchestrator/crew.py`）在
`[projects.dsh].enabled = false` 时回切启用，dsh 启用时只剩 `set_recent_file_channel`
等少量接线仍被调用——它是回切保险，不是主路径，改行为不要往这里加逻辑。

核心原则：**无全局兜底** — 路由不确定就让模型如实说明边界，不塞给某个"兜底 Agent"；
能力名必须落在技能目录里，工具执行失败也要写对能力名（写成「做不到」会让用户
以为能力不存在）。

## 目录地图（agent-platform 分层，只列改代码要碰的）

11 层平台结构（2026-10-12 重构）。**共享内核留 `app/` 根**：`main.py`（入口+飞书 WS）、
`config.py`、`dashboard.py`、`multimodal.py`、`mcpreaper.py`（MCP 孤儿清理）、
`schemas/`、`static/`、`llm/`、`prompts/`。不设 `tools/` 与 `critic/` —— 两者在本仓
没有可归属的实体（工具实现就在 `agents/handlers/`，判定已内联进单次模型循环），
建空层只会诱导后续往里塞东西。

| 层 | 路径 | 职责 |
|------|------|------|
| **gateway** | `app/gateway/channel/` `aggregator.py` `api/` | 飞书接入、debounce 聚合、Dashboard HTTP :9112 |
| **dsh** | `app/dsh/` | 执行面：`supervisor.py`（进程托管+自愈+MCP 规格）、`adapter.py`（ACP over stdio）、`gateway.py`（chat→session + 串行 + TTL）、`media.py`（MediaBundle→ACP 块）、`routing.py`（会话指令单源） |
| **mcp** | `app/mcp/` | 会话工具面：`feishu_server` / `domains_server` / `capabilities_server`（Handler tools 投影）/ `order_agent_server` |
| **orchestrator** | `app/orchestrator/` | 权限判定（`get_user_permission`）+ **回切保险**：`crew.py`（LangGraph router→agent→gather）、`router.py`、`direct.py`、`feedback.py`、`reroute_rules.py`、`history.py`。dsh 启用时不参与消息路由 |
| **api** | `app/api/` | 控制台之外、供**外部进程**调用的 HTTP 端点（与运维台 `/api/stats/*` 互不重叠；未启用时逐条返回 503，避免「改了配置没重启」表现成 404） |
| **agents** | `app/agents/` | `registry.py`（AgentDef + `bot/agents/*/agent.yaml` 发现）、`handlers/`（`{name}_handler.py` 约定式自动发现）、`skills.py`（SKILL.md → 合成 Agent） |
| **memory** | `app/memory/` | MemoryCore 网关客户端（capture/recall/session） |
| **protocols** | `app/protocols/` | `cc_connect/`（Claude Code 桥接）、`feishu_send.py` |
| **eval** | `app/eval/` | 门禁与回归：`prompt_eval` / `prompt_index` / `router_intent_eval` / `verify_spec` / `comment_lint` / `e2e_functional_test` / `user_auth_e2e` / `edc/`（EDD 指标） |
| **guardrails** | `app/guardrails/` | `runtime/`（并发闸 + 任务持久化）、`security_guard.py`（SQL/命令拦截）、`audit_logger.py`；权限白名单在 `bot/todos/whitelist.json` + `orchestrator/__init__.py` |
| **observability** | `app/observability/` | `task_record.py`（`logs/tasks/`）、`dashboard_service`、`cc_usage_service`、巡检报表（`heartbeat` / `task_report` / `token_monitor` / `cache_monitor` / `dashboard_snapshot`） |
| **ops** | `app/ops/` | `capability/`（能力热插拔）、`scheduler/`（定时任务）、`health/`（Agent 健康探针）、`model_switch.py`、`proctree.ps1` |
| **domains** | `app/domains/` | 业务域：`daily/`（资讯日报）、`skill_catalog/`（技能目录）—— 新能力按同样形状加子包 |
| 单源 | `bot/agents/*/agent.yaml`、`bot/prompts/`、`bot/skills/*/SKILL.md`、`deploy/dsh/agentbiz/skills.allowlist` | Agent / 提示词 / Skill 源 / dsh 路由候选集白名单 |
| 配置 | `config.toml` | dsh 执行面 + MCP 四个 server + cc-connect 通道 + daily/messaging/dashboard |
| 运行产物 | `logs/`、`../.dsh-bot/` | trace / audit / tasks / edc；dsh 独立 DSH_HOME（会话、profile），勿手工编辑 |

## 常用命令

```bash
python -m app.main                     # 启动主编排器（飞书 WS + Dashboard :9112）
cc-connect daemon start|status|restart # cc-connect 执行通道（可选）
python app/observability/task_report.py      # 任务档案报表（--days 30 / --negative / --json）
python app/eval/prompt_eval.py               # prompt 门禁（golden < 80% 或 INDEX 过期 exit 1）
python app/eval/prompt_index.py --check      # INDEX 与 frontmatter 一致性
python -X utf8 -m app.eval.comment_lint app/ # 注释纪律（禁变更日志式注释）
python app/eval/verify_spec.py               # 16 项 SPEC 断言
python app/observability/heartbeat.py        # 心跳巡检
python -m app.agents.generator         # 新增 agent 后生成 settings.json + CLAUDE.md
python .agent/eval/health_checker.py --auto-fix   # 记忆健康巡检+修复
```

## 四类常见改动的工作流

> Agent 开发任务的整体路由（设计→代码→门禁→发布）见 **agent-engineering-pack** 专家包
> skill；此处只列本项目最高频的改动路径。

**0. Python 代码门禁（所有代码改动收尾必走）**
任何 `app/` 下 Python 修改，完成前必须过三道闸（详见 `python-dev-guard` skill）：
```bash
python -m ruff check app/ --select F821,F811,F401,F541,F841 --fix   # 闸1 静态
python -X utf8 -m mypy <改动文件>                                          # 闸1 类型（增量；-X utf8 必需，见 mypy.ini 抬头）
python -m py_compile <改动文件> && python -c "import app.main"            # 闸2 冒烟
python -X utf8 -m app.eval.comment_lint <改动文件>                         # 闸1.5 注释纪律（0 容忍）
python -m pytest -q --basetemp=temp/pytest-tmp                            # 闸2.5 全量（Windows 默认临时目录常不可写）
# 闸3 业务验收：动手前写验收标准（真实输入样例+期望输出+验证方式），完成后贴对照表
```
**注释纪律**：注释只解释「为什么代码必须这样」，不写日期、「用户要求」、「原来是 X 现改为 Y」、
事故编号——那些属于 git log / `bot/prompts/CHANGELOG.md` / `docs/features/` 简报。
闸 1.5 机械拦截，详见 `python-dev-guard` skill。
存量基线：2026-11-06 起 `ruff check app/` 全量 0（此前 53 项已清）；**F821/F811/E4/E7/E9 必须为零**。
`RUF100` 只对「本文件用不到的 noqa」报警，与 `E402` 是同一个码的两面：给必需的行加
`# noqa: E402` 后，若该行其实不需要（导入已在顶部）会被 RUF100 反过来抓——两边都过才算绿。
工具链配置在 `ruff.toml` + `mypy.ini`。
Agent 结构改动收尾另过 agent-dev-standards 的 6 支柱 gate checklist。

**1. 加新 Agent（零 Python 改动）**
`bot/agents/my-agent/agent.yaml`（name/display_name/description/transport/permission_level）
→ 要进程内逻辑则加 `app/agents/handlers/my_agent_handler.py`（类属性 `agent_name` 或 `build(config, ctx)` 工厂）
→ `python -m app.agents.generator` → **`python deploy/dsh/sync_skills.py`**（新能力不进
`skills.allowlist` 就不在 dsh 的路由候选集里，等于模型看不见它）→ 重启。
路由候选集、`/help`、`capabilities` 的 MCP 工具全部自动派生。

**2. 加新 Skill**
`bot/skills/{name}/SKILL.md`（frontmatter: name + description 必填）→ 同步白名单
（`sync_skills.py --add {name}`）→ 重启。
dsh 侧技能目录决定路由；cc_connect 侧仍以 `[use skill: {name}]` 前缀执行。
注意 `.claude/skills/` 是启动时复制同步的生成物，源头只在 `bot/skills/`；已存活的 Claude Code 会话要重启 cc-connect daemon 才能感知。
例外：`bot/skills/digital-oracle/` 是自包含引擎包（数字先知），digital-oracle-agent
Handler 进程内调用，frontmatter enabled:false 防合成 Agent 抢路由。
外部能力可以独立仓库承载：MCP 投影走 `[projects.mcp.<name>]`，A2A 投影留一个
协议壳 Handler，判定逻辑全部在对端，本仓不重复实现。

**3. 改提示词**
- **消息路由/行为规则**改 `bot/prompts/router/dsh_routing_rules.md`（dsh 系统提示词单源，
  由 profile 的 `system-prompt.personaPrefix` 读入，`app/dsh/routing.py` 只负责读取与
  启动期校验；改完跑 `python deploy/dsh/verify_routing.py` 做真实 golden 回归）
- **旧编排回切链路**改 `bot/prompts/router/system_prompt.md`
- **Agent 人设**改 `bot/prompts/agents/{name}.md`
→ frontmatter version +1，更新 `bot/prompts/INDEX.md` 与 `bot/prompts/CHANGELOG.md`
→ **必须过门禁**：`python app/eval/prompt_eval.py`（golden 套件在 `bot/prompts/_golden/`）。
提示词是**单文件单源**（2026-10-12 起）：历史版本由 git 承载，不再存 `stable.md`/`v*.md` 副本，也没有需要手工 `cp` 同步的别名。

**4. 免重启接入（能力热插拔，管理员飞书自助）**
管理员把能力包（zip / agent.yaml / SKILL.md，≤2MB）直接发进飞书会话
→ 机器人静态扫描（凭据/危险指令/zip-slip；high 自动拒）→ 发审批卡片
→ L2 管理员点「接入」→ 落盘 + registry 重载 + graph 原子替换热生效。
实现：`app/ops/capability/`（不变量见 `docs/features/capability-hotplug.md`）。
边界：skill/cc-connect 类能力新会话生效；MCP 凭据须补 `.env`（回执会提示降级项）。

## 改动收尾（知识保鲜回路，SPEC-20260924-fresh-loop）

> 目标：下一个人（含 AI）改同一功能时，只读功能简报一页纸即可动手，不必 git 考古。
> 四步与代码**同 commit** 完成，禁止「回头再补」——补不完的债就是文档腐化的起点。

1. **索引行同步**：行为变更 → 更新 `docs/project-index.md` 对应功能域行，行尾加 `(YYYY-MM-DD)`
2. **证据锚点**：结构性决策（为什么改成这样）→ 对应功能简报 `docs/features/{域}.md` 第 6 节
   补一行：日期 + 事件 + commit hash；该域无简报且属高频改动域 → 按模板
   `docs/specs/_feature-brief-template.md` 建页
3. **commit `Docs:` 行**：message 正文末尾列本次更新的文档；无文档更新写
   `Docs: 无（纯实现内聚改动）`——强迫确认过，而不是忘记
4. **简报优先**：该域已有简报页 → 同步之（简报与代码矛盾时以代码 + commit 为准并回写简报）

红灯自检：说不清「本次改了哪些文档行」= 没改完。
放弃信号：简报页第 8 节演练连续两次返工多于直接读码改 → 停用简报驱动（spec R4）。

## 单源与凭据约定

- 8 个 agent 的 `agent.yaml` 一律 `prompt_ref: "agents/{name}.md"`，不内联 system_prompt
- frontmatter `depends_on`（shared/safety-rules、shared/output-format）在运行时由
  `PromptLoader.load()` 注入全部加载路径（agent prompt 与 router prompt 均生效）
- 凭据只放 `.env`（gitignore）；`config.toml`/`agent.yaml` 写 `${ENV_NAME}` 占位符或留空；
  `.claude/settings.json` 系生成物已退库，本地由 generator 展开生成；pre-commit 钩子拦截已知凭据入库

## 模型与配置要点

- **执行面模型**（消息链路真正用的）：`config.toml [projects.dsh]` 的 `provider` / `model`，
  默认 `flp-self-oai` / `glm-5.3-flash`；换模型要同时改 `deploy/dsh/agentbiz/cordis.patch.yml`
  的 provider 表，否则 dsh 报 `unknown model option`
- **回切链路模型**（dsh 关闭时才用）：`deepseek-flash`（`.env` 的 `LLM_MODEL`）
- 模型三要素单源在 `.env`：`LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY`（config.toml 不再承载）；
  dsh 侧的 key 走 `AIGW_API_KEY`（`DSH_` 前缀被 dsh 保留，不能用）
- 子 Agent 执行模型在各自 `agent.yaml` 的 `model:` 声明（sonnet/haiku → cc-connect 侧 Claude）
- 多轮历史：dsh 会话常驻（`session_ttl_sec` 空闲回收）；回切链路 `history_max_turns=6`
- 工具限流：10 分钟 50 次/会话（回切链路）；澄清阈值 `clarify_threshold`（0.6）同样只作用于回切链路

## 可观测性约定

- 每次编排一条 TaskRecord：最终 agent、路由链、执行方自评（results_quality）、门禁拦截、token/成本、负反馈
- Agent 回复全文落 `logs/task_replies/{task_id}.md`（ReplyStore，gitignore），供控制台「📄 报告」tab 查看
- 负反馈：短文本不满表达（"不对/答非所问"等）自动回填最近一条任务档案，不拦截消息
- trace：`logs/agents/trace.jsonl` 单行 JSON，trace_id 贯穿多跳
- Dashboard `:9112` 可看任务档案与指标；「📄 报告」tab 查看回复全文（`GET /api/stats/reports`）

## 平台注意

- Windows 环境：main.py 已强制 stdout UTF-8；start-bot.bat 先清理残留的 MCP 孤儿进程
- MCP 孤儿清理：app/main.py 启动时 reap + 120s watcher
- git 禁止提交：`.env`、`logs/`、`bot/tokens/`
