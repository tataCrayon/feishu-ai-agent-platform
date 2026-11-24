# feishu-agent-platform 专用 dsh profile（`agentbiz`）

> 当前基线：`dsh 0.2.1-rc.5`（`package.json` 钉版本），本项目版本 `0.2.1-dsh.1`。

bot 的执行面跑在**一个独立的 dsh 进程**上，通过 ACP（stdio JSON-RPC）由
`app/dsh/adapter.py` 驱动。本目录是该进程的 profile 补丁层。

## 目录内容

| 文件 | 作用 |
|---|---|
| `cordis.patch.yml` | profile 补丁：LLM 网关（aigw，默认路由 `flp-self-oai` / `glm-5.3-flash`）+ 技能根与会话指令收敛 |
| `skills.allowlist` | 路由候选集白名单（技能目录 = 路由候选集，见下） |
| `skills/` | `sync_skills.py` 生成的技能目录（gitignore，不要手改） |

## 新机器从零起

```bash
# 1) 引擎：版本随仓库走，装到本仓 node_modules（约 810MB，gitignore）
npm install

# 2) 凭据：只放 .env（见 .env.example）
cp .env.example .env && $EDITOR .env      # 填 FEISHU_*、AIGW_API_KEY

# 3) 技能目录：按白名单从 bot/skills + bot/agents 生成
python deploy/dsh/sync_skills.py

# 4) 起 bot（首次会自动初始化 DSH_HOME 下的 profile）
python -m app.main
```

`DSH_HOME` 不用手工设，`DshSupervisor.start()` 按 `SupervisorConfig.dsh_home`
（缺省仓库同级 `.dsh-bot/`）注入。手工排障时见文末「怎么用」。

## dsh 引擎装在哪

引擎钉在本仓：`package.json` 精确版本 + `npm install` 产物 `node_modules/`
（gitignore）。`app/dsh/adapter.py` 的解析顺序是
**显式 `dsh_bin` > 仓库内 `node_modules/@deepseek-ai/dsh/lib/bin.js` > 全局安装**。

为什么不依赖全局安装：桌面版与 bot 共用一份全局包时，桌面版升级会顺带改掉
线上 bot 的引擎版本，而 dsh 是预发布版本、无兼容承诺（见迁移计划「风险」）。
仓库内安装让版本随仓库走——`package-lock.json` 入库，`node_modules/` 按需装。
回退全局只是为了让没跑过 `npm install` 的机器也能启动，日志会写明用的是外部安装。

## 三层隔离：怎么保证 bot 不带个人配置

bot 与操作者的 dsh 桌面版、个人 Claude Code / Codex 配置**完全不共享**，
靠三处各自独立：

| 层 | bot | 操作者 | 由谁保证 |
|---|---|---|---|
| 引擎 | `<repo>/node_modules` | 全局 `@deepseek-ai/dsh` | `package.json` + adapter 解析顺序 |
| 运行目录 | `<repo>/../.dsh-bot` | `~/.dsh` | `SupervisorConfig.dsh_home` → `DSH_HOME` |
| 共享 agents 根 | `<repo>/../.dsh-bot/agents` | `~/.agents` | `DSH_AGENTS_HOME`（supervisor 注入） |
| 技能目录 | `deploy/dsh/agentbiz/skills`（白名单产物） | `~/.agents/skills` | `includeDefaultRoots: false` |
| 会话指令 | 只有 `app/dsh/routing.py` 注入 | `~/.agents/AGENTS.md` | `agent-instructions` 插件禁用 |

后两层要一起看：`dsh-base` 默认挂 `dsh-agent-instructions`，它读
`$DSH_HOME/AGENTS.md`、`<agentsHome>/AGENTS.md`（默认即 `~/.agents/AGENTS.md`）
与会话 cwd 链上的 `AGENTS.md`/`CLAUDE.md`。对 bot 来说三处都是别人的东西——
尤其 `CLAUDE.md` 是本仓给改代码的人看的开发约定，不是线上机器人的人设。
所以整插件禁用，没有「清空候选名」这种半关状态（候选为空时它仍会读两处全局文件）。

## 为什么要有独立 DSH_HOME

dsh 的 profile、会话、凭据都从 `$DSH_HOME`（缺省 `~/.dsh`）派生。桌面版和 bot
共用同一个 home 会互相覆盖 `.credentials.yaml`，会话文件也会混在一起。

```
D:\Users\devuser\IdeaProjects\
├── feishu-agent-platform-dsh\        ← bot 仓库
└── .dsh-bot\              ← bot 的 DSH_HOME（gitignore，不手工编辑）
    ├── agents\            ← DSH_AGENTS_HOME：bot 自己的共享根（空目录也正常）
    ├── sessions\          ← 会话记录（按 cwd 分目录）
    └── profiles\
        └── agentbiz\     ← 首次启动时自动初始化
```

`supervisor` 启动时设 `DSH_HOME`，并且**不**把本目录的 patch 复制进去，
而是用 CLI 层叠加。这样仓库里的 profile 是唯一事实源，改配置只改这里。
profile 目录缺失时 dsh 自动按模板初始化
（`@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-acp-app`）。

## 为什么要关默认技能根

dsh 的 `dsh-skill-filesystem` 默认扫五个根（rank 从低到高）：

| 来源 | 路径 |
|---|---|
| `project-dsh` | `<projectRoot>/.dsh/skills` |
| `project-agents` | `<projectRoot>/.agents/skills` |
| `custom` | `customSkillDirs` |
| `user-dsh` | `$DSH_HOME/skills` |
| `user-agents` | `~/.agents/skills` |

后两个是**操作者个人的**全局技能。线上 bot 如果加载它们，个人实验性 skill 会直接
影响机器人行为，且不可审计（实测 dsh 会引用 `~/.agents/skills/*/SKILL.md` 的正文行号）。

因此本 profile 设 `includeDefaultRoots: false`，只保留 `customSkillDirs`。
它指向的是**白名单同步产物** `skills/`，不是 `bot/skills` 源目录：
技能目录**就是路由候选集**，工具型技能放进来会与业务 agent 抢路由
（实测个人 `code-concept-tracer` 抢走 db-agent 的请求）。

`customSkillDirs` 里的相对路径在 dsh 侧按**进程 cwd** 解析（`path.resolve`），
而 cwd 取决于谁拉起进程，不可依赖——所以必须是绝对路径，从 `DSH_BOT_SKILLS`
取，`supervisor` 启动时按仓库实际位置注入，换机器不用改本文件。

白名单变更流程：改 `skills.allowlist` → `python deploy/dsh/sync_skills.py`
（`--check` 只校验；`--add NAME` 追加后同步）。新增 Agent 后要同时过
`python -m app.agents.generator`。

## 怎么用（手工排障）

通常不需要手工起进程，`DshSupervisor.start()` 会做。排查时：

```bash
# 1) 校验补丁能正确合并（不启进程，只打印合成后的配置树）
DSH_HOME=$PWD/../.dsh-bot \
DSH_BOT_SKILLS=$PWD/bot/skills \
DSH_AGENTS_HOME=$PWD/../.dsh-bot/agents \
node node_modules/@deepseek-ai/dsh/lib/bin.js \
  --profile agentbiz --patch deploy/dsh/agentbiz/cordis.patch.yml --dump-config

# 2) 直接按 ACP 起（stdin/stdout 是协议通道，日志在 stderr）
#    同上环境变量，去掉 --dump-config
```

检查三件事：

- `agent-instructions` 段是 `disabled: true`
- `skill-filesystem` 段有 `includeDefaultRoots: false`，`customSkillDirs`
  指向 `deploy/dsh/agentbiz/skills`（不是 `~/.agents/skills`）
- `llm-pi-ai` 的每条 provider 都是 `apiKeyEnv: AIGW_API_KEY`

## 凭据

模型网关 key 由 profile 里的 `apiKeyEnv: AIGW_API_KEY` 从**进程环境变量**读，
不从本文件读。`supervisor` 把 `config.toml [projects.dsh].api_key` 或 `.env`
的 `AIGW_API_KEY` 注入成同名变量。缺 key 时 dsh 能起来，第一次 prompt 才报
`no credential`。

实测结论（dsh 0.2.1-rc.2）：

| 方式 | 结果 |
|---|---|
| `$DSH_HOME/.credentials.yaml` 的 `refs:` 段 | 生效（任意 provider 都认） |
| 子进程 `env` 给 `AIGW_API_KEY`，路由是 `openai-completions` | **生效** |
| 子进程 `env` 给 key，路由是 `anthropic-messages` | **不生效**（该路由只读 `.credentials.yaml`） |

所以默认路由选 `flp-self-oai`（openai-completions），不必在 `DSH_HOME` 里放
凭据文件。升级 dsh 后要重测这条。

环境变量名不能带 `DSH_` 前缀（`DSH_API_KEY` 之类）：dsh 把该前缀视为启动环境
专属，`.env` 里出现会直接拒绝启动。所以用 `AIGW_API_KEY`。

## MCP 工具面

会话里的工具来自 `session/new` 的 `mcpServers`，由
`DshSupervisor.mcp_server_specs()` 汇总两处：

| 来源 | 内容 |
|---|---|
| `config.toml [projects.mcp]` | 本仓自己实现的 `feishu` / `domains` / `capabilities` / `order_agent` |
| 各 `bot/agents/*/agent.yaml` 的 `mcp_servers` | 数据源：MySQL、CMDB、Nacos（同名只挂一次） |

`capabilities` 是**能力的执行入口**，不是可选项：技能目录只放「名字 + 描述」进路由
候选集，真正动手要靠工具。它把各 Handler 的 `tools` 原样投影成 MCP 工具
（数字先知 `gather_signals`、CMDB 订阅/关注、定时任务读写、工单、Skill 目录）。
不挂它，模型会如实回答「本会话没有这个执行入口」——能力清单看着有、实际做不了。
细节见 `docs/features/capabilities-mcp.md`。

**调用方身份写进 MCP env**：`open_session(chat_id, caller)` 把
`PROMATE_CALLER_CHAT_ID / _SENDER_ID / _SENDER_NAME` 加进每个 server 的 env。
dsh 给**每个会话**拉起独立的一份 MCP 进程（实测：同一 spec 两个 session 两个 pid），
所以「谁在说话」在进程启动时就固定了。订阅/关注类工具只认它，不认模型口述的身份。

四个构造细节都是踩过的坑，改这里之前先读：

- **env 是 `name`/`value` 条目数组，不是字典**。传 dict 会被当空列表，子进程拿不到凭据
- **`${VAR}` 在这里展开**。dsh 不替 MCP 子进程做占位符替换，留着就把字面量当密码发出去
- **`npx` / `npm` 要解析成 `node <cli.js>`**。Windows 上它们只有 `.cmd` 包装器，
  MCP 客户端不经过 shell，直接 exec 会失败
- **按会话的 env 覆盖配置里的同名项**。身份只能有一个来源，配置不该能改它

**某个 MCP 起不来时把它 `enabled = false`，别挂着**：dsh 对 MCP 启动失败的处理是
`session/new` 直接拒绝，表现出来是「bot 完全不回话」，而不是「这项能力不可用」。

正在修的：`order_agent` 已置 false——对方仓 `.venv` 的 mcp 升到 2.x，而其
`order_agent/mcp_server.py` 仍 import 1.x 的 `mcp.server.fastmcp`，一启动就
`ModuleNotFoundError`。修复在对方仓（`requirements.txt` pin `mcp<2`，或迁到
`MCPServer`），本仓只负责指过去。

## 与 `~/.dsh/profiles/acp` 的关系

`cordis.patch.yml` 的引擎段（`llm-pi-ai` 的 providers 列表、`agent-default-model`）
是从桌面版 `acp` profile 移植的同一份网关配置。本 profile 多三段：

- `skill-filesystem`（acp 用默认根，本 profile 关掉）
- `agent-instructions` 禁用（acp 保留，本 profile 不读任何外部指令文件）
- `acp` 行的 `provider/model` 覆盖（acp-app 自带的默认路由是 `deepseek-official`，
  本 profile 的网关里没有这个 provider，不覆盖则每个会话第一次 prompt 就报 no API key）

新模型要加到网关时，两处都得改；长期应由 dsh 的网关目录统一发现，
迁移期先手工同步。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `profile "agentbiz" does not exist` | `DSH_HOME` 指错目录（adapter 只在 `dsh_home` 下自动建 profile） | 确认 `DSH_HOME` 下是 `profiles/agentbiz` |
| `cannot resolve profile bundle ...` | profile 的 `package.json` 缺 bundles | 删掉 `profiles/agentbiz` 让 adapter 重新初始化 |
| 技能列表里出现个人 skill | 补丁没生效（`--patch` 路径错） | 跑 `--dump-config` 看 `includeDefaultRoots` |
| 会话自称「编码智能体」/ 知道本仓 `CLAUDE.md` | `agent-instructions` 没被禁用 | 检查 patch 该段与 `DSH_AGENTS_HOME` 是否注入 |
| `session/new` 返回 `Internal error` | 有 MCP 子进程起不来；也可能 cwd 在 `deploy/` 下（那里没有 `.git`/`.dsh`） | 先逐个挂载定位是哪个 server，再 `enabled=false`；cwd 用仓库根 |
| 第一次 prompt 报 `no credential for provider route` | 子进程环境里没有 `AIGW_API_KEY` | 见「凭据」 |
| 回复里夹杂思维链 / 首行不是能力名 | 路由规则没注入或模型没遵守 | 跑 `python app/eval/router_intent_eval.py --case-tags ...` 复现 |
