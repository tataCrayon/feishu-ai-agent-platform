## 2026-11-24

### 定时任务口径纠偏：删掉「须管理员审批」残句（`router/dsh_routing_rules.md` v2.2.0 → v2.3.0，`router/system_prompt.md` v1.14.0 → v1.15.0，`schedule-agent/agent.yaml` v1.0.0 → v1.1.0）

- **背景**：审批门 2026-11-08 已下线（`agents/schedule-agent.md` v1.6.0 有记录，代码侧
  `store.new_task` 直接落 `approved/`、`cards.py` 无按钮）。但**路由期**的两份文本漏改，
  仍写着「须管理员审批」——描述是模型判断「这是什么能力」的唯一输入，写着审批它就对用户
  说「等审批」，与真实行为矛盾。
- **改动**：`dsh_routing_rules.md` / `system_prompt.md` 的定时任务域改为「评估通过即生效，
  无需审批」；`schedule-agent/agent.yaml` 的 `description` 同步（它同时是 `/help` 能力清单
  与路由工具的 description）。
- **顺带补边界**：`dsh_routing_rules.md` 写明一次性提醒不在能力面内（宿主只支持
  `every` / `daily` 两种周期），要求如实说明、不假装已排上——此前「30 分钟后提醒我」
  既没有能力承接、也没有一句口径告诉模型该怎么答。
- **golden**：`_golden/schedule-agent.json` 两条 `expected_behavior` 去掉审批表述
  （该字段只提取函数名作关键字，不影响判分，但留着会误导下一个读者）。
- 门禁：`prompt_eval` / `prompt_index --check`。

## 2026-11-23

### Agent 更名：rwa-bot → dev-bot，显示名「RWA 业务助手」→「开发助手」

- **改名范围**：`bot/agents/dev-bot/agent.yaml`（name/display_name/description）、
  `bot/prompts/agents/dev-bot.md` v2.12.0、`replies/help_reply.md` v2.3.0、
  `config.toml` 的 `[[projects]].name` 与 `[projects.cc_connect].default_project`、
  `~/.cc-connect/cc-exec.toml` 的 project name、token_monitor 的 session 文件名口径。
- **理由**：这个通道是维护者的远程开发通道（`routable: false`，仅 `/dev` 直达），
  "RWA 业务"这个名字与实际能力面不符。
- **兼容**：历史 cc-connect session 文件已复制为 `dev-bot_*.json`（原 `rwa-bot_*` 保留不动），
  `.cursor.json` 游标键同步改名。
- 门禁：`prompt_eval` / `prompt_index --check`。

## 2026-11-20

### 日报固定成卡片（`router/dsh_routing_rules.md` v2.1.0 → v2.2.0，新增 domains 工具 `daily_report_card`）

- **行为**：日报类请求先发卡片——要点头「技术日报要点 · 日期」+ lead 一句话导读 +
  按 API 板块分区 + **每条标题可点**（`links.aihot`，缺失回落 `links.original`）+
  「查看完整日报」按钮。卡片发出后正文只回一句确认，不再把要点复述成文本。
- **兜底**：卡片发不出去时回执带 `push_text`，照原文发文本；对端无当日成品时
  如实说，不拿别的日期顶替。
- **原因**：同一天两张截图（11-17 纯文本要点、11-18 卡片）说明结构在漂——
  分不分段、链不链条目全看模型当轮心情。结构收进 `app/domains/daily/card.py` 由代码钉死，
  模型只决定「发哪天」。
- 门禁：`prompt_eval` / `prompt_index --check`。

## 2026-11-17

### easyprd 需求分析：自检项补齐 + 逐条打分（`agents/prd-watch-agent.md` v1.2.0 → v1.3.0）

- **检查项**：在「数量/集合/删除线/术语/状态覆盖」之外新增三类可证明的检查 ——
  放行词（无限制/一律/全部放行…未定义边界）、跨章节枚举一致性（同一维度枚举两处写法不同）、
  结构完整性（占位符残留、空章节）。全部是零判断的事实，每条仍带块 id 与跳转。
- **卡片按钮**：每条意见下从「采纳/修正/忽略」扩到再加「👍 有用 / 👎 误报」。
  采纳 = 往 PRD 新增一条评论（有副作用，只有人点才算授权）；打分 = 只落准确率样本，
  无副作用。两者分开记，避免把「多少人愿意动手」当成「判得准不准」。
- **边界**：分析意见没有现成评论，采纳是**新增一条**并锚在原段（讨论变更的采纳是
  回复已有评论）；对「这条挺好的」这类口头认可，要求人自己点按钮，不代点。
- 门禁：`prompt_eval` 8/8 通过（总分 91%，ALL SUITES PASSED）。

## 2026-11-16

### easyprd 扩展：会议整理 / 需求知识地图 / PRD↔UI 对账（`agents/prd-watch-agent.md` v1.0.0 → v1.1.0，新增 `agents/prd-watch-ui-diff.md` v1.0.0）

- **行为变化**：prd-watch-agent 从「查状态 + 催群讨论」扩到五类主动入口 —— 新增
  `ingest_meeting`（贴妙记/纪要链接即整理）、`analyze_requirement`（需求知识地图：
  PRD/会议/变更历史/父版本与相关需求）、`review_prd_ui`（PRD↔UI 文本层对账）。
- **新提示词**：`agents/prd-watch-ui-diff.md` 只判文本层（字段/入口/状态/文案），
  明确列出「颜色/间距/视觉层级不做」，避免模型假装看过图；置信度分带与
  `prd-watch-extract.md` 同一口径（`extract_min_confidence` / `extract_flag_confidence`）。
- **边界不变**：不写 PRD 正文、不猜群↔Story↔PRD 绑定、不凑变更。新增一句
  「台账未配就明说（知识台账 / CR 台账 / Figma token 缺哪个说哪个）」。
- 门禁：`prompt_eval` 8/8 用例通过（91% 总分，ALL SUITES PASSED）。

## 2026-11-11

### 代发消息能力：卡片出口（`router/dsh_routing_rules.md` v2.0.0 → v2.1.0）
- **能力变化**：feishu MCP 新增 `send_card_message`。此前模型手里只有 `send_message`
  （纯文本），「给李四发个消息卡片」只能如实回「接口只支持文本」——那不是找借口，
  是工具面确实没有卡片出口。
- **结构由代码钉死**：`app/protocols/notify_card.py` 负责渲染与校验（标题 / 正文 /
  字段行 / 脚注），模型只填纯文本。理由：卡片 JSON 差一处就整卡拒收，报错只有
  `invalid card content`，模型拿着它改不动。
- **署名取真实发送者**：副标题「来自 {PROMATE_CALLER_SENDER_NAME} 的消息」，
  env 缺失才回落到模型填的 `from_name`——与订阅/关注类工具同一口径，身份不由模型填。
- **边界**：`<at>` 标签一律剥除（卡片是 @全体 的唯一入口）；超长一律拒绝不截断；
  拿不到 open_id 必须如实说并给下一步，不许猜。

## 2026-11-08

### 定时任务审批门下线（`agents/schedule-agent.md` v1.5.0 → v1.6.0）
- **行为变化**：`create_scheduled_task` 评估通过即落 `approved/` 并纳入调度，回执卡片
  无按钮；删除「须 L2 管理员审批」「不承诺马上生效」「等管理员在卡片上点通过」等口径。
- **仍然拦的**：能力面评估（间隔 <10 分钟 / 执行方不在清单 / 目标非会话 ID）照旧驳回——
  那不是权限门，是「我们做不到」。
- **兼容**：审批门下线前留下的 `pending/` 档案由调度循环启动后首轮收编进 `approved/`，
  不让用户看到「任务凭空消失」。

## 2026-11-06

### CMDB 提醒开放订阅：按类型过滤 + 50 人规模性能（`agents/cmdb-watch-agent.md` v1.1.0 → v1.2.0）
- **能力变化**：`subscribe_cmdb_watch` 增加 `kinds`（sql / deploy / nacos / cron，
  可多选，不传 = 全收）。「所有我自己提交的工单都提醒」原本已由按姓名订阅覆盖，
  这次补的是「只要其中几类」的真实缺口，不新增链路。
- **边界不变**：只认发送者 open_id、只推本人 `creator` 的任务、只读不触发执行。
- **性能事实**（实测）：订阅 1 → 50 人，稳态每轮请求数不变（只随「还有任务未结束」
  的工单数增长）；已收口的单退出轮询预算，轮询并发 8 路后 30 张单 1468ms → 265ms。

## 2026-11-04

### CMDB 工单提醒扩展：指定工单关注（`agents/cmdb-watch-agent.md` v1.0.0 → v1.1.0）
- **新增三个工具**：`watch_cmdb_order`（按单号关注整单进度）/ `list_cmdb_watches` /
  `unwatch_cmdb_order`。语义：单里第一次出现「执行中」= 开始执行；所有任务到终态 =
  执行完成（失败也算完成，卡片标红）。**不是逐任务提醒**——逐任务那条是「按姓名订阅」。
- **两条链路并列写进 prompt**：姓名订阅（我的任务）与单号关注（整张单）互不覆盖，
  退订只影响各自那一条；工单号同样必须用户提供、不许猜。
- **`_golden/router-intent.json` v2.9 → v3.0**（72 → 76 例）：关注单号 / 贴链接 /
  查关注列表 / 取消关注。

### CMDB 工单提醒改为全员自助订阅（新文件 + 意图套件 v2.9）
- **新增 `agents/cmdb-watch-agent.md` v1.0.0**（角色）：只管「谁在收提醒」——三个工具
  （订阅 / 查状态 / 退订）+ 四条硬边界（只订阅本人、CMDB 用户名必须用户给、不改 CMDB、
  不承诺实时）。提醒本身仍由常驻旁路 `app/domains/cmdb_watch/` 打，不进消息路由。
- **`_golden/router-intent.json` v2.8 → v2.9**（68 → 72 例）：+4 例区分「订阅工单提醒」
  「盯我的发布单」「查订阅」「退订」，覆盖口语与否定式说法。
- **背景**：原实现把关注名单写死在 `config.toml`，只有管理员能改。改成每个用户在对话里
  声明自己的 CMDB 用户名即可订阅，订阅归属由发送者 open_id 决定，不收别人的工单。

## 2026-11-03

### easyprd 识别器支持多 PRD 归属（`agents/prd-watch-extract.md` v1.0.0 → v1.1.0）
- **背景**：一场「WebPortal RWA 接口对齐」同时改货架/申购/赎回/订单/持仓 5 篇 PRD，
  旧 prompt 只接收一篇 PRD 原文，整场会喂给一篇判 → 别的模块的结论被评论进这篇文档。
  用户实测发现该错位（比漏报代价更高：产品会在错的文档上核对，从此不信工具）。
- **改法**：输入改为**带 story_id 标记的候选清单**；每条变更必须回填归属的 `story_id`；
  新增「归属判据」一节（按页面/模块/流程对齐，PRD 自述的覆盖范围是强信号，一条只归一篇，
  拿不准就不输出）。代码侧对清单外的 story_id 直接丢弃并计数（`unattributed`）。
- **不变**：变更判据五类、置信度三分带、严格 JSON 输出（单篇路径行为一字不改）。

### easyprd 提示词落地（两个新文件 + 意图套件 v2.8）
- **`agents/prd-watch-agent.md` v1.0.0**（角色）：只承担「查监听状态 / 催一次识别」
  两个主动入口 + 三条红线（不改 PRD 正文、不猜群↔Story↔PRD 绑定、阈值之下不凑变更）。
- **`agents/prd-watch-extract.md` v1.0.0**（识别专用，由 `prd_watch/extract.py` 直引）：
  变更判据（须同时满足「明确改动意图」+「能对应到 PRD 条款」）、不算变更的五类
  （排期人员/复述提问/闲聊/被当场否掉/实现细节）、置信度三分带与严格 JSON 输出。
  与角色 prompt 分成两份的理由：识别器每轮窗口都调（旁路，无 Agent 会话），
  角色 prompt 只在人主动问时加载；合一份会让高频调用背上用不到的边界文案。
- **`_golden/prd-watch-agent.json`**（8 例）+ **`router-intent.json` v2.7 → v2.8**（65 → 68 例，
  新增 easyprd 状态/催识别/能力问询三条路由）。
- 门禁：`prompt_eval` 143/146 = 98% PASS（含新套件 8/8）；`router_intent_eval` 68/68 = 100%。

## 2026-11-02


### 回复篇幅按意图分级（output-format v1.1.0）
- **一刀切 ≤300 字 → 短/长两档**：短档（单一事实查询、回执、确认）仍 ≤300 字；长档
  （分析/方案对比/排查推理/趋势解读/报告）放开到 800–1200 字，允许分节标题 + 表格，
  结论先行再给论据。分级判据只在 `shared/output-format.md` 写一遍，
  config/daily/db/deploy/rwa-bot 五个 agent prompt 去掉各自硬写的「≤300 字」副本，
  改为引用分级表（`depends_on` pin 同步 `@1.1.0`）。
- **direct_answer v1.4.0 → v1.5.0**：自答此前一律短档，用户问「怎么选/为什么/怎么排查」
  只能拿到结论片段；现按档展开，并显式禁止「用短档敷衍明确要分析的问题」（等于答非所问）。
- **为什么可行**：回复全文本就落 `logs/task_replies/`（控制台「报告」tab 读），群消息
  长度不是唯一出口，长答复不额外占群屏。
- 门禁：`prompt_eval` 132/135 = 98% PASS；`prompt_index --check` 一致。

## 2026-10-12

### 提示词管理格式统一（2026-10-12，单文件化）
- **删版本归档与副本**：`router/system/`（13 个）、`router/decompose/`（2 个）整目录移除，
  `router/system_prompt.md` 与 `router/decompose_system_prompt.md` 升为**运行时唯一真源**
  （此前它们是 `stable.md` 的手工 `cp` 副本——升版靠人记着同步，必然腐化）。历史版本由 git 承载。
- **depends_on 全部加版本 pin**：13 个文件 43 条引用 `shared/xxx` → `shared/xxx@1.0.0`；
  `PromptLoader._inject_dependencies()` 解析 `@` 后缀，pin 与文件实际版本不一致时告警
  （单文件模式下没有可切换的旧版本可加载，pin 的价值是让漂移在日志里可见）。
- **删非标死字段**：`title`（18 个文件，与 `description` 重复且无消费者）、
  `golden_ref`（2 个，`_golden/*.json` 已自带 ref，是第二源）——`PromptMeta` 同步删除。
  保留 `name` / `token_estimate` / `changelog`（单文件化后 changelog 是唯一的版本历史载体）。
- **INDEX.md 改为派生物**：新增 `app/eval/prompt_index.py` 从 frontmatter 生成表格区
  （`<!-- BEGIN:generated -->` 标记），并接入 `prompt_eval.py` 门禁——INDEX 过期即阻断。
  根因：此前 INDEX 手工维护，实测 10 个版本号与 golden 用例数与 frontmatter 不一致且无人发现。

### 删除 router/judge_system_prompt.md v1.2.0、router/recover_system_prompt.md v1.1.0
- 独立的「结果判定」与「纠错重路由」节点一并移除（连同 judge.py / recover.py /
  reroute_rules 规则表）。178 条任务档案实测：judge 判 fail 的 43 次里 29 次
  （67.4%）换 agent 后依然失败；14 条 fail→success 逐条复核后真正由改道救回的
  只有 1 条（0.6%），其余 3 条是 direct_answer 编造的「能力确认」表格；该子集
  占 24.5% 成本、26.9% token，平均耗时 161.4s（全量均值 77.1s 的 209%）。
- `_golden/judge-agent.json`、`recover-reroute.json` 同批删除（只删 prompt
  不删 golden 会让 prompt_eval 判 prompt_loaded=False 直接阻断）。

### 新增 shared/self-check.md v1.0.0
- 执行类 Agent 输出前自评：末尾追加 `<<<QC quality="..." retry_with="...">>`，
  编排层在 gather 统一剥离并落 TaskRecord（results_quality / retry_with），
  **只观测不裁决、不触发自动重试**。
- 取代 judge 的语义判定能力：`failed`=答非所问、`unclear`=需补料（合法终态）。
- 10 个 agents/*.md 的 depends_on 追加本文件并升版（新增指令 → minor）：

### agents/*.md（10 个）— depends_on 增 shared/self-check
| 文件 | 版本 |
|---|---|
| config-agent / db-agent / deploy-agent / skill-catalog-agent | 1.2.0 → 1.3.0 |
| digital-oracle-agent / merge-check-agent | 1.1.0 → 1.2.0 |
| daily-agent | 3.1.0 → 3.2.0 |
| rwa-bot / webportal-agent | 2.9.0 → 2.10.0 |
| schedule-agent | 1.3.0 → 1.4.0 |

### 配套（非 prompt）
- TaskRecord：删 `judge_verdicts`/`recover_count`，增 `results_quality`/
  `retry_with`/`gate_blocked`；status 枚举 success/error/**failed**（原 giveup/empty 消失）。
- 门禁修复：verify_spec 的 subprocess 输出按 UTF-8 解码（此前 Windows 下 GBK
  解码失败使 AC-8.1/8.3 拿到 `stdout=None` 而**假失败/假通过**）；FakeRouter 桩
  补 `**kwargs`（AC-3.1/3.2）。verify_spec 由 13/16 升至 **16/16**。

## 2026-10-08

### agents/webportal-agent.md v2.9.0 — 案例定稿闭环 + 案例文档链接改条目
- 新增 confirm_case：**人工确认是定稿的唯一入口**。采集物恒为 draft，只有用户
  明确说「确认/没问题」才改 verified 并重推镜像；禁止沉淀后自行确认。
- 镜像同步成功后自动把子文档链接写回本地案例 front-matter 的 `mirror_ref`
  （本地知识库 ↔ 飞书镜像的唯一映射）；缺映射的案例不许定稿，引擎侧拦。
- 案例文档链接改用 `case_ref.url`（案例知识库**条目**子文档），不再渲染溯源
  `case_sources` 的飞书手册/工单链接——依据已在条目「## 来源」段里，两份材料
  会让审核侧分不清。配套改 `_format_sql_payload` 的渲染分支。

### router/system/stable.md v1.14.0 — 移除飞书文档/知识库问答域
- 团队知识库查询移交飞书豆包工作，feishu-doc-agent 整体移除（agent 目录/
  handler/提示词/golden 正向用例一并退役）。工具表由 registry 派生自动收敛，
  业务背景域与「不在工具表」名单去掉飞书文档条目。
- 配套（非 prompt）：`[projects.knowledge]` 配置与 `_kb_grounding_context`
  自答知识库接地拆除；reroute 规则「文档/知识库 → feishu-doc-agent」删除；
  lark_cli 仅保留案例镜像所需的 get_doc_content/create_wiki_node/update_doc_markdown。

### router/direct_answer_system_prompt.md v1.4.0 — 移除知识库接地
- 用户帧不再附「团队知识库参考」块；自答回到参数记忆 + 诚实声明。

### 删除 agents/feishu-doc-agent.md v1.2
- 飞书文档/知识库问答角色提示词随 agent 移除。

## 2026-10-07

### agents/webportal-agent.md v2.8.0 — 案例沉淀扩源 + 飞书案例库镜像同步
- ingest_order 素材二选一：CMDB 工单号（order_id）或飞书文档链接（feishu_url，
  本侧取正文）；落 draft 后**自动同步飞书案例库镜像**（parent_node_token 建子
  文档 + index_doc_token 更新目录索引，回读校验一致才算完成）。用户要沉淀案例
  却没给来源时，先问清是工单号还是飞书文档链接。
- 新增 sync_case 语义：案例定稿（verified）后更新飞书镜像，或镜像缺失/过期时
  补同步；不改本地案例内容。
- 配套（非 prompt）：config.toml `[projects.case_mirror]` 收口写目标；路由
  `_TICKET_HINT_RE` + reroute 规则确定性兜「沉淀案例/案例入库/同步案例」话术
  （排在飞书文档路由前）；agent.yaml v3.1.0 描述同步。

### replies/help_reply.md v2.2.0 — 清单新增「案例沉淀」
- 与 webportal-agent v3.1.0 动态清单同口径（本地案例库 + 飞书案例库镜像）。

## 2026-10-05

### router/system/stable.md v1.13.0 — 催单不改判（历史失败不改道拒答）
- 实测审批 DATA2026093002 连续催单（第 4、5 轮）被误判 direct_answer：第 5 轮
  router LLM 合法自选 direct_answer conf=0.9，intent_summary 写成「服务不可达
  且缺参，需拒答并列缺口」——把会话历史里的旧失败当成路由依据，用户明明还在
  要完整 SQL，收到的却是硬拒 + 「走工单 Agent」的二跳指引。
- 注意事项新增两条：**催单/重复请求不改判**（路由只看当前消息意图，历史失败/
  拒答/缺参不构成改判依据，能不能交付是 Agent 层的事）；**工单/审批强特征
  不落 direct_answer**（流程编号 DATA 单号、审批短链、明确要 SQL → 永远
  webportal-agent）。
- 配套（非 prompt）：router.py 成功路径 hint-override——LLM 调 direct_answer
  但 `_TICKET_HINT_RE` 命中时确定性改判 webportal-agent（extra_calls 保留），
  与 no-tool-call/unknown-tool/llm-failed 三条兜底同口径；`router-intent.json`
  golden 含审批短链用例。

### agents/webportal-agent.md v2.6.0 — 审批链接解析能力（fetch_approval）
- 目标刚性：审批短链 → 表单内容 + 附件 → 交工单管线（用户裁定：旧的「读不了」
  限制作废，兜底只在彻底失败时）。
- 新增 fetch_approval 工具（url+context）：短链 302 探测 → 关联用户审批列表
  （L0 流程编号 DATA\\d+ 精确 → L1 短链数字 ID==task_id → L2 待办/知会唯一
  自动选定 → L3 歧义列 top-5 候选回序号）→ `GET /approval/v4/instances/detail`
  拉 form 全文；attachmentV2/imageV2 widget 的 value 即 12h 附件地址，下载后
  以文件 Part 自动随工单携带（app/domains/webportal/approval_link.py）。
- 红线 4 重写「先读再处理」：链接→fetch_approval→按意图 process_order 或直接
  答复；「请原样转达」段落照搬；歧义等序号续接（5 分钟 pending，取即删）。
- 配套（非 prompt）：agent.yaml description += 审批链接解析（路由单源）；
  prompt_eval `_ROUTE_ALIASES` 死行 route_to_approval_link_agent 并入 webportal；
  golden router-intent v2.6（63 例，+审批短链用例）；CLAUDE.md 清 stale
  approval_link transport 行。

## 2026-10-04

### agents/webportal-agent.md v2.5.0 — 去 vendor 切 A2A
- 工单引擎（原 bot/skills/order-sql 自包含包）整体迁出为独立 A2A 服务
  webportal-order-agent（独立仓库，127.0.0.1:8080，`python -m order_agent.a2a_server`，
  独立迭代独立发布）；feishu-agent-platform 内 handler 换为 A2A 客户端壳
  `app/agents/handlers/webportal_handler.py`（替代 order_sql_handler.py）。
- prompt 同步：标题/描述/首行去「order-sql 引擎」字样改「webportal-order-agent
  A2A 服务驱动」；能力段附件语义改「附件由系统随工单自动携带（服务端解析），
  ticket_text 只含文本描述」。
- 配套（非 prompt）：agent.yaml v3.0.0（timeout_sec 120→180，注释改 A2A）；
  健康探测改 Agent Card 可发现；multimodal 自带引擎无关文档解析。

## 2026-10-02（下午）

### router/system/stable.md v1.12.0 — 能力盘点双出口（动静结合）
- 实测两次「我们有哪些SKill和Agent能力？」「团队有哪些Agent和Skills?」被误判
  help（15:07），用户收到纯自我介绍 + 引导行，想要的是团队资产清单——v1.11.0
  的「盘点一律归 help」矫枉过正。
- 规则 5 改双出口：**第二人称自我介绍**（你能做什么/你是谁）→ help 秒回；
  **团队资产盘点**（我们/团队 + Skill/Agent/能力/技能字样）→ skill-catalog
  （数据源=本地快照毫秒级，不再是 96s 慢查询）。
- 配套 skill-catalog-agent v1.2.0：`list_team_skills` 新增 view 参数，
  盘点用 overview 紧凑视图（31 项能力按 SDLC 链分组、同名多分支合并，
  `query.py::format_inventory_overview`）；建设情况/分支明细用 full。

### router/judge_system_prompt.md v1.2.0 — 意图履行检查（双锚点）
- payload 新增 `intent_summary`（路由层复述的意图，crew `_judge_node` 传入）。
- 判定以「用户原话 + 复述意图」双锚点中更具体者为准：回复覆盖原话字面但
  回避复述意图的核心诉求（要资产清单只给引导行）→ 判不满足 → recover 改道。
- **judge 覆盖面扩到 help 路由**（crew `help_node → judge` 新边）：help 是
  模板自我介绍，资产盘点被误路由到 help 时由 judge 兜底改道 skill-catalog。
- recover 确定性规则（reroute_rules.py）：盘点关键词（有哪些skill/哪些agent/
  技能库/能力清单…）→ 强制 route_to_skill_catalog_agent；置于表首防被
  skill_ranking 规则抢走（「我们有哪些Skill」不含「哪些」前缀会误改道日报）。

## 2026-10-02（上午）

### router/system/stable.md v1.11.0 — 意图预判 + 能力盘点出口收敛
- **路由规则 0（意图预判）**：调用任何工具前先在 intent_summary 复述「用户要做什么」，
  复述与所选工具对不上时换目标或降 confidence——LangGraph supervisor/
  OpenAI structured-output 路由的 CoT 思路，压缩幻觉 tool 名与边界误判。
- **能力盘点出口收敛**：「我们团队有哪些AI能力」「你能做什么」类整体能力清单
  问题一律 route_to_help（秒回+口径统一，清单尾由编排层追加团队技能库引导行）；
  skill-catalog 只承接技能治理（立项查重/审查缺项/状态板）。2026-10-02 实测
  该类问题在 help/direct/skill-catalog 三出口漂移，skill-catalog 全量扫描
  慢查询 96s，两个变体各答一份口径。
- 配套：crew `_help_node` 清单尾追加「📦 团队技能库另有 N 项 Skill 资产」引导行
  （数量读本地快照，失败静默省略）；`_gather_node` 常规路由尾注防重
  （handler 模仿历史尾注导致双条「由 XX 处理」，对称 recover 分支已有防重）。
- golden：router-intent.json v2.4（+4：能力盘点×2 → help、技能治理 → skill-catalog、
  澄清闭环「查库那个」→ db）。

### router/direct_answer_system_prompt.md v1.3.0 — 知识库接地
- 自答引擎此前零事实源（仅参数记忆 + 能力清单），团队私有知识（流程/口径）编造
  风险高。编排层在问句特征命中（是什么/怎么/流程/制度/？等，长度 ≥10 字）时，
  用 LarkCli 检索团队知识库空间（config `[projects.knowledge].space_id`，
  与 feishu-doc-agent 同源）top-3 注入用户帧；检索 5s 超时/未配置/失败均静默
  降级（对齐 memory recall 容错风格），问候/闲聊零开销。
- prompt 新增「知识库接地」节：命中条目引用内容并附来源链接；「未命中」声明
  时诚实说明知识库没找到，基于通用理解回答并标注仅供参考。

### 编排层（非 prompt）配套修复
- `reroute_rules.py`：命中下线 Agent 的纠错规则跳过（feishu-doc-agent 可被
  控制台 agent_switches 下线，下线期间知识库关键词不再强制改道到不可达目标）。
- `history.py`：新增 `record_pending`/`pop_pending`（澄清 pending，内存 +
  5 分钟 TTL，取即删）；`crew._clarify_node` 反问时记录、`execute` 路由前取出
  拼入 memory_context——用户回「对，就是那个」「查库那个」可结合候选定向，
  澄清闭环成立。

## 2026-09-30

### agents/webportal-agent.md v2.4.0 — 工单编号只认本轮（防串单）
- 新增红线 6：ticket_text 只认本轮消息（含「引用的上一条」与附件），
  历史轮次的旧工单编号/正文禁止带入；本轮没有工单正文时先向用户要。
- 背景：2026-09-30 DATA2026093002 工单被串成历史 DATA2026092903 出单——
  引用正文在编排层丢失后，LLM 只能拿会话历史里的旧工单顶替。
  编排层修复（aggregator 挂回 prefetched 文本 + raw.parent_id 兜底）是主修，
  本红线是 LLM 侧兜底。

### agents/skill-catalog-agent.md v1.1.0 — 规范状态层（现状总表 + 单技能状态卡）
- 新增工具语义：get_skill_status_board（状态计数/八项缺口/近 30 天活跃/分支在飞）、
  get_skill_status（草稿/生产/未验证/下线候选 + 八项字段逐项打勾）。
- 状态语义约束：复审过期是状态变化不是提醒，照工具依据句转述不软化；
  状态升级靠作者补字段走 MR，Bot 不代填。
- golden 增 4 例（总表×2 + 状态卡×2），路由别名增「技能建设情况/规范状态/技能库现状」。

## 2026-09-29

### router/system/stable.md v1.10.1 — 运行时入口改到 stable，背景去掉已下线域
- `PromptLoader.load_router_system_prompt()` 改为读 `router/system/stable.md`。
  此前进程读 `router/system_prompt.md`（停在 1.9.0），1.10.0 的能力是非题规则没有生效。
- 业务领域背景去掉数据库查询、发布管理、配置管理、飞书文档。这四项已强制下线。
- `v1.10.1.md` 与 `system_prompt.md` 同步为副本。golden ref 改为 `router/system/stable.md`。

### agents/rwa-bot.md v2.9.0 — /dev 秒回清单与团队仓对齐
- 去掉已下线的发布管理，补上数字先知与团队 Skill 目录。
- 不再写 mysql-guard Agent / cmdb-guard Agent。数据库、配置、发布指向 infra-ops-skills 上的 Skill。

### router/direct_answer_system_prompt.md v1.2.1
- 自称改为中台核心组数字助手。超范围请求不再把已下线入口说成专用工具。

### agents/skill-catalog-agent.md v1.0.0 — 团队 Skill 目录
- 新 Agent：按分支读 description，回答「有哪些 / 会不会重复 / 审查缺什么」，并给出 Git 链接
- 不安装、不写回、不把「没有接近项」说成可以立项
- 失败回退文案（`app/prompts/loader.py`）去掉已下线的 /db /ticket /findcode，
  不再把 rwa-bot 写成综合业务兜底。只在 prompt 文件加载失败时启用

## 2026-09-28

### agents/daily-agent.md v3.0.0 — 日报单源化：AI HOT 取数落本地缓存（旧目录读取下线）
- 数据路径收敛为单源：AI HOT（aihot.virxact.com）经 `app/domains/daily/aihot.py` 取数，
  即落本地缓存 `logs/daily/cache.json`（历史日报一经生成不变，缓存即终态，
  再读零 API 成本）；新增 `get_daily_by_date()`（按日期取，缓存优先）与
  `list_daily_archive()`（归档列表 = 本地缓存 ∪ API 索引，cached 标注）
- 旧 LocalKBase 目录多数据源（tech_daily/trend_tracking/skill_ranking + sources.json +
  DailyReader/DailySourceRegistry）运行时路径全部下线：daily_handler 重写为
  read_daily_report(date)/list_available_reports 两工具；DailyScheduler 改
  AIHOTDailySource 注入（converter 退役）；health 探测改「API 可达 + 缓存存量」
- 涉及代码：app/domains/daily/{aihot,scheduler,store,__init__}.py、
  app/agents/handlers/daily_handler.py、app/ops/health/__init__.py、app/main.py
- golden：daily-agent.json 重写 v3.0.0；recover-reroute/judge-agent 去 skill_ranking 源名；
  router-intent.json v2.3（+3 能力是非题用例，55 例）

### router/system v1.10.0 — 能力是非题边界（help 边界 v2）
- 实测「你应该不能处理开户权限流程才对」被误判 route_to_help，用户收到一屏能力
  清单却没得到是非回答；新增规则：能力是非题不落 help——清单外能力 direct_answer
  诚实确认做不到并指最近能力；清单内能力简短确认并引导直接说需求
- stable.md → v1.10.0；router-intent.json v2.3 新增 3 例（含实测误判原句）

## 2026-09-25

### shared/persona.md v1.0.0 — 公共人格基座上线（靠谱同事人设）
- 班味治理第二层：机制文案（进度卡/回执）已去公文体重写；本基座管 LLM 回复的
  「我是谁/什么语气」——平级（你/我）、结论先行、禁语清单（收到/您的需求/亲/
  处理过程如下/请稍后重试等客服腔）
- 与 output-format.md 分工：persona 管人格语气，output-format 管排版表情；冲突以 output-format 为准
- 注入方式：全部 10 个 agents/*.md 与 router/direct_answer_system_prompt.md v1.2.0
  depends_on 新增 shared/persona（minor 版本，不破坏缓存前缀）；loader 自动拼接

### 机制文案去班味（代码层，无 prompt 变更）
- 进度卡（app/main.py）：卡头「🛠 处理中 · AgentMate」→「⏳ AgentMate」，
  删「收到消息，处理过程如下：」开头与「完成后将直接回复处理结果」footer
- 事件文案（app/orchestrator/crew.py）：「正在阅读你的消息」→「正在想该派谁处理」，
  「已经理解你的需求，正在交给 X」→「交给 X」，「同时交给 X 并行处理」→「同时交给 X」，
  giveup 文案去「您」、late_delivery 回执精简
- 错误兜底（app/main.py）：「请稍后重试」类改口语化短句

## 2026-09-24

### agents/digital-oracle-agent.md v1.0.0 — 数字先知 Agent 上线（市场数据概率预测）
- vendoring komako-workshop/digital-oracle（MIT）引擎至 `bot/skills/digital-oracle/`
  （enabled:false 自包含包，同 order-sql 先例；13 个免费数据源零 API key）
- 执行通道 handler（`app/agents/handlers/digital_oracle_handler.py`），**不走 cc-connect**：
  单工具 gather_signals 并行拉取（引擎线程池 + 45s 整批超时 + 单源失败不连坐），
  信号渲染按 spec.type 匹配（key 是 LLM 起的中文名）
- prompt 承载方法论（只交易数据不读新闻 + ≥3 独立信号 + 时间尺度分层）+ 17 类信号菜单
  + 结构化报告模板；golden `_golden/digital-oracle-agent.json` 6 例
- `_ROUTE_ALIASES` 增 route_to_digital_oracle_agent（数字先知/概率预测/市场信号）

## 2026-09-17

### agents/webportal-agent.md v2.2.0 — 群附件自动转文本（缺口 1：文件消息→order-sql）
- 事故复盘：用户把工单文件（3 个 .msg）与「处理这个工单」分两条消息发出，
  附件链路断裂——channel 白名单不收 .msg、引擎不解析邮件、LLM 看不到本地路径、
  失败即清缓冲丢上下文，最终 direct_answer 谎称"没有文件解析工具"
- 修复四触点：channel 下载白名单 +.msg/.eml；引擎 read_ticket_text +邮件解析分支
  （extract_msg / email stdlib）；handler run_with_message 确定性把附件转文本注入
  ticket_text（剥 `<file/>` 占位符噪音），缓冲仅在引擎成功出单后清空（TTL 10 分钟可续跑）；
  prompt 能力声明同步（禁止再声称无法读附件）
- 依赖：order-sql requirements +extract-msg>=0.41

### agents/merge-check-agent.md v1.0.0 — 分支合并检查 Agent 上线
- 接入外部 merge-check 服务（GET /api/merge-check，GitLab 分支对比封装）：
  check_branch_merge(branch_type=all/release/bugfix, project_ids 可空)
- 执行通道 handler（app/agents/handlers/merge_check_handler.py），配置
  `config.toml [projects.merge_check]`；内网直连 trust_env=False，超时 120s
- golden `_golden/merge-check-agent.json` 5 例 + `_golden/router-intent.json` v2.2（+2 例）
- 三处同源文案同步：rwa-bot.md v2.7 秒回清单、help_reply.md v2.1、agent.yaml description

## 2026-09-16

### router/system_prompt.md v1.9.0 — help 边界规则（具体能力的细节问题不落 help）
- 实测「我们技术日报的数据来源是哪里？」被 Router conf=0.8 误判 route_to_help，
  用户只收到自我介绍清单，答不出数据来源
- 规则 5 增加边界：help 只承接整体能力清单/自我介绍/能力版本号；某具体能力的
  数据来源/原理/用法/限制 → 路由到该专业能力 Agent
- 同步 `_golden/router-intent.json` v2.1（+3 用例：日报数据来源×2、定时任务扫描机制×1），
  prompt_eval 50/50=100%；stable.md/v1.9.0.md 快照同步

### agents/daily-agent.md v2.1.0 — 实时生成底层切换为 AI HOT
- 旧 ai-daily-report skill 管道（境外 RSS + GitHub 渲染 PNG）因抓取源在境内网络
  长期不可达下线；generate_daily_report 底层改为 AI HOT 精选日报 API
  （aihot.virxact.com 匿名只读 v1，技能包 bot/skills/aihot，实现 app/domains/daily/aihot.py）
- 语义变化：图文报告发图 → 文字版日报直接发会话；成品未生成时回退过去 24h 精选简报；
  耗时 1-2 分钟 → 5-20 秒
- 同步 CLAUDE.md 简版、golden `_golden/daily-agent.json`（generate 用例期望行为）、INDEX 行
- 定时推送链路（DailyScheduler）同步接入 AI HOT 兜底：本地 tech_daily 无当日文件时
  自动回退，修复 LocalKBase 停更导致的断推
- 日报每天只生成一次：AI HOT 结果写 logs/daily/cache.json 持久缓存（含 markdown），
  实时生成与定时推送共用同一份，当日再次请求直接复用不再打 API
- 修复 handler 基类多轮工具循环 tokens_in/tokens_out 未初始化崩溃
  （「今天的技术日报」失败真因：handler error → judge fail → recover 放弃）

## 2026-09-14

### agents/schedule-agent.md v1.0.0 — 定时任务 Agent 上线
- 新增 schedule-agent（transport=handler）：创建定时任务（create_scheduled_task）
  与查询档案（list_tasks）；创建须 L2 管理员卡片审批后生效
- 提示词编码三条能力面红线：执行方仅 rwa-bot / 目标仅 oc_/ou_ 会话 ID 发消息 /
  扫描间隔 ≥ 10 分钟，超界请求如实解释并给改法，不创建
- 新增 golden 套件 `_golden/schedule-agent.json`（5 例：daily/every 创建、
  间隔下限驳回、能力边界驳回、列表查询）
- 配套运行时：`app/ops/scheduler/`（评估/审批/调度/执行）+ `[projects.scheduled_tasks]` 配置
- router 提示词同步 v1.5.0：业务领域背景新增定时任务域（路由意图归 schedule-agent）

### router-system-prompt v1.5.0 — 新增定时任务域
- 业务领域背景新增「定时任务：创建周期/定时执行的任务（须管理员审批，间隔最低 10 分钟）」
- 配合 schedule-agent 注册进 registry（路由工具表自动派生 route_to_schedule_agent）

## 2026-09-13

### agents/feishu-doc-agent.md v1.1.0 — 团队知识库优先
- 团队化知识库落地（[projects.knowledge].space_id 单源配置）：
  search_docs 优先在团队知识库空间内检索，命中标记 `knowledge_base_hit`
- 新增 `list_knowledge_nodes` 工具：浏览知识库目录树（可逐层展开），结果附文档链接
- 引用规范强化：知识库命中的回答须说明"来自团队知识库"
- 临时空间升正式/换空间只改 config.toml 一行 + 重启

### agents/rwa-bot.md v2.4.0 — 秒回自我介绍与 _help_node 对齐
- 秒回自我介绍重写：去除已下线的 /db /deploy /config /daily /ticket /findcode /ai
  斜杠命令清单（编排层早已无命令解析），改为与编排层 `_help_node` 一致的
  自然语言分组（🤖 智能助手 / 🛠 专业工具 / 🧩 扩展技能）
- 示例句对齐当前真实能力（ELK 日志查询、工单转 SQL、技术日报）
- 同步 `bot/agents/rwa-bot/CLAUDE.md` 生成物；`replies/help_reply.md` 仍是
  无消费者的备用模板，INDEX 已标注勿直接启用

## 2026-09-12

### router-system-prompt v1.4.0 — 意图识别强化（SPEC-20260912）
- 所有路由工具新增 `confidence`（0~1 自报置信度）与 `intent_summary`（意图复述）参数
- 新增 `route_to_clarify` 内置工具：意图模糊/多候选难取舍时反问确认（question + candidates）
- 编排层新增 `clarify_node`：低置信度（< clarify_threshold，缺省 0.6）自动降级为反问，不经 judge
- 新增行为级回归 `_golden/router-intent.json`（21 用例）+ `app/eval/router_intent_eval.py`
- 决策日志：`logs/decisions/{date}.jsonl`（query→tool→confidence→demoted 全链路）
- 静态门禁 prompt_eval 61/61 + 行为回归 95% 双层验收

## 2026-08-24

### direct_answer 自答引擎 v1.0 — 新增
- 新增 `router/direct_answer_system_prompt.md`（v1.0.0）：编排层自答引擎，
  替代「路由模型在 function-call 参数里顺手 reply」的低质量自答
- 配套 golden 套件 `_golden/direct-answer.json`（6 case：问候/通用问答/
  上下文追问/超范围引导/不编造/风格）
- 同步修改 `router.py` ROUTER_TOOLS 中 direct_answer 工具 description：
  语义从「仅问候闲聊」扩为「通用问答兜底」，reply 参数改为可选
- 动机：agent-dev-standards 评估发现自答能力薄弱 + 监控断路，
  本次同步落地 TaskRecord 任务档案与负反馈闭环

## 2026-08-14

### v1.0 — 初始版本
- 从代码中提取所有硬编码提示词到 `.agent/prompts/` 目录
- 新增 YAML frontmatter 版本管理规范
- 新增 `app/prompts/loader.py` 统一加载器
- 迁移文件：
  - `app/orchestrator/router.py` → `ROUTER_SYSTEM_PROMPT` → `.agent/prompts/router/system_prompt.md`
  - `app/orchestrator/router.py` → `DECOMPOSE_SYSTEM_PROMPT` → `.agent/prompts/router/decompose_system_prompt.md`
  - `.agent/agents/*/agent.yaml` → `system_prompt` → `.agent/prompts/agents/*.md`
  - `app/main.py` → `help_handler` 回复 → `.agent/prompts/replies/help_reply.md`