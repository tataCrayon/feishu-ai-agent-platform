# 项目功能索引 — 快速定位 + 快速修改指南

> 目标：改任何一个功能时，从这张表直接定位「要碰的文件 + 不用碰的文件」。
> 架构总览见根目录 `CLAUDE.md`；本文按功能域展开。
> 本索引 2026-09-11 按六支柱审查逐条核对过文件真实性（文件存在 + 消费者存在）。
> **改功能前先读对应功能简报**（`docs/features/`，只凭流程+期望即可动手，不必预读实现；
> 制度见 SPEC-20260924-fresh-loop）；简报与代码矛盾时以代码 + commit 为准并回写简报。

## 索引使用方法

1. 先在下表找到功能域 → 得到「单源文件」（唯一要改的地方）
2. 对照「改动生效方式」执行（多数情况 = 重启 `python -m app.main`）
3. Python 代码改动 → 过 `python-dev-guard` skill 三道闸（ruff/mypy/冒烟 + 业务验收对照表）
4. 涉及提示词的改动，**必须**过门禁 `python app/eval/prompt_eval.py`
5. 凭据只放 `.env`（已 gitignore）；yaml/config.toml 里写 `${ENV_NAME}` 占位符，
   `python -m app.agents.generator` 展开；pre-commit 钩子拦截已知凭据再次入库
6. **改动收尾（保鲜回路，与代码同 commit）**：行为变更 → 本表对应行加 `(YYYY-MM-DD)`；
   结构性决策 → `docs/features/` 简报页第 6 节补证据锚点行；commit message 末尾写
   `Docs:` 行指回更新的文档（无则写「无」）——详见 CLAUDE.md「改动收尾」段

## 功能域索引

| 功能 | 单源文件（改这里） | 派生物（勿手改） | 生效方式 |
|------|-------------------|------------------|----------|
| 消息接入/回执 | `app/main.py`（on_message/_process_batch）；**过程卡 = 思考 + 能力调用按到达顺序缝成一条时间线，答案写在同一张卡里（只发一条消息）**：`app/dsh/timeline.py` 管合并（相邻 thought 帧续写一块、遇工具事件断开、同名动作原地更新、超预算按整块丢头），`app/dsh/progress_card.py` 管渲染（头部写答案首句、折叠面板放过程、GFM 表格转原生 table、反馈按钮内嵌、超 8000 字答复退回文本；**运行态只留最近 3 块/每块 200 字，完成态动作 <2 步不渲染面板**）；`app/dsh/narrative.py` 把 `ToolCallEvent.title`（`mcp__capabilities__gather_signals` 这类内部名）翻成「获取市场信号」，失败再补一句可行动原因；**卡片首发串行**（`_push_card` 的 `_card_lock`，否则并发帧各发一张，多出永不收尾的孤儿卡）（2026-11-11）；**耗时行实时刷新 + 预估**：`_heartbeat` 每 `_PROGRESS_TICK_SEC`=5s 无条件重画（只改计时、走节流路径），`progress_card._eta_text` 按步骤档位（15/60/180/300s）给「预估约 Ns（还剩约 Xs）」，超出档位写「已超，仍在处理」，只升不降（2026-11-23）；**回调统一回到本仓事件循环**：`lark_channel` 在自建后台线程+独立循环上 await 我们注册的 handler，而 dsh 进度帧从主循环推来，两边抢同一把 `_card_lock` 会抛 `is bound to a different event loop`，异常从完成卡那次刷新冒出去、把已拿到的答案整条丢掉。`BotChannel` 注册时锁定本仓循环、回调经 `run_coroutine_threadsafe`+`wrap_future` 投回（`RuntimeGuard._sem`/`DshGateway._map_lock` 同类隐患一并归位），`_push_card` 的锁获取也挪进 try——刷新失败只降级过程卡（2026-11-24）。旧编排链路的 `_progress_line`/`_upsert_tool_line` 仍在（回切链路无 thought 帧，经 `_legacy_timeline` 转成动作块） | — | 重启 |
| dsh 执行面（消息链路走 dsh） | `app/dsh/`（`supervisor` 进程托管+自愈、`adapter` ACP over stdio、`gateway` chat→session 映射+串行+TTL 回收、`media` MediaBundle→ACP 块、`routing` 注入路由规则）+ `deploy/dsh/agentbiz/`（`cordis.patch.yml` 网关/技能根/指令插件收敛、`skills.allowlist` 路由候选集、`sync_skills.py` 同步、`upgrade.py` 内核升级+配置断言、`verify_routing.py` 真实 golden 回归）+ `package.json`（引擎钉版本，装在仓库 `node_modules`）。**三层隔离**：独立 DSH_HOME（`../.dsh-bot`）、独立 DSH_AGENTS_HOME（不读 `~/.agents`）、`agent-instructions` 整插件禁用（不读任何个人 AGENTS.md/CLAUDE.md）。升级：`python deploy/dsh/upgrade.py --check / --to <ver> / --verify`（2026-11-05，rc.2→rc.5 实测） | `deploy/dsh/agentbiz/skills/`（白名单同步产物，gitignore） | 重启；内核升级后跑 `--verify` + `verify_routing.py` |
| MCP 工具面（会话执行入口） | `config.toml [projects.mcp]` + `app/mcp/`：`feishu_server`（发/回消息、读文档、**读会话最近消息 read_conversation / 成员名**、**代发通知卡 `send_card_message`**——结构由 `app/protocols/notify_card.py` 钉死，模型只填纯文本；副标题署名取 `PROMATE_CALLER_SENDER_NAME` 而非模型填的参数；`<at>` 一律剥除、超长一律拒绝，2026-11-11）、`domains_server`（**日报：`daily_report` 全文 markdown + `daily_report_card` 直接发卡到本会话**、Skill 目录、定时任务只读）、**`capabilities_server`（把进程内 Handler 的 `tools` 投影成 MCP 工具：数字先知、定时任务、Skill 目录——schema 原样透传）**。身份：会话建立时把 sender/chat 写进 MCP 子进程 env（`PROMATE_CALLER_*`），订阅类工具只认它，不认模型口述（2026-11-05，简报 `docs/features/capabilities-mcp.md`） | — | 重启；新会话生效 |
| 多模态（图片/文档/音视频） | `app/multimodal.py`（下载+分流解析：xlsx/xlsm `openpyxl`、**xls（BIFF）`xlrd`**、docx `python-docx`、**pptx `python-pptx`**、**odt/ods/odp `odfpy`**、pdf `pypdf` 文本层、html `bs4`、**doc/rtf antiword+RTF 剥壳**、代码/文本按 utf-8/gbk/utf-16+charset 探测；单份 3 万字、单条消息 6 万字上限并**明说截断**）+ `app/dsh/media.py`（→ ACP 图片块，bmp 转 png，视频/超限降级说明）。附件后缀白名单在 `app/gateway/channel/`（与 `_DOC_EXTS` 同口径，2026-11-05 扩容；简报 `docs/features/session-context.md`） | `requirements.txt` 文档解析段 | 重启 |
| 目录分层（agent-platform 11 层） | `app/` 下 10 个子包 + 共享内核留根：**gateway**（channel/aggregator/api）、**orchestrator**（控制流单点）、**agents**、**memory**、**protocols**（cc_connect/feishu_send）、**eval**（5 个门禁脚本 + edc）、**guardrails**（runtime/security_guard/audit_logger）、**observability**（task_record + dashboard/cc_usage 服务 + 5 个巡检报表）、**ops**（capability/scheduler/health/model_switch）、**domains**（daily/skill_catalog）。不设 `tools/` 与 `critic/`（无可归属实体）。共享内核 `main.py`/`config.py`/`dashboard.py`/`multimodal.py`/`mcpreaper.py`/`schemas/`/`static/`/`llm/`/`prompts/` 留 `app/` 根（2026-10-12） | — | 重启 |
| 连发消息合并 | `app/gateway/aggregator.py` + `config.toml [projects.messaging]`（批次附件合并挂末条消息 c84f47a；附件带来源 message_id 供跨消息下载 7526194，2026-09-18；回复引用的父消息正文在进聚合前拼进文本，2026-09-29；引用正文异步拉回后挂 `_prefetched_text` 供聚合读取 + `raw.parent_id` 兜底（修复串单，2026-09-30）；不变量见 `docs/features/aggregator-attachments.md`） | — | 重启 |
| 意图路由 | `bot/prompts/router/system_prompt.md`（**单文件真源**，v1.14.0：规则 0 意图预判复述 + 能力盘点双出口——第二人称自我介绍→help、团队资产盘点→skill-catalog 本地快照毫秒级）+ `bot/agents/*/agent.yaml` 的 `description`。无副本无归档，历史版本由 git 承载（2026-10-12 单文件化） | 路由工具表（registry 派生，仅含 `routable: true` 且未被运行时开关禁用的 Agent） | 重启 + prompt_eval |
| 澄清闭环（pending 确认） | `app/orchestrator/history.py::record_pending/pop_pending`（内存 + 5 分钟 TTL，取即删）+ `app/orchestrator/crew.py::_clarify_node`（反问时记录）/`execute`（路由前取出拼入 memory_context，用户回「查库那个」可结合候选定向，2026-10-02） | — | 重启 |
| ReAct 多意图编排 | `app/orchestrator/router.py`（`_parse_tool_args`/`extra_calls` 多 tool_calls 解析）+ `app/orchestrator/crew.py`（router 节点 `parallel_tasks` 展开 → `_decompose_node` 并发分派与整合；exclusive_domain 主路由时抑制并行 extra_calls）+ `bot/prompts/router/system_prompt.md` | — | 重启 + prompt_eval |
| 回执策略（去向尾注） | `app/orchestrator/crew.py::_gather_node`（尾注 `_🤖 由 XX 处理_`，替代 routed_receipt 中间消息）+ `config.toml [projects.messaging].routed_receipt`（已默认 false） | — | 重启 |
| 能力开关（控制台动态上下线） | `app/agents/switches.py`（load/save `bot/todos/agent_switches.json`，false=强制下线）+ `app/agents/registry.py::apply_switches` + `app/gateway/api/dashboard.py`（`GET /api/agents` + `POST /api/agents/switch`，main.py 启动时 `configure_agent_control` 接线）+ 控制台「服务健康」页能力开关面板 | `bot/todos/agent_switches.json`（开关态，已入库） | 热生效（写文件→apply_switches→rebuild，无重启） |
| /dev 远程开发通道（管理员） | `app/main.py::_handle_dev_channel`（`/dev <需求>` 前缀拦截：仅 L2 管理员，直达 cc-connect dev-bot，不经路由 LLM）+ `bot/agents/dev-bot/agent.yaml`（`routable: false`——团队消息永远路由不到 dev-bot，2026-09-14）；**2026-11-23 更名**：agent 名与显示名统一为「开发助手」，同步 `~/.cc-connect/cc-exec.toml` 项目名与 token_monitor 的 session 文件名口径 | — | 重启 |
| /help 能力清单 | `app/orchestrator/crew.py::_help_node`（registry 派生：智能助手/专业工具/扩展技能 分组 + **热插拔技能单独分组带 📦 标识**（`CapabilityStore.active_capabilities()`，bot/inbox 档案）+ 清单尾「团队技能库另有 N 项 Skill」引导行（读 `logs/skill_catalog/snapshot.json`，失败静默省略；能力盘点出口收敛 2026-10-02）） | Agent 清单（registry 派生） | 重启 |
| 新增/停用 Agent | `bot/agents/{name}/agent.yaml` | `.claude/settings.json`（generator 产物，已退库） | 重启（必要时 `python -m app.agents.generator`） |
| 新增 Skill | `bot/skills/{name}/SKILL.md` | `.claude/skills/`（启动时同步） | 重启 + 重启 cc-connect daemon |
| 进程内 Handler | `app/agents/handlers/{name}_handler.py`（`agent_name` 属性 = 注册键） | 自动发现（discovery.py，约定见文件头） | 重启 |
| 分派超时（两级时限） | `app/orchestrator/crew.py::_inproc_with_deadline` + `::_agent_hard_timeout`（进程内通道 >90s 转后台直投原会话、>agent.yaml `timeout_sec` 才取消并回失败回执；decompose 子步同口径）+ 常量 `_INPROC_SYNC_WAIT_SEC`。**`timeout_sec` 此前只解析不消费＝没配**，长任务占满并发闸额度致后续消息「系统繁忙」（＝用户报的任务经常中断）。声明值按 logs/tasks 真实成功耗时上调（2026-11-02） | `bot/agents/*/agent.yaml` 的 `timeout_sec`（单源） | 重启 |
| 数字先知（概率预测） | `bot/skills/digital-oracle/`（引擎自包含，vendoring komako-workshop/digital-oracle MIT，2026-09-24）+ `app/agents/handlers/digital_oracle_handler.py`（编排壳：gather_signals 并行取数，渲染按 spec.type）+ `bot/agents/digital-oracle-agent/agent.yaml`（transport=handler，不走 cc-connect）+ `bot/prompts/agents/digital-oracle-agent.md`（方法论+信号菜单+报告模板） | `bot/skills/digital-oracle/tests/`（引擎单测 246 例） | 重启 |
| 自答回复 | `bot/prompts/router/direct_answer_system_prompt.md`（v1.5.0）+ `app/orchestrator/direct.py` + `crew.py::_direct_answer_node`（知识库接地已移除 2026-10-08——查询移交飞书豆包） | — | 重启 + prompt_eval |
| 回复篇幅分级 | `bot/prompts/shared/output-format.md`（**单源**，v1.1.0：短档 ≤300 字=事实查询/回执，长档 800–1200 字=分析/方案/排查/报告，允许分节标题）——各 agent prompt 不再各自硬写数字，只引用本表；全文回复另落 `logs/task_replies/` 供控制台「报告」tab（2026-11-02） | `bot/prompts/INDEX.md` 生成区 | 重启 + prompt_eval |
| 执行方自评（self-check）+ 结构化门禁 | `bot/prompts/shared/self-check.md`（v1.0.0，10 个 agents/*.md 经 depends_on 注入） + `crew.py::_gather_node`（剥离末尾 `<<<QC ...>>>` 标记 → TaskRecord 的 results_quality/retry_with/gate_blocked，**只观测不裁决、不重试**） + `crew.py::_extract_self_check`（只认最后一行，无标记原样返回）。取代 2026-10-12 移除的独立 judge/recover 节点 | — | 重启 + prompt_eval |
| 复杂任务分解 | `bot/prompts/router/decompose_system_prompt.md`（单文件真源）+ `app/orchestrator/crew.py::_decompose_node` | — | 重启 + prompt_eval |
| 日报 | `app/domains/daily/`（`aihot.py` AI HOT 匿名只读 v1 取数与推送文本 + `card.py` **卡片渲染**：要点头写日期、按 API 板块分区、每条标题带 `links.aihot` 蓝链、超量写「另有 N 条」）+ **MCP 出口** `daily_report_card`（`app/mcp/domains_server.py`，发到本会话，发送失败回执带 `push_text` 兜底；`daily_report` 仍给 markdown 全文）+ 技能包 `bot/skills/aihot/`（2026-11-20 起日报交付固定走卡片） | — | 重启 |
| 定时任务 | `app/ops/scheduler/`（scheduler.py 总装 + store 档案 + evaluator 评估 + cards 回执卡片 + loop 调度 + executor 执行 + runs 执行历史）+ `app/agents/handlers/schedule_handler.py`（创建入口 + 生命周期工具 + `_resolve_target` 口语化目标解析）+ `bot/prompts/agents/schedule-agent.md` + `config.toml [projects.scheduled_tasks]`。**无审批门**：评估通过即落 `approved/` 并纳入调度，卡片只是回执（`cards.py` 无按钮、无 card_action handler）；残留 `pending/` 由 loop 收编（2026-11-08）。**执行面与消息链路同一条**：`executor._run_instruction` 先走 `dsh_gateway`（chat=target_id），未启用才回落旧编排器——到点产出必须和用户手动发同一句话一致（2026-11-06）；**2026-11-24 三处执行可靠性修补**：① 投递判 `SendResult.success`——飞书拒收（机器人被移出群）走的是 `success=False` 不抛异常，此前只判「没抛」会把没送到的任务记成成功，运行历史全绿而群里一个字都没有；② `compute_next_run` 加「落后就改 now+interval」钳制——按 `上次+interval` 累加时，停机 2 小时后一条 30 分钟任务会每轮扫描补一次、连刷到追平，现在对齐原生 Schedule「只补最近一次」的语义（未落后时仍保持原节拍，不漂移）；③ 调度器加 `_in_flight` 去重——执行时长可能超过扫描周期（实测 25 分钟 > 10 分钟间隔），没有这层会对同一任务并发起两次执行、抢同一个 dsh 会话。另补 `on_agents_reloaded` 接线（此前全仓无调用点，热插拔新增能力后建任务仍会被「执行方不在能力清单内」驳回） | `logs/scheduled_tasks/`（运行档案：approved/rejected/paused/done/cancelled，已 gitignore）+ `logs/scheduler_runs/`（逐次执行历史） | 重启 |
| 分支合并检查 | `app/agents/handlers/merge_check_handler.py`（check_branch_merge 工具：参数校验+HTTP+渲染，判定逻辑全在服务端勿重复实现）+ `bot/agents/merge-check-agent/agent.yaml` + `bot/prompts/agents/merge-check-agent.md` + `config.toml [projects.merge_check]`（base_url/enabled/timeout_sec）；底层=外部 merge-check 服务 `GET http://127.0.0.1:8080/api/merge-check`（GitLab 分支对比封装，project_ids 可空、branch_type=all/release/bugfix，全量约 30s；项目清单在服务端 config/settings/app.yaml 维护）。**内网直连 trust_env=False**；该服务仅在内网可达，联调须换到对应网络环境 | `bot/prompts/_golden/merge-check-agent.json`（5 例）+ router-intent +2 例 | 重启 + prompt_eval |
| 团队 Skill 目录 | `app/domains/skill_catalog/`（scan 按分支读 SKILL.md 治理字段与活动证据，store 一天一刷，query 查重/审查/状态渲染，status.py 状态机）+ `app/agents/handlers/skill_catalog_handler.py` + `bot/agents/skill-catalog-agent/agent.yaml` + `bot/prompts/agents/skill-catalog-agent.md` + `config.toml [projects.skill_catalog]`（repo_url/skip_branches/refresh_interval_hours）。只查询，不安装、不写回、不拦截立项。**规范状态层（2026-09-30）**：八项治理字段（owner/明确不做/自治上限/工具/成功标准/停止后找谁/复审日/status）→ 草稿/生产/未验证/下线候选/已下线，状态在渲染时按当天推导（复审过期自动降级，快照只存事实）；`get_skill_status_board` 总表 + `get_skill_status` 单技能卡；活动证据 `git log --all` 近 30 天提交（master 可能停更，2026-09-30 实测停在 5 月）+ 分支在飞。状态机与八项字段键名见 `docs/features/skill-catalog.md` §4.1 | `logs/skill_catalog/`（bare clone + snapshot.json，已随 logs/ gitignore）+ `bot/prompts/_golden/skill-catalog-agent.json`（9 例） | 重启 |
| 失败回退文案 | `app/prompts/loader.py` 的 `_DEFAULT_HELP_REPLY` / `_DEFAULT_ROUTER_PROMPT`（仅当对应 prompt 文件加载失败时启用；不得列举已下线命令，不得把 dev-bot 当兜底，2026-09-29）。路由运行时真源是 `bot/prompts/router/system_prompt.md`（单文件，无副本） | — | 重启 |
| 权限矩阵 | `bot/todos/whitelist.json` + `bot/prompts/shared/safety-rules.md`（depends_on 注入全部 agent prompt） | — | 即时/重启 |
| 能力热插拔 | `app/ops/capability/`（ingest 扫描建档 → approval_card 审批卡片 → reloader 热重建）+ `app/orchestrator/crew.py::rebuild` + `config.toml [projects.capability]` | `bot/inbox/`（档案：pending/approved/rejected/failed/archive，已 gitignore） | 热生效（graph 原子替换；skill/cc-connect 类新会话生效） |
| Dashboard（控制台） | `app/dashboard.py`（FastAPI 静态页）+ `app/gateway/api/dashboard.py`（stats/behavior/runtime/agents 四组接口）+ `app/observability/dashboard_service.py`（logs/ 文件聚合；**运行趋势数据源=logs/tasks 任务档案**——logs/agents 的 trace 不携带 token/cost（写入侧未填），2026-09-15 修复）+ `app/static/index.html`（archify 视觉语言，用户视角 BDD 组织；125% 根字号缩放，「路由视图」纵向树（飞书消息→智能路由→终态扇形+agent 明细，纯 CSS 连线无 SVG），运行趋势按指标切换单位：次/个/$/ms/%，缓存命中率无真实数据源已下架）+ `config.toml [projects.dashboard]`（host=0.0.0.0 对局域网开放）+ **架构视图 tab**（`app/dashboard.py` 白名单路由 `GET /arch/{name}.html` 内嵌 archify 图：runtime/lifecycle 两张，源 JSON 在 `docs/diagrams/{feishu-agent-platform-runtime,feishu-agent-platform-request-lifecycle}.archify.json`，改图走 archify skill validate+deliver 后刷新即生效，iframe 懒加载） | — | 重启（静态页刷新即可，支持 #tabXxx 直达）；**UI 改动验收**：`python app/observability/dashboard_snapshot.py`（headless Edge 一键多视口截图 → logs/dash-shots/，坑已固化：scale=1、virtual-time-budget、目录自动创建） |
| 可观测性/任务档案 | `app/observability/task_record.py`（TaskRecord/TaskRecorder/ReplyStore） | `logs/tasks/`（运行产物）+ `logs/task_replies/`（Agent 回复全文 .md，gitignore；crew.py 每次编排落盘） | 重启 |
| 报告查看（控制台「📄 报告」tab） | `app/gateway/api/dashboard.py::GET /api/stats/reports`（列表，days/agent 过滤）+ `GET /api/stats/reports/{task_id}`（全文）+ `app/observability/dashboard_service.py::{get_reports,get_report_detail}`（扫 logs/tasks 7 天档案、过滤有全文回复的任务）+ `app/static/index.html` tabReports（列表+筛选+点击弹层，marked.js 渲染 markdown） | `logs/task_replies/{task_id}.md`（数据源） | 重启（静态页刷新即可） |
| 费用统计 | `app/llm/client.py`（MODEL_PRICES 三档价按 aigw 官方页 example.com/pricing 校准 + estimate_cost 缓存折扣：非缓存 input×input价 + cache_read×cache价 + output×output价，返回 8 位小数）+ `app/orchestrator/{router,direct}.py`（estimate_cost 传 cache_read）+ `app/observability/__init__.py::AgentTrace.with_usage`（同口径）+ `app/observability/cc_usage_service.py`（**Claude Code 通道费用**：扫 `~/.claude/projects/*feishu-agent-platform*/*.jsonl` 转录按 message.id 去重聚合，模型名归一 glm-5-3-flash-260828→glm-5.3-flash，文件级增量缓存 logs/cc_usage/cache.json）+ `app/gateway/api/dashboard.py::GET /api/stats/cc-usage` + 控制台「服务健康」CC 通道费用面板 | `logs/cc_usage/cache.json`（缓存，运行产物） | 重启；**口径盲区**：任务档案 cost 只含路由+直答 span（agent span 无 cost_usd），CC 通道费用须看新面板，对账以 aigw 账单为准 |
| 心跳巡检 | `app/observability/heartbeat.py` | — | 即时运行 |
| 健康探测 | `app/ops/health/__init__.py`（探测表 _PROBES：name 专属探测 + transport 通用探测；handler 通道=注册表命中+tools 非空即 green，2026-09-16 补位消除控制台 unknown；daily 探测 = 数据源可达 + 本地缓存存量）；main.py 须把 handler_registry 传给 AgentHealthMonitor/quick_health_check | `logs/health/current.json` | 重启 |
| cc-connect 通道 | `app/protocols/cc_connect/client.py`（投递注入 [task:tag] 精准关联回复 + 90s 超时转后台守望 20 分钟，迟到结果回调直达原会话不再静默丢弃 b81e8a0，2026-09-18；不变量见 `docs/features/cc-connect-delivery.md`）+ `config.toml`（[projects.cc_connect] 含 `max_sessions`=Claude Code 会话并发上限，send_and_wait 占额度超限排队；会话复用由 cc-connect 侧 session_key resume 保证；2026-11-23 project 名 rwa-bot→dev-bot，历史 session 文件按新名复制）+ `~/.cc-connect/cc-exec.toml`（执行通道专用配置：feishu 平台为占位 app_id 连不上飞书——cc-connect v1.4.1 强制要求每 project 至少一个 platform，disabled_platforms 实测不生效；**飞书消息只能 feishu-agent-platform 独占消费**，同 app 多长连接是竞争消费，2026-09-14 曾因双连接抢消息致 /help 透出 cc-connect 内置帮助、控制台档案断流） | — | 重启 cc-connect（daemon watcher 或 `cc-connect --config ~/.cc-connect/cc-exec.toml`） |
| LLM/缓存/成本 | `app/llm/client.py` | — | 重启 |
| 多轮历史 | `app/orchestrator/history.py` + `config.toml`（history_max_turns/history_persist） | `logs/history/`（运行产物，重启回填） | 重启 |
| 运行时护栏（并发/排队/任务持久化） | `app/guardrails/runtime/guard.py`（RuntimeGuard/TaskLifecycleStore）+ `config.toml [projects.runtime_guard]` | `logs/runtime_tasks/`（运行产物） | 重启 |
| 记忆服务（MemoryCore 进程托管） | `app/memory/lifecycle.py`（随 bot 启动拉起/关停整树回收；模型经 TDAI_LLM_* 与 bot 同源）+ `config.toml [projects.memory.lifecycle]`。独立部署形态与 8 项缺口（源码不在库 / aigw IP 白名单 / Node 22+ / .env 下发 / Linux 启动脚本 / 记忆数据迁移 / 网络可达面）见下一节 | `~/.memory-tencentdb/`（记忆数据） | 重启 |
| 收到即回执（ack/去向回执） | `app/main.py::_process_batch`（Step -1 ack）+ `config.toml [projects.messaging]`（ack_receipt/routed_receipt） | — | 重启 |


## 一次请求的生命周期（排障用）

```
飞书 WS → main.on_message → Aggregator(debounce)
  → TraceContext → RuntimeGuard 并发闸（[projects.runtime_guard]：
      max_concurrent 内直接执行；超出排队，队满/超时回复"系统繁忙"；
      启跑落 running 档案，结束落 finished → logs/runtime_tasks/）
  → feedback 预检（短文本负反馈回填任务档案）
  → memory.recall
  → CrewOrchestrator.execute（LangGraph）:
      router（deepseek-flash Function Calling，工具表=registry 派生，
              自报 confidence + intent_summary（SPEC-20260912）；
              幻觉工具名 → 白名单回落 direct_answer）
        → direct_answer（自答）/ route_to_help（清单）
        → route_to_clarify → clarify_node 反问确认（模型主动澄清，或
           confidence < [projects.router].clarify_threshold=0.6 自动降级）
        → is_complex（router 分解出 sub_tasks）→ decompose 节点按依赖分组执行 → gather
        → agent 节点（handler 注册优先于 transport 声明）:
            handler + run_with_message → 聚合分派（多轮连发+附件）
            handler                    → Handler.run()             [daily]
            cc_connect                 → webhook :9111（失败置 no_recover）[dev/skill-*]
      → gather（结构化门禁：error/⚠️/空响应 → 诚实告知，不调 LLM；
          剥离执行方末尾自评标记 → results_quality/retry_with 落档，不重试）
      → 回复
  → record_history → memory.capture → tracer.trace
  → TaskRecord 落 logs/tasks/（含 intent_summary/confidence/demoted/clarify_question）
  → 决策日志落 logs/decisions/（query→tool→confidence→demoted，与 TaskRecord 同 task_id）
```

## 需求规格与验收体系（2026-09-12 规范化）

- **Spec 模板**：`docs/specs/_template.md` — **用户操作时序（§二）必填且先于需求分解**：
  Given/When/Then 三段式，Then 一律 `[用户看到]` 开头（可观察面），每步标认知走查四问；
  R 需求标注服务的时序步骤（故事映射：功能挂骨干上）。方法论文献见模板附录 A
- **走查清单**：`docs/checklist/user-journey-walkthrough.md` — AI 走查表（四问 +
  `[用户看到]` 断言 + 证据）与人工走查清单（AI 验不了真机的步骤，单条 ≤2 分钟）两层
- **验收双层**：时序走查（用户座位）+ AC 对照表（系统座位），缺一不算完成；
  python-dev-guard 闸 3 已升级为该双层结构

## 意图识别测试体系（SPEC-20260912，2026-09-12 新增）

- **Spec**：`docs/specs/20260912-router-intent.md`（R1-R8 + AC 验收标准；验收脚本逐条对照）
- **双层门禁**：
  - 静态：`python app/eval/prompt_eval.py`（151 用例，验证提示词编码了路由语义，<80% 阻断）
  - 行为级：`python app/eval/router_intent_eval.py`（65 用例 v2.7 真实调 LLM 断言实际工具选择，
    含模糊→clarify、多轮指代、团队资产盘点→skill-catalog、澄清闭环，<85% 阻断；网络不可达 exit 2）
- **一键验收**：`python app/eval/verify_spec.py [--with-llm]` — 按 spec AC 逐条
  PASS/FAIL/SKIP 出报告，16 项断言（改路由相关代码后必跑）
- **专家模式规划**：各 agent.yaml 的 description 是路由识别的单源依据——写清
  「能力关键词 + 触发场景」；后续按 agent 域扩充 golden 用例即可持续强化各自专家能力
- **日志链路（排障）**：`logs/decisions/{date}.jsonl`（为什么这么路由）+
  `logs/tasks/{date}.jsonl`（整条编排：query→route_chain→spans(含 agent query_preview)→回复）
  同 task_id 串联，可完整回放一次请求

## deepseek-harness 迁移（执行面已切换，P3/P6 进行中）

- **决策**：`docs/adr/006-dsh-migration.md`（dsh 接管执行面+技能面+路由；feishu-agent-platform 留用户面+治理面）
- **需求 spec**：`docs/specs/20261104-dsh-migration.md`（R1-R13 + AC；6 条用户时序）
- **实测证据**：`docs/design/20261104-dsh-full-migration-spike.md`（技能目录三组对照 80.3% / 89.5% / 94.7%，
  这是 `includeDefaultRoots: false` + 白名单的依据；另有 ACP / MCP / 多轮实测）
- **一句话**：`@deepseek-ai/dsh`（ACP over stdio）替换自研编排 + cc-connect；技能目录零改写复用；
  飞书能力下沉 MCP 工具层；MemoryCore 退役改由 dsh 会话持久化承担
- **不得丢弃的资产**：golden 双层门禁、L2 审批+静态扫描、三红线（下沉工具层）、凭据铁律、热插拔、插件安装
- **进度**：见本文件「dsh 迁移阶段进度」
- **已作废（2026-11-06 删除，git 历史可查）**：9-18 版 dsh 重构 spec 与架构设计稿、
  `20261104-dsh-migration-plan.md`（进度已并入下方表格）

## dsh 迁移阶段进度

| 阶段 | 状态 | 证据 |
|---|---|---|
| P0 实测 | ✅ 2026-11-04 | `docs/design/20261104-dsh-full-migration-spike.md` |
| P1 骨架 | ✅ 2026-11-04 | `app/dsh/{adapter,supervisor,events,gateway}.py`；39 项契约测试 |
| P2 路由 | ✅ 2026-11-05 | 路由规则注入 + 真实技能目录；golden 分段全过（`deploy/dsh/verify_routing.py`） |
| P3 业务能力下沉 MCP | 🟡 进行中 | feishu / domains / capabilities / order_agent 已挂载；db-agent 已下线 |
| P4 技能分层 | ⬜ 未开始 | 白名单与 `includeDefaultRoots: false` 已就位，热插拔落点未改指 |
| P5 记忆 | ⬜ 未开始 | — |
| P6 可观测 | 🟡 进行中 | 进度卡与 TaskRecord 仍走旧链路；会话映射已落盘 |
| P7 退役 | ⬜ 未开始 | — |

## 能力热插拔（2026-09-12 新增）

- **功能简报**：`docs/features/capability-hotplug.md`（改这个功能先读那一页）
- **入口**：管理员发 zip / agent.yaml / SKILL.md 文件到会话 → 自动扫描建档 → 发审批卡片
- **安全闸**：静态扫描（凭据/危险指令/zip-slip/${ENV} 校验）high 自动拒（D3，无强制通过）；
  提交与审批都须 L2（D1）；MCP 只经新建 Agent 包承载（D2）；覆盖前备份旧版（D4）
- **热重建链**：`HotReloader.deploy` → registry.reload + setup_skills 重注入 → generator 生成物
  → `CrewOrchestrator.rebuild()`（重派路由工具 → 重编译 graph → 原子替换引用，失败保留旧图）
- **生效边界**：agent 类即时生效；skill/cc-connect 类新会话生效（存量会话需重启 cc-connect daemon）
- **凭据联动**：MCP env 必须写 `${ENV}` 占位符，管理员把真实值补进 `.env`；
  未配置的 env 变量会在审批回执里以降级清单提示

## 定时任务（2026-09-14 新增）

- **一句话**：用户说「每 30 分钟总结日报发到本群」→ schedule-agent 提取参数 →
  创建评估（超能力面驳回，通过即纳入调度）→ 调度循环到点交给 dsh 执行 → 回复发目标群/用户
- **能力面红线（评估驳回，确定性规则不调 LLM）**：
  1. 执行方只能是 `SCHEDULABLE_AGENTS` 白名单里的能力名（默认 `aihot`；skill 合成 Agent 不承诺常驻，不纳入）
  2. 投递目标只支持发消息（群 oc_ / 用户 ou_ 前缀 ID），不支持按名称投递、定时改配置、触发发布等写操作
  3. 扫描间隔 ≥ 10 分钟（store/loop/Handler 三层强制，`scan_interval_sec` 调低会被拉回 600）
  4. 只支持 `every`（每 N 分钟）/ `daily`（每天 HH:MM）两种周期；**一次性提醒不在能力面内**
- **状态机**：目录即状态（对齐能力热插拔）：新建直接落 approved（`pending/` 只剩历史档案，loop 收编）；
  approved ⇄ paused；approved/paused → cancelled（手动）；approved → done（过期归档）。重启后 approved 档案自动恢复排期（无内存态）
- **执行链路**：到期指令优先走 `DshGateway.handle`（与用户消息同一条 Agent Loop），未启用才回落旧编排器；
  结果 `channel.send` 到目标会话，**判 `SendResult.success`**（拒收不抛异常，只看「没抛」会记成成功）；
  执行统计（run_count/last_run_ok/last_error）回写 manifest
- **防重复**：① 排期落后于现在时改成 `now + interval`（停机欠下的轮次不补，对齐原生 Schedule
  「只补最近一次」）；② `_in_flight` 集合拦住「上一轮还在跑」的任务，避免同一任务并发两次执行
- **上下文透传**：`app/orchestrator/context.py`（contextvars）把 sender/chat/message_obj 传给
  进程内 Handler（schedule-agent 创建入口依赖 message_obj 回执）
- **单测**：`python -m pytest tests/ops/scheduler/test_scheduler.py -q`（评估红线/状态机/排期/执行统计）
- **golden**：`_golden/schedule-agent.json`（含间隔下限与能力边界驳回用例）

## 删除 Agent 能力的触点清单（2026-09-13 approval-link-agent 移除沉淀）

删除一个 agent 不只删 `bot/agents/{name}/`，按此 checklist 排查（以 approval-link-agent 为先例）：

1. `bot/agents/{name}/`（agent.yaml + CLAUDE.md）+ `bot/prompts/agents/{name}.md` + INDEX.md 行
2. `app/orchestrator/crew.py`：`_dispatchers` 分派项 + `_run_xxx` 方法 + docstring
3. `app/agents/registry.py`：VALID_TRANSPORTS（若是唯一使用者）
4. `app/eval/prompt_eval.py` `_ROUTE_ALIASES` 别名行 + `bot/prompts/_golden/` 正向用例
   （负向用例**保留**，防路由回退）
5. `app/observability/dashboard_service.py` agentLabel 标签
7. 文档三件套：`docs/project-index.md`（功能域行 + 日期）、`docs/features/*.md`（该域有简报就同步）
   、`docs/diagrams/feishu-agent-platform-runtime.archify.json`（仅当拓扑变了才改；改完须让 archify 重新
   deliver 出 `.html`，控制台内嵌的是生成物）
8. 同域 agent 的 CLAUDE.md/prompt 中「职责重叠」段落
9. `python app/eval/prompt_eval.py` 门禁 + `python -X utf8 -m mypy app bot` 收尾验证

## 环境与网络备忘

- 主编排器模型：`deepseek-flash`，模型三要素单源在 `.env`（`LLM_MODEL`/`LLM_BASE_URL`/`LLM_API_KEY`），网关 `aigw.example.com`（2026-09-29 从 qwen3.8-flash 换；同网关短请求约 1.5s，qwen3.8-flash 约 4.4s）
- 行为级意图门禁 `router_intent_eval.py` 依赖网关 IP 白名单：未加白时全部用例 403 落 direct_answer（exit 前 fail），
  这是**环境问题非回归**——先与 HEAD 基线对比确认 403 数量一致，再找网关管理员加白
- 凭据单源：`.env`（FEISHU_APP_SECRET / CC_WEBHOOK_TOKEN / LLM_API_KEY / AIGW_API_KEY，其余按需追加）；
  `config.toml` 与 `bot/agents/*/agent.yaml` 一律写 `${ENV_NAME}` 占位符或留空，运行时由 `load_config`/generator 从环境展开
- 内网网关需绕代理：`NO_PROXY="aigw.example.com,cmdb.example.com"`（Git Bash 下起进程时）
- cc-connect 侧 Claude 模型按各 agent.yaml 的 `model:` 声明（sonnet/haiku）

## 独立部署（bot + MemoryCore 上服务器）

结论：**可行**。MemoryCore 官方自带 Docker 生产镜像配方，standalone 模式零外部依赖
（`deployMode: standalone` + `stateBackend: local` + `storeBackend: sqlite`）；
bot 侧 `lifecycle.py` 的 spawn/健康轮询/关停回收已有 Windows 与 POSIX 双分支，
`manage_process=false` 可退化为「只连不拉」（对应 MemoryCore 独立容器化）。

形态 A（推荐）：docker-compose 双服务 `bot` + `memorycore`，记忆数据目录挂 volume，
`base_url=http://memorycore:8420` + `manage_process=false`。故障隔离、可独立重启。
形态 B：维持现状单进程树（省资源，但崩溃恢复绑 bot 重启周期）。

**部署前必须闭合的 8 项**（D1-D8，逐条都在部署路径上会直接挡住）：

| # | 缺口 | 处理 |
|---|---|---|
| D1 | MemoryCore 源码不在版本库（`tdai-agent-memory/` 被 gitignore，只在开发机） | 部署机 clone 上游并 **pin 版本**；禁止人肉拷贝 |
| D2 | aigw 令牌绑开发机 IP，服务器出口不在白名单 → 全部 LLM 403 | 部署前加白；服务器不设系统代理 |
| D3 | `core_dir` 默认是 Windows 个人路径 | 改 `config.toml` |
| D4 | MemoryCore 要 Node ≥22.16 + native 工具链（@node-rs/jieba、sqlite-vec） | 用官方 Dockerfile 天然解决；裸机要装 |
| D5 | 部署机需要完整 `.env`（不入库是对的） | 走密钥管理/CI secret，不落明文聊天 |
| D6 | `start-bot.bat` 仅 Windows | 补 systemd unit 或 compose（形态 A 下 compose 即入口） |
| D7 | 既有记忆数据在开发机本地 sqlite | 拷 volume，或接受记忆清零 |
| D8 | 服务器网络可达面（飞书 wss、模型网关、内网 Git） | 部署前逐条 curl 验证 |

顺序：先解 D2（硬阻塞）→ 形态 A 起步并写 `deploy/docker-compose.yml` → D5/D6 随 compose 解 → D7 建议清零重来。

## 提示词单源（2026-09-11 整改后）

- **8 个 agent 全部 `prompt_ref: "agents/{name}.md"`** — agent.yaml 不再内联 system_prompt
- frontmatter `depends_on` 在运行时生效：`PromptLoader.load()` 会拼接 shared/safety-rules、
  shared/output-format 等公共基座（legacy load 路径与 load_artifact 行为已对齐）
- 提示词清单：`bot/prompts/INDEX.md`（frontmatter 派生，勿手改）
- **运行时单源是 `bot/prompts/agents/*.md`**；`bot/agents/*/CLAUDE.md` 是目录自描述文档（人读），
  改动后需同步。两边说法不一致时以 prompts/ 为准并回写
- ~~router prompt 版本快照~~（已废除，2026-10-12）：改 `router/system_prompt.md` 即改运行时，历史由 git 承载；原要求：
  system/v{当前版本}.md（历史版本 v1.3.0 不追改，只在新版本快照中体现变更）

## 开发门禁工具链

- **专家包入口**：`agent-engineering-pack` skill — Agent 开发任务的整体路由（设计→代码→门禁），含成员边界裁定
- **流程**：`python-dev-guard` skill — 三道闸：静态检查 → 运行冒烟 → 业务验收
- **Agent 设计标准**：`agent-dev-standards` skill — 6 支柱（prompt 版本化/registry/工具 schema/评估/可观测/安全）+ gate checklist；其 validate 脚本目录约定与本项目不同，结论仅供参考
- **深度参考**：`.claude/skills-ref/` — 5 份 Python 工程实践参考（测试/异步/错误处理/类型/可观测，
  源自 wshobson/agents，MIT）。写对应类型代码前先读（路由表见 guard skill）
- **静态检查**：`ruff.toml`（`python -m ruff check app/ bot/`，`.agent/`、
  `bot/skills/digital-oracle/` 豁免）；真 bug 级拦截 F821/F811/E4/E7/E9 必须为零
- **类型检查**：`mypy.ini`（增量模式：只要求改动模块类型自洽；存量豁免见 ini 内 per-module 覆盖；
  运行方式 `python -X utf8 -m mypy app bot`（-X utf8 必需：mypy.ini 含中文注释，Windows cp936 读 ini 会崩）——ini 含 exclude 正则，不能用 `python -m mypy .`，Windows 下会触发 nul 盘符崩溃）
- **提示词门禁**：`python app/eval/prompt_eval.py`（golden 套件，score < 80% 阻断）
- **注释门禁**：`python -X utf8 -m app.eval.comment_lint app/` —— 只拦「变更日志式注释」
  （日期戳、「用户要求…」、「原来是…现在改成」、裸 TODO），0 容忍。为什么必须拦：
  这类注释对下一个改代码的人无信息量，且随代码腐化必然变成假话；改动历史属于 git log、
  CHANGELOG、简报。**注释只写"为什么必须这样"**，写不了就别写
- **凭据门禁**：`.git/hooks/pre-commit` 拦截已知凭据模式；`.claude/settings.json` 系生成物已退库
- **存量基线**：2026-11-06 全量清零（`python -m ruff check app/` → All checks passed）；
  F821/F811/E4/E7/E9 必修为零的门禁自此从「基线比对」升级为「绝对零」。
  清的是机械项（未用导入/变量、导入排序、noqa 失配、zip strict、隐式 Optional），
  顺带修了四类真问题：`heartbeat`/`ai_campaign`/日报模板按重构前路径找脚本
  （`app/scripts/`、`app/daily/`，重构后已搬走 → 运行期必失败）、MemoryCore 日志句柄
  只开不关（每次重启泄漏一个 fd）、`_warm_catalog` 悬空 task 可能被 GC 取消、
  `zip()` 缺 `strict=` 在长度不一致时静默截断；
  mypy 增量 36 项存量（同域），改动模块必须类型自洁

## 相关文档

- 架构与工作流：`CLAUDE.md`（根目录）
- 快速启动/运维命令：`README.md`
- **功能简报（改功能先读）**：`docs/features/` — 每个高频改动域一页纸（流程+期望+不变量
  带证据锚点），制度见 `docs/specs/20260924-fresh-loop.md`，模板见
  `docs/specs/_feature-brief-template.md`
- 提示词清单：`bot/prompts/INDEX.md`；变更记录：`bot/prompts/CHANGELOG.md`
- 架构决策记录：`docs/adr/`（一条决策一个文件，被取代时新建并链接，不改旧的）
- 设计文档：只剩 `docs/design/20261104-dsh-full-migration-spike.md` —— 它的存在理由是保留
  「不实测就会选错」的数字。**新功能不要再往 `docs/design/` 加文档**：取舍写 ADR，
  流程与不变量写 `docs/features/`，接口与数据模型以代码为事实源
- 结构图：`docs/diagrams/` 只放两份 `.archify.json` 源 + 两份 `.html` 生成物，
  源是单源、html 是 archify deliver 的产物；不要往里放截图或 visual-check 产物
- 飞书平台配置：`docs/feishu-platform-setup.md`
- 业务知识库：`.notes/`（foundation/patterns/ai-consulting）
