# 功能简报：easyprd · PRD 变更助手

> 改这个功能前读这一页即可动手。与代码矛盾时以代码 + commit 为准，并立即回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：`docs/specs/_feature-brief-template.md`

## 1. 一句话

群讨论 / 会议纪要 / UI 设计稿里的需求变更与差异，被识别成「改什么、凭什么、谁来定」，
写成 PRD 文档评论并推一张群卡片让产品一句话拍板；Bot 只写评论，**不改 PRD 正文**。

四段能力，别混：

| 能力 | 入口 | 产出 |
|---|---|---|
| **旁路监听**（默认关 + dry_run） | `[projects.prd_watch]`，白名单群非@消息攒窗 | PRD 评论 + 群卡片 |
| **手动入口**（2026-11-23 起不配群也能用） | `prd_watch_status` / `analyze_story` / `analyze_requirement` / `ingest_meeting` / `review_prd_ui` | 按 Story ID/链接直接评审，无需群绑定 |
| **会议整理** | `ingest_meeting`（贴妙记/纪要链接）/ `minutes_story_map` / `meeting_auto` 扫描 | 同上 + 知识台账会议行 |
| **需求知识地图** | `analyze_requirement`（Story ID / 链接 / 名称） | 只读：PRD/结论/变更历史/同族需求 |
| **PRD↔UI 对账**（文本层） | `review_prd_ui`（Figma 链接） | 同上（差异走同一套评论/卡片） |

与 pm-hub 的边界：本能力产出「PRD 评论 + 变更台账」，pm 产出「个人交付事项/日计划」；
数据不互通，判定标准也不同（见 §4 「与 PM 的边界」）。

## 2. 主链路

```
入料
  群消息（watch_chats 白名单内才放开非@监听）
    → app/main.py on_message 截断 → watcher.handle_message 攒 window_sec
  会议，三条路
    → ingest_meeting：用户贴妙记/纪要链接（meeting.from_link 判形态）
    → minutes_story_map：显式映射，service._loop 定时 poll_minutes
    → meeting_auto：按需求名关键词 minutes +search（唯一命中才整理）
  设计稿
    → review_prd_ui：Figma REST design data → ui.build_ui_frame_text
加工（三条来源汇入同一 watcher._run）
  extract（讨论→变更）/ UiMatcher（PRD↔UI→差异），二者共用置信分带阈值
  → dedupe（批内近义合并 + 跨轮按素材键去重）
  → 按 Story 分组 → anchor 定位段落 → 文档侧判重 → dry_run 闸
产出与闭环
  → 写 PRD 评论（bot 身份）→ 发群卡片 → CR 表落 pending
  → 决策：卡片按钮（即时）/ 评论回复轮询（poll_replies_sec）/ N 天超时催一次
  → 会议来源额外写知识台账（会议纪要表 + 需求主表最新结论 + 变更记录）
```

## 3. 期望行为（可观察面）

- 一次讨论 / 一场会只出一张卡（同一素材跨多个 Story 也只发一张），逐条发会刷屏。
- 评论第一行就是署名 `🤖 Bot EasyPRD 自动评论 · CR-{YYYYMMDD}-{StoryID}-{序号}`。
- 置信度落在中间带 → 评论末尾加「⚠️ 此条由 AI 自动识别，置信度较低，请人工确认」。
- 采纳 → 群通知必须写明「**PRD 正文需人工按评论表述更新**」，不写「已生效」。
- 修正 → 先不改状态、不写评论，回「请回复修正后的表述」，30 分钟内且只认点按钮那个人。
- 忽略 → 评论标 ❌ 并 resolve；归档。
- 超时未决（默认 3 个工作日）→ 群内提醒一次，不再重复催。
- 同一场会贴两次 → 第二次回「之前已整理过」，不重复写评论（去重键是素材 token）。
- 会议读不到（他人妙记无权限）→ 报具体原因并建议换智能纪要链接，不说「没有内容」。
- PRD↔UI 对账 → 明确说「仅文本层」；素材只有图片没有 TEXT 图层时如实说没取到可比较内容。
- 分析卡片每条意见 = 一段 markdown + 一行按钮（采纳/修正/忽略 + 👍有用/👎误报）；
  采纳会**新增**一条锚在原段的 PRD 评论，打分只记准确率、不写任何文档。
- 同族需求（同前缀 + 同迭代）逐条是可点蓝链，只列候选、不替人建关系。
- Story / PRD 标题 / 空间 / 状态缺哪项就省哪项，不编造。

## 4. 约束与不变量（改前必读）

**评论格式（飞书纯文本渲染，三条硬约束，都有单测钉住）**

1. **正文不写 markdown**：`**加粗**` 和 `[文字](url)` 会原样显示成星号和方括号。
   版式靠「▎【】换行缩进」。
2. **链接必须是独立的 `link` 元素**：裸 URL 不会自动变可点；`+add-comment --content`
   只支持 `text` / `mention_user` / `link`，且只渲染**一个**可点链接（优先原始讨论，其次 Story）。
   纯文本元素末尾自带换行，直接传 `\n` 的 text 元素会被 CLI 拒。
3. **第一行就是署名**：评论不显示「机器人」标记（`user` 身份署名是登录人，`bot` 身份只是应用名），
   不写清来源会被当成提需求的人自己写的。

**评审能力分层（脚本 vs Skill，2026-11-23 定界）**

| 层 | 内容 | 落点 |
|---|---|---|
| 取证工具（只读） | `read_prd_blocks`（按块读正文）/ `read_prd_sheets`（读内嵌表格）/ `grep_codebase`（本地代码检索） | `app/agents/handlers/prd_watch_handler.py` + `code_search.py` |
| 判断（Skill） | `prd-review`（总入口/门禁）→ `prd-consistency` / `prd-executability` / `prd-acceptance` / `prd-evidence` | `bot/skills/prd-*/SKILL.md` |
| 展示（脚本） | ① `analyze_story` 的机械自检卡（`analysis_card.py`）；② **`render_review_card`** 的四维评审卡（`review_card.py`） | 同一套灰底表头 + 行网格 + 逐条按钮 |

边界纪律：**工具只取证，判断不写脚本**。已按此删掉 `deep_review.py`（把评审判断写成
Python 调用模型的模块，属于用脚本约束执行面能力）。

**评审卡 vs 分析卡（2026-11-23 新增 `render_review_card`）**

只贴一篇 PRD（没有 Story）时要能出卡，而 `analyze_story` 的结论只能由 `analysis.py`
的机械检查产出，Skill 判出来的四维结论进不去。于是拆成「判断 → 渲染」两段：

- `render_review_card(items=[...])` **不做任何判断**：级别（阻断/待确认/建议）、标题、
  原文引用、建议、证据全由执行面按 Skill 判好后传入，工具只排版 + 登记；
- 级别归一只有三种（`blocker`/`待定`/`P1` 这类别名归并，归不进去降级为「建议」——
  误标阻断比漏标更伤可信度）；
- 编号 `B1/Q1/S1` 由 `order_items()` 统一产出，**卡片与登记簿同源**（两边各算一次
  就会出现卡片写 B2、采纳写进 B3 的评论）；
- 登记条目 id 前缀 `RV-`，与讨论变更 `CR-`、需求分析 `AN-` 分开统计，但走同一套
  副作用（采纳=在原文块写评论、忽略=只记录、👍👎=只记准确率），`is_analysis_id()`
  同时认 `AN-`/`RV-`；
- `quote` 必须抄 PRD 原文：工具用它把评论锚到原文块，锚不上则降级成整篇评论
  （回执会说「原文锚定 N/M 条」）。
- **单卡有两条硬上限**（实测飞书 `230099 / ErrCode 11310 element exceeds the limit`，
  整卡拒收、群里什么都不留）：① 顶层元素数（实测 22 仍发得出去，超了被拒）；
  ② 卡片 JSON 体积（约 28KB 封顶，长结论的「问题/建议」能上千字，只数条目会在
  这类卡上翻车）。所以切页按**真实渲染结果逐条试放**（`_fits_one_card`）：
  `_PER_CARD=6` 是保守下限，短结论实测 8 条一张就够；9 条起才切页，标题带
  「第 i/n 页」（只有一张时不写页码——`第 1/1 页` 反而像漏发了后面的）。
  按钮用 `compact=True` 去掉低频的「修正」。**HTTP 出口不再吞异常**：
  `_send_card_http` 失败必抛，工具回执才能如实区分「发了 N 张」与「一张都没
  发出去」——之前它 `return ""`，回执写「已发卡片」而群里空无一物。
- **每条结论的左列顺序固定「编号 → 枚举词 → 定位原文」**，宽度 96px：编号压在
  枚举词与链接下面时（64px 会把三者拆成三行）扫读找不到它，用户看到的是
  「序号跑到卡片最下方，像不属于这一条」。锚不上原文时**不给链接、不留空行**
  （不猜块）；锚定走三级兜底 `quote → 关键词共现`（`locate_by_terms`），
  只试逐字 quote 会让模型综合表述的结论整条没有「定位原文」。
- **「问题/建议」不截断**：那是给产品拍板看的论证（哪条规则和哪条冲突、后果是
  什么），截到 120 字会砍成半句话。字段上限按「够读完一条论证」取（问题 320 /
  建议 300 / 原文 200），靠 markdown 自然换行，不靠省略号省空间。

锚点为此补了两处归一化（`anchor.normalize`）：解 HTML 实体（DocxXML 里 `>` 是
`&gt;`，不解就永远不相等）与去行内代码反引号；另加「中段/半段软锚」，用于跨块截断的引用。

`code_search.py` 的四个坑（都已修，别再踩）：

- **每词上限 + 总预算**：`per_term` 默认 2、整次调用 `budget_sec` 默认 90s。早先 6 个词
  不限总时长在 hub-platform 上跑 2 分多钟被调用方判超时，执行面只看到「检索失败」；
- **按词轮转取结果**：热门词（`ExampleCorp` 本仓几乎每个文件都有）会吃光配额，
  别的词被挤成 0 条——现在第 1 轮各取第 1 条，再取第 2 条；
- **多仓根**：一次需求横跨中台（hub-platform）/ RWA 引擎（rwa-engine）/ 财富侧（wealth-service）
  几个仓，只搜一个根得到的是「检索成功但未命中」——那不是代码里没有，是搜错了地方。
  默认探测本仓同级候选集（`_DEFAULT_CANDIDATES`），可用 `PRD_REVIEW_CODE_ROOTS`
  （逗号分隔）或工具参数 `roots` 覆盖；命中路径带仓库名前缀，`_PER_ROOT_CAP` 保证
  大仓不垄断配额；查询顺序是「词优先、根轮转」，预算耗尽时每个根都有一份证据；
- 两个 Windows 坑（grep 参数与路径切分）见下：
- Git/MSYS 的 `grep` 同时给多个 `--include` 时会把后面的 `-F` 与检索词一并吞掉
  （报 `grep: <term>: No such file or directory`，rc=2）→ 改为只传 `-F`，后缀与目录
  过滤在 Python 侧做；
- `grep -rn` 输出按第一个冒号切会把 `D:\…` 切成 `D`（命中 19 条、过滤后 0 条）→
  用锚定正则 `^(.+?):(\d+):(.*)$`。

**台账（2026-11-23 起改为本机 SQLite）**

- 三张表落在 `logs/prd_watch/ledger.db`（`[projects.prd_watch].local_ledger`，默认开）：
  `binding`（Story↔群+PRD token）、`cr`（变更建议状态机）、
  `story` / `meeting` / `change`（需求知识地图）。**base 坐标即使配着也不会被读**。
- 为什么不是多维表格：这些是 bot 的运行状态，不是给人协同编辑的表格；本地零配置、
  零凭证、不走网络。代价是换机要搬文件，人工查看走 `ledger_dump` 工具。
- 适配层在 `local_adapters.py`：`LocalCrAdapter` / `LocalKnowledgeAdapter` 把同步
  SQLite 对齐成 base 版的 async 接口，**只做接口对齐，不放业务判断**。
- 手工入口：`bind_prd_story`（登记一条 Story 并解析 PRD token）、
  `group_prd_stories`（多条 Story 归成版本组，成员互指「相关需求」）、
  `ledger_dump`（只读导出，未知表名拒绝）。

**启动与门禁（2026-11-23 修订）**

- `enabled=true` 但**没有任何监听来源**（watch_chats / minutes_story_map / meeting_auto 全空）时，
  `PrdWatchService.start()` 起一个**空转守卫**（`_idle_guard`）：只保持 poll 任务存活，
  每 ≤300s 检查一次，`active` 一旦为真就切进真正的监听循环。
- `main._build_prd_watch` 注入 watcher 的判据是 **`enabled`**，不再是 `active`——
  手动入口不需要群绑定，「不配群」不该把 `analyze_story` 等工具一起关掉。
  群旁路仍由 `_process_batch` 里的 `_prd_watch.active` 单独把关（`active` 语义没变）。
- 因此 `snapshot()["enabled"]` 与「群监听是否在跑」是两件事；启动摘要分别打印。

**三种边界（评论正文）**

| 情况 | 「当前PRD原文」 | 「建议调整」 |
|---|---|---|
| 定位不到段落（新增条款） | （未在PRD中定位到对应段落，此为建议新增） | 完整新增条款表述 |
| 建议删除整段 | 逐字引用要删原文 | 【建议删除本段】 |
| 置信度中间带 | 正常引用 | 正常建议 + ⚠️ 低置信度提示 |

**群卡片（CardKit 2.0 dict，单测钉住形态）**

- 白底加粗 header（不传 template，避免彩色告警条），标题带 `（N 条 · M 个 Story）`。
- 每条变更 = **两行 + 一行按钮**：行1 Story（可点 `#ID` + 状态）│ PRD（文档标题）│ 变更项
  （标题/类型/章节/CR）；行2 变更前 │ 变更后左右对照；行3 该条的 [✅采纳][✏️修正][🚫忽略]。
- 整表是 **column_set 行网格**，不是原生 `table`——table 单元格放不了按钮
  （`data_type=button` 与单元格塞数组都实测 HTTP 400）。
- 列宽用**百分比**：像素值（`width: 120`）被飞书判 HTTP 400，`"20%"` 能过。
- 卡片撑满会话宽度（`width_mode="fill"`）：默认宽度下多列表每列只剩省略号。
- 行数上限 10，多出的写「另有 N 条见 PRD 评论」；**不分页**。
- 「定位原文」链接 = `{文档 URL}#{block_id}`；**没有 block_id 就不给链接**，
  域名前缀 `minutes_base_url` / `notes_base_url` 留空也不给——不猜租户域名。
- @人用 `<at id=…>` 真推送；同人多角色去重。

**决策闭环**

- 两个入口（卡片按钮 / 评论回复轮询）写同一份状态；卡片路径下「修正」是**新写一条评论**
  （飞书不能编辑评论，覆盖会丢掉「为什么改」），原评论回复 🔄 后 resolve。
- 状态先落、再回写评论：评论回写失败不能让状态停在 pending，否则下轮重复处理。
- 状态落 **CR 表**（`cr_base_token` / `cr_table`，本能力自建）；旧台账（绑定表+变更记录）
  作为灰度期兜底同时写。CR 表未配 → 用内存替身，状态面板明说「重启即丢」。
- 低置信度识别必须标注 AI 自动识别，不得冒充定论。

**来源通道**

- 会议素材优先级：**智能纪要 docx > 妙记摘要/待办**。实测他人妙记 `+detail` 回
  `No read permission`，而会议自动生成的纪要 docx 两种身份都能读。
- 妙记摘要/待办在 `data.minutes[0].artifacts.{summary,todos}`（不是顶层）。
- 会议搜索 `--query` 与 `--owner-ids` 是**与**关系：只给 owner 会返回空列表。
- `meeting_auto` 自动扫描的归属判据：需求名关键词必须**唯一命中一条 Story**，
  多候选时列候选并跳过（不猜）。
- UI 对账第一期**只做文本层**（Figma TEXT.characters + 图层/组件名 + 疑似字段）；
  颜色/间距/视觉层级不做，提示词里明确禁止假装看过图。

**需求自检的检查项（`analysis.py`，全部零判断、可指回块 id）**

| 检查 | 判据 | 误报防线 |
|---|---|---|
| 数量 | 「共 N 个」与实际枚举项数不符 | 只统计 ≤8 字、无句号的项 |
| 集合 | 支持链 vs 地址校验链差集 | 两处都有才比，按链别名归一 |
| 删除线 | `<del>` / `~~` 残留段落 | 整块汇总成一条，不逐块刷屏 |
| 术语 | 同义异名并存（`_TERM_SYNONYMS` 白名单） | 只收实测踩过的对 |
| 状态覆盖 | 列表页状态集 ⊆ 状态流转表目标状态 | 只看表内，不看全文 |
| 放行词 | 「无限制/一律/全部放行」等绝对表述 | 只提示要写边界，不判对错（0.7） |
| 一致性 | 同维度枚举两处写法不同 | **必须共享维度词**（状态/枚举/类型/方式/渠道），否则链 vs 币会误报 |
| 结构 | 占位符残留 / 空章节 | 空章节只判叶子标题（按数字级别，不按字符串比） |

打分的意义：误报率按 `kind` 统计（`AnalysisFeedback.summary_line`），哪条规则误报最多
就先收窄它——这是把「人工反馈」变成**规则迭代输入**，而不是只做满意度收集。

**「不改」清单**

- 不写 PRD 正文（只有评论）；正文更新是人的动作。
- 不猜需求绑定：群 ↔ Story 必须由管理员在 `story_chat_map` 显式配置；
  Meegle 没有「需求对应哪个群」，靠群名模糊匹配 = 可能把建议写到别人的 PRD 上。
- 不自动发现妙记 ↔ Story（`minutes_story_map` 也显式配）：靠会议标题猜需求是禁止的猜测行为。
- 知识台账里**不写决策状态机**（那是 CR 表的事）；CR 表里**不写时序叙事**（那是台账的事）。

**与 PM 的边界（`app/domains/pm/`）**

| 轴 | easyprd | pm |
|---|---|---|
| 产出物 | PRD 评论 + CR 状态机 | 个人交付事项/日计划/多维表格 |
| 判定 | 置信分带，问「哪篇 PRD 哪一段」 | `pm_scope` 业务线 + 三档分流，问「归不归他」 |
| 绑定 | Story↔群↔PRD 显式映射 | 负责人名下全部会 |
| 身份 | 机器级 lark-cli（user 读 / bot 写评论） | 每人独立 `~/.lark-cli/pm/<open_id>/` |
| 会议素材 | 会上**结论 → 需求变更** | 会上**待办 → 他的交付事项** |

共享的只有「怎么调 lark-cli 读会议」这层技术细节（`prd_watch/meeting.py`）；
业务判定一条都不共用——复用 `pm_meeting_sensor` 会把 `pm_scope` 耦合进来，两套边界迟早打架。

## 5. 单源与派生物

- 单源：`app/domains/prd_watch/`（watcher / extract / anchor / render / decisions / ledger /
  meegle / meeting / minutes / kb / cr_store / ui / analysis / analysis_card /
  review_card / analysis_feedback / service）+ `app/protocols/lark_base.py`
  （base 域最小读写层，与知识台账共用）+ `config.toml [projects.prd_watch]`
- 提示词单源：`bot/prompts/agents/prd-watch-extract.md`（讨论→变更）、
  `bot/prompts/agents/prd-watch-ui-diff.md`（PRD↔UI→差异）、
  `bot/prompts/agents/prd-watch-agent.md`（角色与入口口径）
- 派生物：PRD 文档评论（写出去就收不回）、群卡片、CR 表行、多维表格台账行

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-11-02 | 初版设计评审：只写评论不改正文；决策闭环双入口；两段能力（旁路 + 主动入口） | git 历史 `docs/design/20261102-easyprd-change-watch.md` |
| 2026-11-03 | 卡片改 CardKit 2.0 三段式（对齐飞书项目机器人观感），纯文本卡片删除 | `render.build_notice_card` |
| 2026-11-04 | 一条变更占两行 + 一行按钮：先要「操作跟随每行」，再要「行太长，操作换行」；table 换 column_set 网格 | `render._grid_row` |
| 2026-11-16 | 解开启动门禁（`active` 不再要求 watch_chats 非空，会议来源也能独立跑）；新增会议整理 / 知识地图 / UI 对账；CR 状态机独立成表，与知识台账时序归档分离；单测迁入 `tests/` 镜像 | `service.active`、`cr_store.py`、`kb.py`、`ui.py`、`meeting.py` |
| 2026-11-17 | 逐条打分：采纳与评分分成两条信号（采纳=写评论有副作用，👍/👎=准确率样本）；分析意见走 `AN-` 前缀与讨论变更 `CR-` 分流副作用；登记簿落 `logs/prd_watch/` 让重启后老卡片仍可点；检查项补到 8 类（放行词/一致性/结构），一致性按维度词收窄以消掉「链 vs 币」误报 | `analysis_feedback.py`、`card_actions._handle_analysis`、`analysis.structure_findings` |
| 2026-11-17 | 需求分析链路：`analysis.py` 机械自检（数量/集合/删除线/术语/状态覆盖）+ 设计稿三级取数（Story 字段 → 设计工单 → 明说没有）+ 同族按名称前缀只列候选；`analysis_card.py` 紧凑卡片（每条一段 markdown + 一行按钮，`flex_mode=flow`；`action` 标签在 2.0 不支持）；同族「一起看/暂不」按钮只回话不写关系；Figma token 支持从 mcp-gateway 读取并把「过期」与「没配」分开报 | `analysis.py`、`analysis_card.py`、`meegle.DESIGN_FIELDS`、`ui.resolve_token` |
| 2026-11-23 | 约定改成「在哪聊就在哪答、讨论可转发」，不再配授权群 → `enabled=true` 后手动入口必须可用：`start()` 增空转守卫、`_build_prd_watch` 的 watcher 注入判据从 `active` 改 `enabled`（群旁路仍看 `active`） | `service._idle_guard`、`main._build_prd_watch`、启动摘要 |
| 2026-11-23 | 评审分层定界：判断下沉 5 个 Skill，脚本只留取证与展示（删 `deep_review.py`）。用户反馈「只贴 PRD 没有 Story 也得出卡」→ 新增 `render_review_card`：纯渲染 + 登记，判断由执行面给，级别三档、编号 `B1/Q1/S1` 与登记簿同源、`RV-` 复用采纳/忽略/评分副作用；同时修锚点三处归一化（HTML 实体 / 反引号 / 中段软锚）——实测「待受理-> 待成交」锚空就是 `&gt;` 没解 | `review_card.py`、`prd_watch_handler._render_review_card`、`anchor.normalize`、`bot/skills/prd-review/SKILL.md` |
| 2026-11-23 | 8 条结论的评审卡「回执说已发、群里有卡才 1 条」：单卡元素数超限被飞书整卡拒收，而 HTTP 出口把 400 吞成空串。改为按 6 条切页 + `compact` 按钮 + 出口失败必抛；顺带把评分按钮从 👍/👎 换成 🙂/🙃（群聊里拇指像给同事点赞，而这里问的是判读准不准） | `review_card._PER_CARD`/`build_review_cards`、`capabilities_server._send_card_http`、`analysis_card.FEEDBACK_UP` |
| 2026-11-24 | 评审卡三处可读性问题：① 编号被挤到左列最后一行（64px 放下「枚举词+序号+定位原文」要三行）→ 左列改 96px、顺序固定「编号→枚举词→定位原文」；② 部分结论没有「定位原文」→ 锚定补 `locate_by_terms` 关键词共现兜底（与讨论链路同源）；③ 「问题」被截到 120 字成了半句话 → 字段上限按「够读完一条论证」放宽（问题 320 / 建议 300 / 原文 200），靠换行不靠省略号。切页同时从「固定 6 条」改成按真实渲染体积/元素数逐条试放 | `review_card.py`、`analysis_card.grid_row`、`prd_watch_handler._render_review_card` |
| 2026-11-24 | 评审卡按钮点了没反应：登记簿由 MCP 子进程写、按钮回调在 main 读，`AnalysisFeedback` 只在 `__init__` 读一次，发卡之后登记的条目主进程永远查不到（旧卡点得动、新卡报「找不到登记记录」）。改为查不到时按 mtime 重载、写盘前先合并；同时把「待修正」状态从 `(chat_id, open_id)` 改成只认 `open_id`——卡片转发到别的会话后回调带回的是转发目标 chat_id，带上就永远对不上 | `analysis_feedback.py::{get,_reload_if_changed,register_items}`、`card_actions.py::_pending_fix`、`main._apply_prd_fix` |
| 2026-11-24 | 转发来的卡片点不动（飞书提示「这是转发消息，无法直接操作」）：客户端语义改不了，改为把**编号变成指令**——卡片脚注教 `采纳 B1` 这类写法，`handle_command` 走与按钮同一套副作用；解析只认极短一句（否则群里正常讨论会被当成指令去写 PRD 评论），编号歧义时列候选不猜 | `card_actions.{parse_plain_command,handle_command}`、`review_card.plain_commands`、`main._apply_prd_command` |

## 7. 已知坑

- `comment_identity` 默认 `bot`：实测 user 身份缺 comment scope。切 user 需先补授权
  （`missing_scopes` 报错时走 `lark-cli auth login`）。
- 妙记生成事件是 user 身份，bot 长连接收不到 → 用 `minutes_poll_sec` 轮询，不是事件驱动。
- 群非@消息监听是隐私敏感动作：只有 `watch_chats` 里的群放开，其余群准入行为一字不变。
- 灰度顺序：默认 `enabled=false + dry_run=true`，先把识别误报率看清楚再开真写评论。
- **启动门禁陷阱**：`active` 现在三种来源任一成立即为真；只想跑会议时不必先配群，
  但 `flush_prd_watch` 仍只对 `watch_chats` 里的群生效（它读的是群缓冲）。
- **CR 表需要人工建表**：`cr_base_token` + `cr_table` 指向一张自建表
  （列名见 `cr_store.COLUMNS`）。未建表时评论照写，但决策状态只在内存里。
- **Figma token 会过期**：REST 返回 `401 Token has expired` 时 `ui.load_frames` 明确报
  「已过期」并给出换 token 的路径（与「没配」分开，否则排查方向是错的）。
  token 查找顺序：`FIGMA_API_KEY` 环境变量 → `~/.claude/mcp-gateway/backends.json`
  的 figma backend env → 空。**bot 是独立进程，不继承 Claude Code 的 MCP env**，
  只认 `.env` 就会出现「明明配过却报未配置」（实测踩过）。
- **figma MCP 已接入 dsh 会话**（`[projects.mcp.figma]`，官方 `figma-developer-mcp`
  `--stdio`，工具 `get_figma_data` / `download_figma_images`）。凭据只走 env
  （`FIGMA_API_KEY = "${FIGMA_API_KEY}"`，supervisor 展开）；**不要写进 args**——
  命令行会出现在进程列表与日志里。command 必须是 node 绝对路径（npm 包只有 `.cmd` 壳）。
  注意：**MCP 走的是同一把 token 的 `/v1/files/{key}/nodes`，配额与 REST 共享**
  （实测 MCP 调用同样 429），所以它解决的是「工具面/粒度」而不是「配额」。
- **Figma 组织账号的 `/v1/files` 配额很低**（`X-Figma-Plan-Tier: org` /
  `X-Figma-Rate-Limit-Type: low`）：`/v1/me`、`/v1/files/{key}/meta` 正常时，
  `/v1/files` 仍可能 429，且 `Retry-After` 实测 9000+ 秒（≈2.5 小时）。
  所以工具**不自动重试**：把 429 与「token 过期」分开报，并把等待时长写进回执，
  否则使用者会反复重试、把配额继续打满。
- **分析意见登记簿在 `logs/prd_watch/`**：卡片按钮回调只带回 `AN-<StoryID>-<序号>`，
  正文/块 id 全靠登记簿。`logs/` 被 gitignore，换机器/清日志后老卡片点采纳会回
  「找不到登记记录」——这是如实报错，不是 bug；重新让我分析一次即可。
- **登记簿是两个进程共写的文件**：评审卡由 dsh 的 MCP 子进程登记（`render_review_card`），
  按钮回调落在 main。所以 `AnalysisFeedback.get()` 查不到时**按 mtime 重载一次**
  （`_reload_if_changed`，只合并不覆盖），`register_items` 写盘前同样先合并。
  只在 `__init__` 读一次会让「发卡之后才登记的条目」在主进程里永远查不到——
  表现是旧卡片点得动、新卡片报「找不到登记记录」。
- **转发来的卡片按钮点不动**（飞书把转发消息当只读，点了只弹「这是转发消息，无法直接操作」）——
  这是客户端语义，应用侧改不了。退路是**文字指令**：卡片脚注写明编号，人回一句
  `采纳 B1` / `忽略 B1` / `有用 B1` / `判错 B1`（也认 `B1 采纳` 与英文词），
  走的是与按钮完全相同的副作用（`CardActionHandler.handle_command`）。
  解析只认极短一句（`parse_plain_command`），**判宽了会把群里正常讨论当成指令**
  去写 PRD 评论；编号在同多篇文档里重复时不猜，列出候选让人补全。
- **待修正状态按人（open_id）记，不带会话**：卡片可以被转发到别的群/私聊，
  转发后回调带回的是**转发目标**会话 id；按 `(chat_id, open_id)` 存会让
  「点修正 → 回一句新表述」永远匹配不上。
- **序号必须同源**：卡片显示的第几条与登记簿的 `AN-…-0N` 由
  `analysis_card.ordered_findings` 统一产出。两边各算一次会漂移，点 3 号写出来是 4 号。
- **知识台账表 id**：默认值来自 examplecorp 空间的真实表（需求台账/会议纪要/变更记录），
  换空间必须在配置里覆盖三张表 id，否则会读到空表并报「没有需求记录」。
