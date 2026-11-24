# easyprd —— PRD 变更助手 设计说明

> 文档 ID：`20261102-easyprd-change-watch`
> 创建人：@devuser
> 创建日期：2026-11-02
> 关联项目/模块：domains（新增 `app/domains/prd_watch/`）+ gateway（群消息旁路）
> 关联仓库：feishu-agent-platform
> 前置文档：temp 需求草稿两份（本设计稿吸收后删除，不再作为依据）
> 状态：**已实现，灰度未开**（默认 `enabled=false + dry_run=true`；§9.3 待确认项仍未签收——
> 其中 Q2 已有实测结论待拍板，Q1/Q5 卡在用户与管理员侧）


---

## 一、背景与目标

### 1.1 需求背景

飞书 Story 挂一篇文档作 PRD。PRD 的实际变更发生在群里讨论和评审会上，散落在聊天记录与
妙记里，三件事长期缺失：变更点没有结构化沉淀（谁在什么时候为什么改什么）、PRD 原文与
建议变更没有对照、产品拍板后没有写回与通知闭环。

easyprd 做成**常驻飞书的工作伙伴**：监听 Story 群讨论与会议妙记 → 识别需求变更 → 以
**文档评论**形式把 diff 挂在 PRD 对应段落 → 群里发卡片@参与人 → 产品回复采纳/忽略/调整 →
落台账并再次通知。

与对话式助手（喊一句才动一次）的差别是本需求的核心：**事件驱动、以应用身份写飞书资源**。

### 1.2 需求来源

| 字段 | 值 |
|------|-----|
| 需求类型 | 新功能 |
| 优先级 | P1 |
| 目标上线日期 | 待定（依赖权限审批，见 §6） |

### 1.3 关联背景资料

| 文档 | 路径 | 要点 |
|------|------|------|
| 需求台账表结构 | 飞书多维表格（三表，token 待补，见 §9.3-Q1） | Story↔PRD↔群↔变更记录的唯一真源 |
| 飞书平台配置 | `docs/feishu-platform-setup.md` | 现有 app 已申请权限清单 |

### 1.4 影响范围

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `app/domains/prd_watch/` | 新增 | 变更识别、diff 生成、台账读写、状态机 |
| `app/gateway/channel/__init__.py` | 修改 | 按群覆盖 `require_mention`（现状全局 True，见 §2.2） |
| `app/main.py` | 修改 | 非@群消息旁路进 prd_watch，不进编排器 |
| `app/agents/handlers/lark_cli.py` | 修改 | 补评论读写三方法（现仅正文读 + wiki 建节点 + 覆盖写） |
| `bot/agents/prd-watch-agent/` + handler + prompt | 新增 | 手动触发入口（「总结今天这个群的讨论」） |
| `config.toml` | 新增 `[projects.prd_watch]` | 开关、白名单群、台账 token、模型参数 |
| 提示词 | 新增 `bot/prompts/agents/prd-watch-agent.md` | 变更识别判据与评论模板 |
| 既有编排链路 | 无变更 | 不改 router/gather，旁路独立于路由 |

---

## 二、现状分析

### 2.1 现有可复用资产（实测于 lark-cli 1.0.97 + lark_channel SDK）

**结论：草稿里「需要新建一套 API 客户端」的判断不成立，绝大部分能力已在手。**

| 能力 | 现成程度 | 依据 |
|------|---------|------|
| 群消息接收 | ✅ 已在跑，**只差策略开关** | `im.message.receive_v1` 已订阅；`PolicyConfig(require_mention=True)` 把非@消息拦在入口，不是缺权限 |
| 按群放开全量消息 | ✅ SDK 原生支持 | `GroupOverride.require_mention: Optional[bool]`，逐 chat_id 覆盖 |
| 文档评论（写/回复/列表/resolve） | ✅ lark-cli 已有 shortcut | `drive +add-comment` / `+add-reply` / `+list-comments` / `+list-replies` / `+resolve-comment` |
| 评论锚定到段落 | ✅ 需换 fetch 参数 | `drive +add-comment --block-id`；块 ID 由 `docs +fetch --detail with-ids` 给出（现 `get_doc_content` 用 markdown，拿不到 ID） |
| 群内@人 | ✅ | `im +chat-members-list` 取 open_id；`BotChannel.send/send_card` 已在用 |
| 台账读写 | ✅ lark-cli `base` 域 | 表/字段/记录 CRUD 全套 |
| 妙记正文/逐字稿 | ✅ | `note +detail` / `note +transcript`、`minutes` 域 |
| 文档正文读 | ✅ | `LarkCli.get_doc_content()` |
| 评论事件回调 | ⚠️ SDK 有 `Events.COMMENT` + `BotChannel.on_comment()`，**项目内无人注册**，且需 app 侧订阅「评论和回复」事件 |
| 妙记生成事件 | ⚠️ `lark-cli event list` 显示 `minutes.minute.generated_v1` 为 **AUTH=user**，而 feishu-agent-platform 长连接是 bot 身份 → 拿不到，需降级为定时轮询 |

### 2.2 相关代码现状

| 关注点 | 现状 | 位置 |
|--------|------|------|
| 消息准入 | `dm_policy=open` / `group_policy=open` / `require_mention=True` 全局固定，无按群覆盖 | `app/gateway/channel/__init__.py:138-143` |
| 事件注册 | 只注册了 `on_message` / `on_card_action` / `on_error`；`on_comment` 已封装未接线 | `app/main.py:1269-1274` |
| 飞书身份 | 文档读写走 lark-cli `--as user`（个人授权），群消息与发消息走 bot（tenant_access_token） | `app/agents/handlers/lark_cli.py` |
| 长连接归属 | **同一 app 多条飞书长连接是竞争消费**，feishu-agent-platform 独占，easyprd 不得另起进程 | CLAUDE.md / `config.toml [projects.cc_connect]` 注释 |

### 2.3 已知坑点

- **双连接抢消息**：另起 easyprd 独立进程连同一 `app_id`，会让 feishu-agent-platform 漏消息（2026-09-14 实测 /help 串台、控制台断流）。必须寄生在同一条连接上。
- **user 身份写权限收口**：`lark_cli` 的写目标一律从配置取，不接受用户传入 token（`LarkCli` 类注释已立此规矩）。easyprd 的 PRD token 来自台账，同样不得由消息内容决定。
- **markdown 无块 ID**：`get_doc_content` 现为 markdown 输出，做不了段落锚点。定位段落须改 `--doc-format xml --detail with-ids`，返回结构不同，需单独封装而非复用。

---

## 三、目标设计

### 3.1 架构方案

```mermaid
flowchart TD
    FS[飞书 WS 长连接<br/>feishu-agent-platform 独占] --> POL{群白名单?}
    POL -->|否| AGG[MessageAggregator<br/>debounce] --> ORCH[CrewOrchestrator<br/>现有链路不动]
    POL -->|是，非@消息| PW[prd_watch 旁路]
    POL -->|是，@机器人| AGG
    PW --> ID[变更识别 LLM<br/>信号词+置信度]
    ID -->|置信度低| DROP[丢弃并计数]
    ID -->|达标| LOC[PRD 段落定位<br/>docs fetch with-ids]
    LOC --> DIFF[diff 生成<br/>原文/建议/依据]
    DIFF --> CMT[写 PRD 评论<br/>drive add-comment --block-id]
    CMT --> CARD[群通知卡片 @参与人]
    CARD --> LED[台账：变更记录 pending]
    CMT -.评论回复事件/轮询. --> DEC{采纳/忽略/调整}
    DEC --> LED
```

妙记侧同一条识别管道，只是素材来源从群消息换成 `note +transcript`。

### 3.2 组件设计

| 组件 | 职责 | 新增/变更 |
|------|------|-----------|
| `prd_watch/watcher.py` | 群消息旁路入口：白名单判定、攒窗口、触发识别 | 新增 |
| `prd_watch/extract.py` | 变更点提取 + 置信度门禁 | 新增 |
| `prd_watch/anchor.py` | PRD 段落定位（块 ID）与原文逐字引用 | 新增 |
| `prd_watch/commenter.py` | 评论写入/回复监听/状态回写 | 新增 |
| `prd_watch/ledger.py` | 台账三表读写（CR 编号、决策状态） | 新增 |
| `prd_watch/prompt` | 识别判据与评论模板（版本化） | 新增 `bot/prompts/agents/prd-watch-agent.md` |
| `lark_cli` 补方法 | `add_comment` / `list_comment_replies` / `resolve_comment` | 变更 |
| `channel` 按群覆盖 | `GroupOverride(require_mention=False)` 从配置注入 | 变更 |
| `prd-watch-agent` | 手动触发：`[use prd_watch]` 出当日汇总 / 重跑某段讨论 | 新增 |

### 3.3 关键设计决策

| 决策点 | 选择 | 理由 | 代价 | 备选 |
|--------|------|------|------|------|
| 常驻形态 | 寄生在 feishu-agent-platform 现有长连接做旁路 | 双连接抢消息（§2.2）；零新增进程，权限/观测/重启全复用 | prd_watch 挂了就没人监听，无法独立发布 | 独立服务 + 新 app_id（多套凭据、多份审批，否） |
| 走 Agent 还是旁路 | 旁路为主 + 一个手动 Agent 入口 | 变更识别是「消息流上主动判断」，路由器按意图分派无法覆盖不@人的消息 | 主链路多一条分叉，须在入口就分流干净 | 纯 Agent（需每次人工触发，违背「常驻」目标，否） |
| 写 PRD 正文 | 本期只写评论，正文更新走人工 | 机器人误改 PRD 是不可逆事故；评论可 resolve/关闭，风险可控 | 产品采纳后仍须人手贴进正文 | 直接改正文（§9.3-Q3 签收后再评估） |
| 段落定位失败 | 降级为整篇评论（`--full-comment`）+ 标注「未定位到段落」 | 新增条款本就没有原文；静默丢弃等于漏变更 | 评论位置不准，需产品自己挪 | 丢弃（否） |
| 评论回复感知 | 定时轮询（`poll_replies_sec`），事件订阅作第二期 | 事件需 app 侧订阅 + 未验证；轮询用现成 `+list-replies`，确定性高 | 决策生效有分钟级延迟 | 事件驱动（现 `on_comment` 无人注册，风险在未测） |
| 变更识别置信度门禁 | 低于阈值→丢弃并计数；中间带→写评论但标「⚠️ AI 自动识别，请人工确认」 | 误报会烧掉产品对工具的信心，比漏报更致命 | 阈值需实测调（Q4） | 全量都写（否，刷屏） |
| 群↔Story 绑定 | 管理员在配置/台账显式维护，禁止猜测 | 绑错就写错 PRD，是数据污染而非体验问题 | 上手要配一次 | LLM 自动猜群（否） |

### 3.4 数据模型（契约，落点在台账与代码，此处只定字段语义）

CR 编号：`CR-{YYYYMMDD}-{StoryID}-{当日序号}`，贯穿评论 → 群卡片 → 台账，全链唯一键。

台账「变更记录」表本期须新增字段：`决策状态`（pending/采纳/忽略/调整）、`置信度`、
`评论 ID`（回复监听靠它回查）、`素材来源`（群消息 / 妙记 / 手动）。
三表既有字段沿用草稿定义，不另开一份副本 —— 表结构在多维表格侧改，文档只登记语义。

---

## 四、核心契约：评论与卡片格式

草稿已定稿，收编为本设计稿的**唯一出处**（temp 草稿随即删除，不留第二份）。

### 4.1 编号规则

`CR-{YYYYMMDD}-{StoryID}-{当日序号}`，例：`CR-20261030-8821-001`。

### 4.2 PRD 评论模板

```text
🤖 Bot EasyPRD 自动评论 · CR-{YYYYMMDD}-{StoryID}-{序号}

【变更依据】
来源：{评审会/群名/妙记标题} · {YYYY-MM-DD HH:MM}
涉及需求：{Story 标题}（#{StoryID}）

【改什么】
变更类型：{参数调整 / 新增条款 / 删除条款 / 流程变更} · 涉及模块：{…}[ · 合规影响：…]

当前PRD原文：
  {逐字引用，一字不改}

建议调整为：
  {修改后的完整表述}

【凭什么】
  「{讨论原话}」
  ——{说话人}，{场景}
  {一句话业务理由}

【谁来定】
  待产品确认：回复「采纳」/「忽略」/「调整：说明」，或在群卡片上直接点按钮
  本条由 Bot EasyPRD 根据讨论自动生成，未经人工确认，不作为定论；正文不会被自动修改。

原始讨论：{链接}
需求：{Story 链接}
```

**两条硬约束**（都来自实测）：

1. **正文不写 markdown**。飞书评论是纯文本渲染，`**加粗**` 与 `[文字](url)` 会原样
   显示成星号和方括号——所以版式靠「▎【】换行缩进」而不是星号。
2. **链接必须是独立的 `link` 元素**。裸 URL 在客户端不会自动变成可点链接；
   `+add-comment` 的 `--content` 支持 `text` / `mention_user` / `link` 三种元素，
   链接作为第二个元素紧随正文（正文末尾自带一个换行，纯 `
` 的 text 元素会被 CLI 拒）。
   只渲染**一个**可点链接：优先原始讨论，其次 Story——给两条 URL 等于让读者自己挑。

3. **第一行就是署名**。评论不显示"机器人"标记（`user` 身份下署名是登录人，
   `bot` 身份下只是应用名），不写清来源会被当成提需求的人自己写的。

### 4.3 三种边界

| 情况 | 「当前PRD原文」 | 「建议调整」 |
|------|----------------|-------------|
| 定位不到段落（新增条款） | （未在PRD中定位到对应段落，此为建议新增） | 完整新增条款表述 |
| 建议删除整段 | > 逐字引用要删原文 | 【建议删除本段】 |
| 置信度落在中间带 | 正常引用 | 正常建议 + 末尾「⚠️ 此条由 AI 自动识别，置信度较低，请人工确认是否为正式变更」 |

### 4.4 群通知卡片

**形态 = 飞书交互卡片（CardKit 2.0 dict），三段式**（2026-11-03 用户两次指定：先对齐飞书
项目机器人观感，再要求「① 变更依据带链接 ② Story/PRD/diff 表格 ③ 待确认人」）。
实现 `render.build_notice_card` + `CardItem`（行级 Story 归属）；纯文本卡片已删除。

```text
┌ 📋 PRD 变更建议提醒（6 条 · 3 个 Story）        ← 白底加粗（不传 template，避免彩色告警条）
│ **变更依据：** 会议（智能纪要）· WebPortal RWA接口对齐（[查看原始讨论](url)）
│ 共 6 条建议，涉及 3 篇 PRD，已写入各文档评论待确认。
│ ══ 表头（灰底）：Story │ PRD │ 变更项 ══
│ 每条变更 = 两行 + 一行按钮：
│   [#7130…] │ [PRD文档标题] │ **标题** `流程变更·合规`
│   1-排期中 │              │ 章节：资质校验…（[定位原文](URL#block)）
│                              CR-20261104-…
│   ─────────────────────────────────────────
│   变更前                    │ 变更后
│   客户不在白名单内列表不展示 │ 仅按渠道激活状态控制可见性
│   ─────────────────────────────────────────
│   [✅ 采纳] [✏️ 修正] [🚫 忽略]     ← 独占一行，靠左
│ ─────────────────────────────────────────────
│ （下一条重复上面三行结构）
│ **待确认人：** @张三 @丁浩 @胡萍              ← <at id=…> 真推送；同人多角色去重
└
```

约束（都有单测钉住）：
- **卡片撑满会话宽度**（`config.width_mode="fill"`）：默认宽度下五列表每列只剩省略号。
- **卡片可点回原讨论**：群讨论 → applink 会话深链；会议 → 妙记/纪要链接
  （`minutes_base_url` / `notes_base_url` 拼，**留空就不给链接**，不猜租户域名）。
- **Story 列只放可点 `#ID` + 状态**（用户反馈"Story 列小一点"）：三列都写标题时全被截成
  「【Weportal】RWA产品货…」，信息量低于一个能点的 ID；标题归 PRD 列，用**文档标题**。
- **diff 用左右两列**（变更前 / 变更后），与 IDE 等常见 diff 工具同构（用户 2026-11-03 指定）。
  变更前列 = PRD 原文（新增条款明说"无对应原文"），变更后列 = 建议表述（删除条款写
  「【建议删除本段】」）；各 120 字，完整表述在 PRD 评论里。
- **变更项列写明原文所在章节**（最近的 h1~h6 两级，`BlockAnchor.section_of`），并给
  「定位原文」链接 `{文档 URL}#{block_id}`（lark-cli `lark-doc` skill 的 block 直达格式；
  **没有 block_id 就不给链接**）。变更前列的原文本身也指向同一块。
- **一条变更占两行 + 一行按钮**（用户 2026-11-04 两次反馈：先要「操作跟随每行」，
  再说「行太长了，操作换行」）：整表是 **column_set 行网格**而不是原生 `table`
  （table 单元格放不了按钮，`data_type=button` 与单元格塞数组都实测 HTTP 400）。
  - 行1：Story（可点 #ID + 状态，一行写完）│ PRD（文档标题）│ 变更项（标题/类型/章节/CR）
  - 行2：变更前 │ 变更后（各带灰色标签，**左右对照**，每列宽度权重 1:1）
  - 行3：该条的 [✅ 采纳][✏️ 修正][🚫 忽略]，独占一行
  - 6 列挤一行时中文每列只剩 6~8 字、被截成省略号；分行后每列能放 2~3 倍文字。
  - 修正不直接写：回复引导用户给新表述再落评论；忽略带二次确认（会 resolve 评论）。
- **不分页**（用户指定）：行数上限按 10 设（原 table 版是 page_size 上限，网格版沿用同一
  上限免得卡片无限拉长），多出的写「另有 N 条见 PRD 评论」。
- 列宽用**百分比**：实测像素值（`width: 120`）被飞书判 HTTP 400，"20%" 能过。
- 同一讨论/同一场会**只发一张卡**（即使跨多个 Story），逐条发会刷屏。
- 空间名/类型/状态/文档标题只从 Meegle/文档取，**缺哪项就省哪项，不编**。

决策回执同形态（`build_decision_card`）：header 用 ✅/🚫/🔄/⏰ + 台账状态词，
采纳必写明「PRD 正文需人工按评论表述更新」。

### 4.5 决策闭环状态流转

**两个入口并存**：① 群卡片按钮（`card_actions.CardActionHandler`，即时）；② PRD 评论回复
（`decisions.DecisionPoller` 轮询，分钟级）。两个入口写同一份台账状态，谁先到谁生效。

| 从 → 到 | 触发 | 后置动作 |
|---------|------|---------|
| （无）→ pending | 评论写入成功 | 群卡片 + 台账建行 |
| pending → 已采纳 | 卡片点「✅ 采纳」／评论回复「采纳」 | 评论回复 ✅ 已采纳；群通知须写明「**正文需人工按评论表述更新**」（我不代改，写「已生效」会让研发以为文档已变）；台账落决策 |
| pending → 已忽略 | 卡片点「🚫 忽略」／回复「忽略」 | 评论标记 ❌ 并 resolve；归档 |
| pending → 已调整 | 卡片点「✏️ 修正」并回一条新表述／回复「调整：…」 | 卡片路径**新写一条评论**（原评论保留，回复 🔄 标记后 resolve；飞书不能编辑评论，覆盖会丢掉"为什么改"）；评论路径只标 🔄。正文由产品自改，bot 不碰 |
| pending →（待修正文案） | 卡片点「✏️ 修正」 | **先不改状态、不写评论**，回一条「请回复修正后的表述」（30 分钟内有效，且只认**点按钮那个人**在本会话的回复——群里别人接着说话不会被当成修正文案） |
| pending → 超时未决 | 超过 `decision_ttl`（默认 3 个工作日） | 群内提醒一次，不再重复催 |

低置信度识别必须标注 AI 自动识别，不得冒充定论。

---

## 五、接口与配置

### 5.1 外部接口调用面

| 用途 | 调用 | 身份 |
|------|------|------|

| 写段落评论 | `drive +add-comment --doc {t} --type docx --block-id {b} --content -`（stdin 传 elements JSON） | `comment_identity`，默认 bot |
| 整篇降级评论 | 同上 + `--full-comment`（不带 block-id） | user |
| 回查回复 | `drive +list-replies --token {t} --type docx --comment-id {c}` | `comment_identity` |
| 关闭评论 | `drive +resolve-comment`（只吃**评论 id**；拿回复的 reply_id 调会返回 `fail`）／回写状态用 `+add-reply`（飞书无编辑评论的开放能力，回复是唯一不覆盖产品原话的写法） | `comment_identity` |
| 台账读写 | `base` 域记录 CRUD | user |
| 群成员 open_id | `im +chat-members-list` | bot |
| 群卡片 | `BotChannel.send_card`（现有） | bot |
| 妙记逐字稿 | `note +transcript` | user |

> 身份默认 **bot**：user 登录态实测缺 `docs:document.comment:create` + `:write_only`（真实调用返回
> missing_scopes），而 bot 两条请求都能构造。署名差别与切换条件见 §9.3-Q2。
> 段落定位走 `docs +fetch --scope keyword --keyword {片段} --detail with-ids`（服务端筛，
> 命中即返回带 `id` 的块）；关键词筛空则取全文块本地按归一化文本匹配。

### 5.2 配置变更（`config.toml` 新增）

键与默认值的单源是 `app/config.py::PrdWatchConfig`（`config.toml
[projects.prd_watch]` 按同名键覆盖），本表只写设计意图，不复制默认值：

| 配置键 | 设计意图 |
|--------|---------|
| `enabled` / `watch_chats` | 总开关 + 白名单群（旁路唯一依据；不在名单的群准入行为一字不变） |
| `window_sec` | 讨论攒窗口（对齐 `messaging.debounce_sec` 思路，一次讨论一张卡片） |
| `extract_min_confidence` / `extract_flag_confidence` | 置信度三分带：<min 丢弃计数；两值之间评论带 ⚠️；>flag 正常 |
| `dry_run` | 灰度第 1 步：只识别落日志（metrics 进任务档案），不写评论不发卡片 |
| `comment_identity` | 评论读写身份（bot/user，见 §9.3-Q2；两值之外回落 bot） |
| `poll_replies_sec` / `decision_ttl_days` | 回复轮询间隔 / pending 超时提醒（只催一次） |
| `ledger_base_token` / `ledger_change_table` / `ledger_bind_table` | 台账三坐标；任一为空→内存台账降级 + 启动告警 |
| `minutes_poll_sec` / `minutes_story_map` | 妙记轮询间隔 / minute_token→StoryID 显式映射（不做自动发现） |

`enabled=false` 或 `watch_chats=[]` 时旁路不注册、`require_mention` 不放开，主链路零开销。

---

## 六、安全与权限

| 能力 | scope | 现状 | 缺口 |
|------|-------|------|------|
| 群内全量消息 | `im:message.group_msg` | **`docs/feishu-platform-setup.md` 已列为✅必须** | 大概率已开通，只差代码放开 `require_mention`；须实测确认 |
| 发消息@人 | `im:message:send_as_bot` | 已开通（现有回复链路） | 无 |
| 读 PRD 正文（含块 ID） | `docx:document` | lark-cli user 身份已可用（实测读到案例库索引文档全文块） | 需确认对 PRD 所在空间可用 |
| 写文档评论 | `docs:document.comment:create` + `docs:document.comment:write_only`（实测两个都要） | 未申请（user 身份被拒；bot 走应用权限） | **提审项 1** |
| 读写台账 | `bitable:app` | 未申请 | **提审项 2** |
| 妙记摘要/待办 | `minutes:minute`；自动发现另需 `minutes:minutes.search:read`（实测缺失） | 未申请 | **提审项 3**（只做显式映射则不需要 search） |
| 通讯录 | `contact:user.base:readonly` | 已申请（推荐项） | 无 |
| 飞书项目 Meegle | 独立产品线授权 | 未开通 | Story 信息可先由台账人工登记绕过 |

- 全为**高级权限**，需创建版本 + 企业管理员审批，不是申请即用。
- 复星财富为持牌机构：**监听全群消息 + 妙记属员工通信内容采集，提审前先过内部数据合规评审**。
- 红线：不开 `docx` 编辑正文权限；不猜群↔Story 绑定；台账 token 只从配置取。
- 审计：每次评论写入、每次决策状态流转记 `logs/audit/`，含 CR 编号 + 操作人 + 结果。

---

## 七、可观测与测试

### 7.1 观测项

| 指标 | 用途 |
|------|------|
| 候选数 / 识别数 / 丢弃数 / 标⚠️数 | 调 `extract_min_confidence` 的唯一依据 |
| 段落定位成功率 | 决定 `--full-comment` 降级占比 |
| 决策分布（采纳/忽略/调整/超时） | **采纳率高 = 识别准**，这是本能力的北极星 |
| 误报申诉数 | 群里被产品否定几次，比任何内部指标都真 |

旁路任务落 TaskRecord（`final_agent=prd-watch`），控制台「报告」tab 直接可看，不新建观测栈。

### 7.2 测试用例（Given-When-Then，设计阶段先出清单）

| 用例 | Given | When | Then |
|------|-------|------|------|
| 非白名单群不受影响 | `watch_chats` 不含该群 | 群内发任意消息 | `[用户看到]` 与改动前完全一致，无旁路日志 |
| 白名单群非@消息被监听 | 群在白名单 | 成员间讨论「额度改成20万」不@bot | `[用户看到]` PRD 出现段落评论，群里收到一张卡片 |
| @机器人消息不双处理 | 群在白名单 | @bot 提问 | 走现有编排，旁路跳过 |
| 闲聊不误报 | 白名单群 | 「中午吃什么」 | `[用户看到]` 无评论、无卡片，日志计入丢弃 |
| 定位失败降级 | PRD 无对应段落 | 提出新增条款 | `[用户看到]` 整篇评论 + 「未定位到段落」标注 |
| 低置信带标注 | 讨论含糊 | 「要不再看看额度」 | `[用户看到]` 评论带 ⚠️ 人工确认 |
| **多 Story 归属不串篇** | 群里绑定 3 个 Story，一场会改 3 篇 PRD | 讨论同时涉及货架/申购/赎回 | `[用户看到]` 每条评论落在**它自己那篇**、只发 1 张卡片、表格每行只带自己那篇的 PRD 链接；模型给的 story_id 不在候选集 → 丢弃并计入 `unattributed` |
| 采纳闭环 | pending 评论 | 产品回复「采纳」 | `[用户看到]` 评论回复 ✅ 已采纳、台账转采纳、群通知写明正文待人工更新 |
| 忽略闭环 | pending 评论 | 回复「忽略」 | 评论 ❌ 并 resolve，台账归档 |
| **评论本体不算回复** | pending 评论（正文含「采纳 → 回复『采纳』」的操作说明） | 轮询读回复 | 本体被跳过；只有真回复里的决策词才生效（否则首轮会把全部 pending 判成已采纳） |
| **卡片修正两段式** | 卡片点「✏️ 修正」 | 点按者本人 30 分钟内回一条新表述 | `[用户看到]` 新评论落到同一段落、台账转「已调整」；别人回话或超时则不生效 |
| **自测台可点通** | 管理员发 `/easyprd demo <PRD链接>` | 点卡片按钮 | `[用户看到]` 走真实评论/决策链路（不依赖群↔Story 绑定与 Meegle 凭证） |
| 编号不撞 | 同日同 Story | 连续两条变更 | CR 序号递增，全链一致 |
| 关闭态零开销 | `enabled=false` | 任意消息 | 旁路未注册，主链路耗时不变 |
| 台账坐标缺失不崩 | ledger 三坐标未配 | 启动 | `[用户看到]` 启动日志 warn「变更记录只在内存，重启即丢」+ 降级内存台账，不阻断 bot；监听仍可跑（绑定由内存台账给，仅供干跑） |

门禁：`prompt_eval`（`_golden/prd-watch-agent.json` 8 例：两工具 + 三红线 + 口径）+
`router_intent_eval`（+3 例 → prd-watch-agent）+ `verify_spec` 16 项。
上表已落为 `app/domains/prd_watch/test_*.py`（渲染边界/识别分带/锚点/窗口流程/
决策闭环/旁路准入/妙记通道）；用例与测试的对应关系以测试文件为准，本表只述「什么算对」。

---

## 八、发布与回滚

### 8.1 前置检查

| 项 | 方式 |
|----|------|
| 权限版本已审批生效 | 开放平台后台看版本状态；缺 scope 时 lark-cli 返回 `missing_scopes` |
| 台账三表已建且有绑定数据 | 读一条真实 Story 记录验证 |
| **PRD 文档可读性抽查（见 §9.3 Q7）** | 对 `story_chat_map` 里每个 Story 跑一遍「链接形态 → obj_token → 块数」；只读，一条 lark-cli 调用即可。可读率为 0 就别开灰度，先解决授权 |
| 评论写入 dry-run | `--dry-run` 预览请求，不真写 |
| 旁路对主链路无影响 | 白名单留空跑一轮现有 golden + 心跳 |
| `python -m py_compile` + `import app.main` | 闸 2 冒烟 |

### 8.2 灰度顺序

1. **只读干跑**：`enabled=true` + `dry_run=true`，识别结果只写日志不发评论 → 看误报率
2. **单群试点**：挑 1 个活跃 Story 群开评论写入
3. **扩群 + 接妙记**
4. 采纳写回正文（若 Q3 签收）

### 8.3 回滚

`[projects.prd_watch].enabled = false` → 重启，旁路不注册，主链路恢复原状。已写入的 PRD
评论不自动删（人工 resolve 即可），台账记录保留。无 DB Schema、无对外接口变更。

---

## 九、边界与遗留

### 9.1 边界

- 只服务「群讨论/会议 → PRD 变更建议」，不做 PRD 写作、不做需求评审。
- 识别基于当次讨论窗口，不建模需求全生命周期。
- 妙记侧依赖会议与 Story 的关联已由台账登记。

### 9.2 本期不做

| 事项 | 原因 | 后续 |
|------|------|------|
| 采纳后自动改 PRD 正文 | 误改不可逆，先立评论信任 | Q3 签收后 Phase 4 |
| Meegle Story **只读**已实现（2026-11-03）：`meegle.py` 走官方 MCP JSON-RPC + 个人 `m-` 凭证，`binding.py` 把 Story 标题/状态/PRD 链接/参与人自动换成绑定；多维表格退为「变更记录落库」 | 剩下的边界是 **群↔Story 映射 Meegle 上没有**（Story 无 chat_id 字段），只能管理员在 `story_chat_map` 逐个配——靠群名模糊匹配可能把建议写到别人的 PRD 上，禁止 |
| 评论事件订阅 | 轮询已够用且确定性高 | 需要即时性时再接 |
| 多租户/多项目分组 | 无需求 | — |
| 变更趋势报表 | 台账可自助透视 | 有真实使用量再说 |

### 9.3 待确认（卡在谁那里）

| 编号 | 问题 | 需要谁 |
|------|------|--------|
| Q1 | 需求台账三表是否已建？base token 与表 ID？（**代码已就绪**：`config.toml [projects.prd_watch].ledger_*` 三项留空时自动降级为内存台账并打告警） | 用户/项目管理员 |
| Q2 | 写评论用 user 还是 bot 身份 —— **实测已定但需签收**：`docs:document.comment:create`（写）与 `:read`（读回复）在**当前 user 登录态缺失**（真实调用返回 missing_scopes），而 `--as bot` 两条请求都能构造（`POST /drive/v1/files/{token}/new_comments`）。差别在署名：user=评论显示成同事说的（追溯失真），bot=评论署应用名（追溯清楚，但需应用对文档有评论权）。**建议 bot 身份**，需补一次应用权限申请 | 用户拍板 |
| Q3 | 「采纳即改正文」要不要做、何时做（现实现：采纳只回写 ✅ 状态 + 群通知，**不动正文**） | 产品负责人 |
| Q4 | `extract_min_confidence` 阈值 —— 初值 0.7/0.85 在真实网关跑过一轮：5 条变更（含拆细到 3 条）全绿，4 组负向（纯排期/闲聊/未定论提问/被当场否掉）全 0 误报。仍需一段真实群聊数据校准 | 干跑数据后定 |
| Q5 | 监听全群消息 + 妙记的合规评审走不走、找谁 | 用户/合规 |
| Q6 | 群内非@消息旁路后，是否仍需对成员可见「本群已被 easyprd 监听」的告知 | 用户 |
| Q7 | **PRD 文档资源级授权（真链路第一道硬阻塞）**：实测 examplecorp 最近 30 条 Story，「需求文档」字段 17 条是 http 链接 → 其中 **2 条填的是内部 git tree**（不是文档）→ **15 条飞书链接里只有 5 条能解析出 obj_token**，另 10 条报 `resolve wiki node failed: lacks permission`。已确认 **bot 与 user 两种身份同样读不了**（不是 scope 问题，`wiki:node:read`/`docx:document:readonly` 都在；是 wiki 空间/文档没对这两个身份开放）。而 token 一旦解析成功，正文 5/5 可读（块数 2~133）→ **卡点唯一，就是文档授权**。逐条清单（含每条 Story 的产品/提出人，可直接催办）由 `temp/prd_auth_request.py` 生成到 `temp/prd_auth_pending.md`。需 wiki 空间管理员批量开放，或约定 PRD 必须放在对机器人开放的空间；另有 13 条该字段为空或填成正文摘要，属产品填法问题，授权解决不了 | 用户/wiki 空间管理员 |

| Q8 | **识别输出的稳定性口径**：实测同一份会议纪要连跑 3 次，条数已稳定在 **7/7/7**，但每条标题措辞每次不同、个别条目的 `change_type` 会在「删除条款/流程变更」间摇摆。已做：识别固定 `temperature=0`（不吃站点覆盖）、按讨论关键词挑 PRD 节选（不再每次给不同片段）、prompt v1.2.0 加「拆分粒度」机械规则。**仍需拍板**：① 措辞漂移是否可接受（不影响语义，只影响观感）；② 是否要在生成后做一次「同一结果跨轮比对」的去重/归一（会增加一次 LLM 调用与延迟） | 用户 |
| Q9 | **跨轮归一/去重阈值**：已实现批内近义合并 + 跨轮重复拦截（`dedupe.py`，字符集包含度 0.50，带「主语」硬约束：申购≠赎回）。**仍需拍板**：这个阈值是按十几对样本标定的，真实灰度里若发现"两条不同变更被合并"（漏报，比多一条更糟）或"近义条目仍重复"，调 `DEFAULT_THRESHOLD` 即可，不必改逻辑 | 灰度数据后定 |

---

## 附录

- 图统一 Mermaid；渲染方式见 `docs/design/_template.md` 附录 A。
- 里程碑（第1步通道 → 第2步 diff 评论闭环 → 第3步接妙记 → 第4步写回正文）即 §8.2 灰度顺序，不再单列一份。
- 变更识别与评论生成的提示词落地时走 `bot/prompts/` 单源 + frontmatter 版本化，本设计稿不抄提示词正文。
