# Feishu Agent Platform — 飞书 AI Agent 平台

> 把 DeepSeek Harness（dsh）当执行层、飞书当交互层的企业级 Agent 平台。
> 飞书 WebSocket 常驻，语义路由选能力，进程内 MCP 工具取数，卡片承载结果。

[English](#english) | 中文

## 这是什么

给一个飞书群机器人装上「会自己判断该干什么」的执行内核：

```
飞书 (WebSocket)
    │
MessageAggregator ── 连发消息 debounce 合并
    │
RuntimeGuard ── 全局并发闸 + 有界排队
    │
DshGateway ── chat_id → dsh session（同会话串行 + 空闲 TTL）
    │
dsh 内核 (ACP over stdio)
    ├─ 路由：按技能目录语义匹配，首行输出能力名
    ├─ 取数：调会话内 MCP 工具，拿不到就如实说边界，不编
    └─ 多轮：会话常驻，同一 chat 粘同一 session
    │
MCP 工具面（每会话一份子进程，身份经 env 固化）
    ├─ capabilities ── 进程内 Handler 的 tools 原样投影（数字先知 / 定时任务 / 技能目录…）
    ├─ feishu ──────── 发/回消息、读文档、读当前会话上下文
    ├─ domains ─────── 业务域只读入口
    └─ agent.yaml 声明的数据源（同名只挂一次）
    │
回复飞书（进度卡 → 正文）+ trace/TaskRecord + 记忆捕获
```

三条设计原则（决定了代码里为什么是这些形状）：

1. **单源驱动**：`bot/agents/*/agent.yaml` + `bot/skills/*/SKILL.md` 是唯一事实源。
   路由候选集、`/help`、MCP 工具表全部派生，新增能力不改编排器。
2. **无全局兜底**：路由不确定就让模型如实说明边界，不塞给某个「兜底 Agent」。
   能力名必须落在技能目录里，工具失败也要写对能力名。
3. **执行面与消息面同一条链路**：定时任务到点的产出与人工提问走同一个 Gateway，
   否则同一问题会有两套答复。

## 快速开始

```bash
python -m venv .venv && . .venv/Scripts/activate      # Windows
pip install -r requirements.txt
pnpm install                                          # dsh 运行时钉在仓内 node_modules

cp .env.example .env                                  # 填飞书 / 模型凭据
python -m app.main                                    # 飞书 WS + Dashboard :9112
```

飞书侧需要：自建应用（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）、长连接权限、
`im:message` 收发与 `docx` 读权限。详细步骤见 `docs/feishu-platform-setup.md`。

## 目录（只列改代码要碰的）

| 层 | 路径 | 职责 |
|---|---|---|
| gateway | `app/gateway/` | 飞书接入、debounce 聚合、Dashboard HTTP |
| dsh | `app/dsh/` | 执行面：进程托管 / ACP over stdio / chat→session / 媒体 |
| mcp | `app/mcp/` | 会话工具面：feishu / domains / capabilities |
| agents | `app/agents/` | Agent 注册表 + Handler 约定式自动发现 |
| ops | `app/ops/` | 能力热插拔、定时任务、健康探针 |
| domains | `app/domains/` | 业务域（日报 / 技能目录 …，新能力按同样形状加子包） |
| guardrails | `app/guardrails/` | 并发闸、SQL/命令拦截、审计 |
| observability | `app/observability/` | 任务档案、trace、成本、巡检报表 |
| 单源 | `bot/agents/` `bot/prompts/` `bot/skills/` | Agent 声明 / 提示词 / 能力包 |

## 加一个能力

```bash
# 1) 声明 Agent（或纯 Skill 包）
mkdir bot/agents/my-agent && $EDITOR bot/agents/my-agent/agent.yaml
mkdir bot/skills/my-skill  && $EDITOR bot/skills/my-skill/SKILL.md

# 2) 生成派生配置 + 同步 dsh 路由候选集
python -m app.agents.generator
python deploy/dsh/sync_skills.py --add my-skill

# 3) 重启
python -m app.main
```

要进程内逻辑就加 `app/agents/handlers/my_agent_handler.py`（类属性 `agent_name`
或 `build(config, ctx)` 工厂），自动发现，零注册。

## 门禁

```bash
python -m pytest -q --basetemp=temp/pytest-tmp         # 全量 742 通过 / 2 跳过
python -m ruff check app/ --select F821,F811,F401,F541,F841
python -X utf8 -m mypy <改动文件>
python -X utf8 -m app.eval.comment_lint <改动文件>       # 注释纪律
python app/eval/prompt_eval.py                         # 提示词 golden 回归
```

## 能力清单

| Agent | 通道 | 做什么 |
|---|---|---|
| `aihot` | skill | AI 资讯 / 日报（卡片出口，文本兜底） |
| `schedule-agent` | handler | 定时/周期任务：评估通过即生效 |
| `digital-oracle-agent` | handler | 市场数据驱动的概率预测 |
| `skill-catalog-agent` | handler | 团队 Skill 目录检索与治理 |
| `dev-bot` | cc-connect | 桥接本地 Claude Code 的开发通道（`/dev` 直达） |

骨架里只留这几个**示例能力**，用来验证「声明 → 同步 → 路由 → 执行 → 交付」整条链路；
你自己的业务能力按同样形状往里加（见「加一个能力」）。

## 文档地图

- `docs/project-index.md` — 功能级索引，改功能先查这里
- `docs/features/` — 功能简报（不变量、边界、踩坑证据）
- `docs/adr/` — 架构决策记录
- `CLAUDE.md` — 给 AI 编码助手的项目约定

## English

A production-shaped Feishu (Lark) bot platform where **DeepSeek Harness (dsh) is the
execution layer** and Feishu is the interaction layer. Long-lived WebSocket connection,
semantic routing into a skill catalog, in-process MCP tool surface per session, results
delivered as interactive cards.

Highlights:

- **Single source of truth** — `bot/agents/*/agent.yaml` and `bot/skills/*/SKILL.md`
  drive routing, `/help`, and the MCP tool list; no orchestration code to edit.
- **No global fallback** — uncertain routing makes the model state its boundary
  instead of dumping the question on a catch-all agent.
- **One execution path** — scheduled jobs and manual questions share the same gateway,
  so a question never gets two different answers.
- **Guarded** — concurrency gate, SQL/command interception, audit log, per-task records.
- **Durable scheduler** — catch-up clamping and in-flight dedup, so a 7-day outage
  replays one occurrence instead of seven.

Feishu app credentials live only in `.env`. Every hosted/proprietary identifier in this
repo is a placeholder (`example.com`, `ou_admin000…`, `ou_pm000…`); substitute your own
before running.

## 许可

MIT
