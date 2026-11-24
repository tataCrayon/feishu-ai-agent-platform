# 功能简报：PM 工作台 · 能力接口

> 改这个功能前读这一页即可动手。与代码矛盾时以代码 + commit 为准，并回写本页。

## 1. 一句话

把我们自己的 PM 交付能力（排期雷达、事项账本、待办、会议、人物、知识库）包成
`/api/pm/*` HTTP 接口，给**外部调用方**（PM 本人的工作台脚本）读写；
能力层不认识任何人——身份与表位置全部来自 `[projects.pm]` 配置。

## 2. 主链路

```
外部调用方（PM 工作台）
  → GET/POST /api/pm/*（app/api/pm_capabilities.py，挂在 dashboard :9112）
      → app/domains/pm/ 下的能力函数（pm_watch / pm_thread / pm_hub_engine / …）
          → lark-cli（读：user token；写：user token）或 tenant token（发卡片）
```

常驻调度另起一个进程：`python -m app.domains.pm.pm_run`（`scripts/start-pm-daemon.bat`）。

## 3. 期望行为（可观察面）

- `[projects.pm].enabled = false` 时：每条 `/api/pm/*` 返回 **503 + 原因**，
  不是空数组——空数组会被读成「今天真的没事」。
- 启用后 17 条路由全在（清单见 `tests/test_pm_capabilities.py::test_every_pm_route_is_registered`）：
  `events` / `scan` / `threads` / `threads/{key}` / `threads/{key}/settle` /
  `backlog` / `sheet`（GET+POST）/ `records`（POST+PUT）/ `radar` / `people` /
  `doc` / `minutes`（列表+详情）/ `nudge-plan` / `cards`（POST+PATCH）。
- 卡片：`POST /api/pm/cards` 传 `{target, card}`，`PATCH /api/pm/cards/{message_id}`
  就地改原卡**不新发消息**（实测：聊天记录里只有一条，内容被改写）。
- 单位全链路毫秒；日期字符串 `YYYY-MM-DD`；字段名就是 `.threads.jsonl` 里的名字。

## 4. 约束与不变量（改前必读）

- **不改名**：`text` / `ts` / `who` 不改成 `content` / `time` / `sender`——
  `.threads.jsonl` 里有 1400+ 条历史数据就是这个名字，改了要同步改一整套消费端。
- **不 omit 空值**：`""` / `null` / `0.0` / `[]` 显式出现。`business` 为空是
  「还没归」，是信息不是缺数据（证据：`test_threads_returns_raw_fields_not_rendered_string`）。
- **不聚合**：排序、截断、拼「决策队列」都是调用方的事。`threads_of` 给结构，
  不给 `queue()` 渲染好的字符串。
- **`last_at` 必须手动塞**：它是 `@property`，`asdict()` 拿不到；漏了调用方就没法按
  「最近动静」排序（证据同上测试的 `assert "last_at" in row`）。
- **`settle` 非法 action 在 HTTP 层就 400**：`pm_thread.settle` 遇到非法 action 只
  `warning` + 返回 False，照抄会变成「200 却什么都没写」。
- **`directed` 是布尔字段，不是 `"@我" in reason`**：判定结论不能编码进给人看的文案。
- **`/doc` 路由必须是 `async`**：`LarkCli.get_doc_content` 是协程，同步路由拿到的是
  未 await 的对象，返回体会变空。
- **`workdays_left` 是 `warnings[]` 的独立字段**：文案里也拼了「剩N工作日」，但下游
  按天数排序/过滤时不该对句子再正则一次（证据：
  `test_radar_warnings_carry_workdays_left`）。

## 5. 单源与派生物

- 单源：`app/api/pm_capabilities.py`（路由与出口纪律）、`app/domains/pm/`（能力实现）、
  `app/config.py::PmConfig`（绑定）、`config.toml [projects.pm]`（值）
- 派生物：`deploy/dsh/agentbiz/skills/pm-hub-agent/SKILL.md`（由 `sync_skills.py`
  从 `bot/agents/pm-hub-agent/agent.yaml` 生成）

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-11-13 | PM 能力层 36 个模块从对方工作副本迁入 `app/domains/pm/`；裸 import 全部归一为 `app.domains.pm.*`，24 处重复 `try/except ImportError` 删除 | 本次改动（未提交） |
| 2026-11-13 | 去参数化：`PM_OPEN_ID` / `SPREADSHEET_TOKEN` / `PMO_GROUP_ID` / `_ME_OPEN_ID` / `BOT_OPEN_ID` 改读配置或运行期取，`app/domains/pm/` 里不再有 `ou_`/`oc_`/`tbl` 字面量 | `tests/test_pm_config_wiring.py` |
| 2026-11-13 | 卡片私聊出口实测：`send_interactive_card` 原来写死 `receive_id_type=chat_id`，传 `ou_` 直接 `invalid receive_id`（code 230001） | `pm_confirm_card._receive_id_type` |
| 2026-11-13 | 提测询问节拍从 `morning` 分支里提出、加持久化待发账本 | `pm_daemon._flush_nudge_ask` / `_NUDGE_PENDING_PATH` |
| 2026-11-13 | 多身份隔离：每个 PM 用户一套 `~/.lark-cli/pm/<open_id>/`，靠 `LARKSUITE_CLI_CONFIG_DIR` 切换；机器级 `hermes/`（PRD/妙记/案例镜像）完全不受影响 | `app/domains/pm/pm_identity.py` + `tests/test_pm_identity.py` |
| 2026-11-13 | 对话内授权：`/pm 授权` 发起 device flow → 发链接 + 二维码图 → 用户扫码 → `/pm 授权完成` 收尾 | `pm_setup._begin_auth` / `_complete_auth`、`feishu_send.send_image` |
| 2026-11-16 | 传输层换底：`pm_project_mcp` 不再读 `~/.cursor/mcp.json`，改用官方 MCP 端点（复用 `prd_watch.meegle.MeegleClient`），并封装自动翻页 | `app/domains/pm/pm_project_mcp.py` |
| 2026-11-16 | 取数路径修正：`所属迭代` 字段全 null → 改走 sprint 侧 `迭代下需求` 反查；状态筛选改中文 label；`9-待上线`/`9-生产验证` 分档 | `app/domains/pm/pm_story_source.py` |
| 2026-11-16 | 双进程抢答分界：`app.main` 与 `pm_run` 连同一个飞书应用，同一条私聊各答一次。主链在 `_process_batch` Step 0.15 对「PM 本人私聊」整段让路（daemon 存活时），群里不让 | `app/main.py::_pm_owns_message` + `tests/test_pm_ownership.py` |
| 2026-11-16 | `app/` 下 18 个过期测试副本删除（`tests/` 镜像已覆盖全部函数）、唯一缺失镜像的 `test_session_policy.py` 迁入 `tests/`；修复 `test_architecture` 长期红灯 | `tests/test_architecture.py` 转绿 |
| 2026-11-16 | 催办改卡片：`pm_nudge_card`（每条三个按钮 + 整批两键）+ `pm_nudge_actions`（点击动作）+ `pm_card_server`（:9113 回调）；`pm_nudge.send_one` 抽出 `_send_items` 与整批共用发送逻辑 | `app/domains/pm/pm_nudge_card.py` + `tests/test_pm_nudge_card.py` |
| 2026-11-16 | 日计划改卡片：概览四数字 + 分区 + 挂账徽标 + 表格直达按钮，文本渲染保留兜底 | `app/domains/pm/pm_daily_card.py` + `tests/test_pm_daily_card.py` |
| 2026-11-16 | PM 私聊转发：`pm_run` 判域外 → `POST /api/pm/relay` → 主链 dsh 处理 → 答复回 pm_run 发出；新增 `app/runtime.py` 解决 `__main__` 双模块问题 | `app/api/pm_relay.py` + `app/domains/pm/pm_relay.py` + `tests/test_pm_relay.py` |
| 2026-11-16 | PM 受限 `/dev` 通道：cc-connect 新增 `pm-dev` 项目，`CLAUDE_CONFIG_DIR=~/.claude-dev-pm`（permissions.deny 封共享内核）+ PreToolUse hook（`DEV_SCOPE=pm` 按路径前缀二次校验）。`/dev` 按身份选项目而非等级（两人都是 L2） | `app/main.py::_dev_project_of`、`~/.claude/scripts/dev-scope-guard.sh`、`~/.cc-connect/cc-exec.toml` |

## 7. 已知坑

- **`config.toml` 的段位置**：`[projects.pm]` 必须挂在 `[[projects]]` 下面（与
  `[projects.dashboard]` 同级）。写成顶层 `[pm]` 会**静默**读到默认值，不报错也不打日志。
  启动日志里有一行 `[config] pm: enabled=… identity=…` 就是为这个留的。
- **`load_config()` 是白名单式**：不在 `AppConfig` 上的字段它连看都不看。加配置项
  必须同时在 dataclass、`_load_*_config`、`AppConfig` 三处落地。
- **PM 的 bot 应用与 feishu-agent-platform 不是同一个**：`APP_ID` / `APP_SECRET` 从 `.env` 读
  `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，而 `.env` 里这份属于另一个应用；
  真接 PM 那套时要换成对方应用的凭据（否则发出去的消息署名是错的机器人）。
- **`app/domains/pm/` 里的模块仍是裸脚本风格**：`if __name__ == "__main__"` 段大多
  保留着，用 `python -m` 跑会报 `ModuleNotFoundError`（包内是绝对 import）。
- **`pm_hub_handler` 的会议工具已改口径**：原 `analyze_meeting_insights` 随会议认知
  迁走，`pm_scan_recent_meetings` 现在只回 Digest（标题 / 链接 / 未完成行动项），
  过筛统一在 `pm_daily_triage`。
- **`.threads.jsonl` 等状态文件在仓库根**：`parents[3]` 从 `app/domains/pm/x.py`
  算上去正好是仓库根（迁移前是 `app/agents/handlers/`，层数相同，勿再挪层）。
- **lark-cli 身份是机器级单例**：`auth login` 换人会把整台机器的登录态换掉。
  所以 PM 用户各自一套目录（`~/.lark-cli/pm/<open_id>/`），靠
  `LARKSUITE_CLI_CONFIG_DIR` 切换。**所有 lark-cli 调用必须走 `_run_lark_cli`**，
  自己拼 `subprocess` 会退回机器级默认身份——看起来能读，读到的是别人的权限边界。
- **新目录必须先铺 `config.json`**：空目录下 `auth login` 报 `not configured`，
  它会要求跑 `config init --new`（那会新建一个应用）。`pm_identity._seed_config`
  铺的是「已有 app、users 为空」，device flow 才能正常发起。
- **`auth qrcode --output` 的路径限制**：只接受 cwd / `/tmp` / `~/files` 三种根；
  且 lark-cli 在 Windows 只有 `.cmd` 壳，必须 `shell=True`，链接里的 `&` 会被
  cmd.exe 当命令分隔符——**URL 和路径都要加引号**，两者同时满足才跑得通。
- **飞书项目通道走官方端点，不读任何本机 IDE 配置**：`pm_project_mcp` 直接复用
  `prd_watch.meegle.MeegleClient`（`https://project.feishu.cn/mcp_server/v1` + `m-`
  用户凭证）。历史实现读 `~/.cursor/mcp.json`，那是**某个人的 Cursor** 才有的文件——
  换台机器就报「找不到 mcp.json」，看着像没权限，其实是数据源选错了。
- **MQL 一次只回 50 行，`LIMIT n` 不翻页**：必须拿 `session_id` +
  `list[0].group_infos[].group_id` 走 `group_pagination_list` 逐页取。
  `pm_project_mcp.search_mql` 已封装；不翻页的后果是**静默少数据**（139 个迭代只回 50 个，
  看起来像「迭代不存在」）。
- **story 上没有可用的「所属迭代」**：`field_edd7c0` 字段名存在但对全部 1389 条都是 null，
  按它 `WHERE` 永远 0 行。迭代归属只在 sprint 侧（`迭代下需求` = field_ae754c）反向挂着，
  所以「某迭代下有哪些需求」必须 **sprint → story id → 批量回读** 三步走
  （`pm_story_source._iter_story_ids` → `_stories_by_ids`）。
  一条需求可能挂多个迭代（实测 9/1034），取名字日期**最早**的那个——发版按早的那次算。
- **状态筛选要用中文 label**：`work_item_status = '6-实现中'` 有效，传 key
  （`'started'`）静默回 0 行。且 `9-待上线` 与 `9-生产验证` 是两个不同档位（数字前缀相同），
  `pm_story_source._STAGE_BY_LABEL` 按 label 精确覆盖。
- **排期字段 MQL 读不出来**：`schedule` 不随 `SELECT` 返回，节点级时间只能走
  `pm_schedule_risk.plan_of` 的 `list_schedule`（它靠 `work_item_id` 唯一定位实例）。
- **MCP 失败时 daemon 必须能活着跑**：`pm_nudge.due_iterations` 捕获后走「每周发版日」
  兜底节拍，`pm_daemon` 里这一块再套一层 `_probe_due("nudge_ask", 1800)` 节流——
  主循环 3 秒一轮，不节流会一天刷两万行同样的告警，真出问题时被淹掉。
- **两个进程抢答同一条消息**：`app.main`（WS 收消息）与 `pm_run`（3 秒轮询私聊）
  连的是**同一个飞书应用**，PM 本人私聊两边都接得到。分界写死在
  `app/main.py::_pm_owns_message`：**PM 本人私聊整段让**（daemon 的产品面，
  问排期/确认催办/`/pm 授权` 都得由它答），**群里不让**（群是主链地盘，
  daemon 群通道只在 `[projects.pm.push].group_id` 配了才开）。
  探测 daemon 是否存活要起 `wmic` 子进程（0.3s），所以先判归属再探测；
  探测异常一律当「不在」——宁可多答一句，也不要指令石沉大海。
  `wmic` 输出按系统代码页编码，**必须 `errors="replace"`**，否则
  `UnicodeDecodeError` 被 except 吞掉、结果恒为 False（静默失效）。
- **PM 受限 `/dev` 通道是两层保险，不是一层**：`mode=bypassPermissions` 只跳过
  权限提示，**不跳过 hook** —— 所以约束靠（1）`~/.claude-dev-pm/settings.json` 的
  `permissions.deny` 封共享内核路径，（2）`PreToolUse` hook `DEV_SCOPE=pm` 按路径
  前缀二次校验。两层都要，因为 deny 只认 `Edit(...)` 规则（写 `Write(...)` 会被
  警告“is not matched by file permission checks”）且管不住 shell 重定向。
  只读工具（Read/Grep/Glob）与只读命令（cat/ls/grep/git log…）在 hook 里直接放行：
  限的是「能改什么」，不是「能看什么」。
- **PM 私聊转发主链**：`pm_run` 判「不归 PM」时把原话投 `POST /api/pm/relay`
  （:9112，只收回环），主链用 `DshGateway.handle()` 处理 7 个 agent，答复文本回
  `pm_run` 由它发出去——**同一会话只有一个发送方**，否则转发结果和推送会交错。
  判定者在 `pm_run` 而不是主链：它每 3 秒在读那条会话，知道自己判了什么；主链
  不知道，让主链猜只能靠关键词表。
  **`python -m app.main` 下主模块叫 `__main__`**，`from app.main import X` 会再执行
  一遍 main.py 拿到全 None 的新副本（端点报「dsh 未启用」而日志说就绪）——
  进程级单例统一走 `app/runtime.py` 登记。
- **PM 卡片化（催办 + 日计划）**：催办卡片由 `pm_nudge_card` 画、`pm_nudge_actions`
  处理点击、`pm_card_server` 收回调（本机 :9113）。**按钮回调只有主链收得到**
  （飞书卡片回调走应用长连接，主链独占），所以主链 `_on_pm_card_action` 只做搬运，
  草稿状态仍留在 `pm_run` 的 `pm_nudge._PENDING` 里。
  日计划卡片 `pm_daily_card` 只换渲染，取数仍走 `build_daily_brief`；
  纯文本 `render_daily_brief` 保留为兜底与 `pm_agent` 工具返回值。
  **终态卡不能再带按钮**——一次确认即终态，重复点会二次发送。
- **测试文件一律放 `tests/`（镜像 `app/` 结构）**，`app/` 下不得有 `test_*.py`。
  `tests/test_architecture.py` 守这一条。历史上同一份测试在 `app/` 与 `tests/`
  各存一份，`app/` 那份逐渐过期（断言旧行为、mock 打在不存在的函数上）却仍被
  `pytest` 收集，表现为「单跑过、全量挂」的 flaky。
  迁移时先确认 `tests/` 镜像**覆盖了 `app/` 版的全部测试函数**，再删 `app/` 版。
