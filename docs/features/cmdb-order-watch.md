# 功能简报：CMDB 工单提醒（生产 + UAT，cmdb-order-tracker 内化）

> 改这个功能前读这一页即可动手。与代码矛盾时以代码为准，并回写本页。

## 1. 一句话

两条自助链路，都靠发送者身份落地、都不需要管理员改配置：

- **按姓名订阅**：@bot 说「订阅 CMDB 工单提醒 devuser」→ 他创建的每条任务，
  新工单 / 开始执行 / 完成或失败时逐条私聊推送。
- **按单号关注**：@bot 说「关注工单 5778 的执行情况」→ 盯**整张单**，开始执行时
  一次、全部任务结束时又一次（有失败标红）。

**生产与 UAT 都覆盖**（2026-11-13 起）：CMDB 有两套并行接口前缀（`OrderApi` /
`UatOrderApi`），数据完全隔离。⚠ **两套 order_id 是独立自增序列**——UAT 1924 与
生产 1924 是两张毫无关系的单，所以环境必须跟着每张单一起流动，不能当默认参数。

## 2. 主链路

```
订阅：消息 → 路由 cmdb-watch-agent → subscribe_cmdb_watch(自己用户名)
      → watcher.add_subscriber(发送者 open_id, cmdb_user) → state.json
关注：消息 → 同上 → watch_cmdb_order(工单号/链接)
      → watcher.add_order_watch(open_id, 工单号) → 登记即取基线（已开始不补发）

提醒：service._loop（每 interval_sec）
  → watcher.scan_once
      → _discover_orders：**两个环境各跑一遍** _discover_env(env)
                         TotalOrderApi / UatTotalOrderApi ?action=tree（有 session，
                         一次拿全 ~143 / ~190 张 id）
                         或 id 顺序探测（无 session，该环境水位线 +1 起 probe_span 个）
      → 新工单才 fetch_order(order_id, env)：OrderApi/UatOrderApi?action=data
        → parse_order(data, env)，Order.env 从这里确定，之后全程跟着走
      → 已跟踪且「还有任务未结束」的工单 fetch_status：OrderApi?action=deploystatus
        （<1KB，只回 deploy 状态码）；已收口的退出轮询预算
        每轮 8 路并发取数（判定仍串行），每 refresh_sec 才全量 fetch_order 校准一次
      → _process_order/_process_status：按订阅者切分任务 → 首见/迁移判定
      → _process_watches：被关注工单按整单阶段（pending/running/done）判定
        开始执行 / 执行完成（关注单每轮强制全量读，轻量接口没有 SQL/NACOS 状态）
      → _deliver：render.build_*_card → main._cmdb_send_card → channel.send_card(open_id)
      → store.save（logs/cmdb_watch/state.json，原子写）
```

## 3. 期望行为

- 新工单且我的任务都未执行 → **绿头**「CMDB 生产工单提醒」，副标题 `#单号 · 日期`，
  正文给工单名称/编号/命中人员/命中任务数，任务清单用**原生 table**
  （类型｜任务｜名称｜状态｜执行环境）。
- **UAT 卡片**：标题加 `· UAT` 后缀（「CMDB 生产工单提醒 · UAT」/「CMDB 任务已完成 · UAT」），
  正文多一行 `**执行环境：** UAT`，底部链接指向 `UatOrderDetail?order_id=N`。
- 我的任务 未执行→执行中 → 蓝头「CMDB 任务执行中」；→成功 → 绿头「CMDB 任务已完成」；
  →失败 → 红头「CMDB 任务执行失败」（含「任务未能成功完成」）。
- 术语统一：CMDB 的 `未执行/执行成功/执行失败/已取消任务` 在卡片上一律写
  `待执行/成功/失败/已取消`；字段名用业务语言（执行状态/任务创建人/执行环境）。
- 一人一张卡：同一张工单多人协作时，每个人只看到自己名下的任务，各发各的私聊。
- 首轮是「发现」不是「提醒风暴」：已有执行记录的工单静默入册，不补喊历史。
- 同一任务同一事件对同一人只发一次；发卡失败会回滚额度、下一轮补发。
- **同号不同单**：生产 100 与 UAT 100 同时存在时是两条独立跟踪（`prod:100` /
  `uat:100`），各自的第一次见、各自的去重、各自的卡片。
- 工单里我的任务被删 → 退出跟踪，不再每轮拉它。
- **关注工单**：开始执行 → 蓝头「CMDB 工单开始执行」；全部任务结束 → 绿头
  「CMDB 工单执行完成」，有失败 → 红头「CMDB 工单执行结束（有失败）」。
  卡片列**整张单**的任务（关注的是「这单发完没有」，不是他自己那几条）。
- 关注时这张单已经在跑 → 只提醒「完成」，不补发「开始」；已经结束 → 不发卡。
- 关注单连续拉取失败到 `watch_error_limit`（默认 5）轮 → 自动停止并告警
  （工单被删/权限回收时不该无限重试）。
- 没人订阅时不发任何请求（零开销）；冷启动只对齐水位，不把历史工单全量拉一遍。

## 4. 约束与不变量

- **只读**：绝不触发 SQL 执行/发布/NACOS 变更（证据：原 skill 硬约束第 1 条；本实现只调 GET 读接口）。
- **环境是一等参数，绝不靠「先试生产、失败再试 UAT」猜**：两套编号空间重叠，
  猜错会把用户对 UAT 单的关注悄悄绑到一张同号的生产单上，然后推一堆不相干的提醒。
  环境来源优先级：链接里的（`UatOrderDetail` > `OrderDetail`）> 工具参数 `env` > 生产。
  **判定顺序必须先 UAT**——`uatorderdetail` 里也含 `orderdetail`，反了就永远判成生产
  （单测 `test_order_page_detects_env_uat_first`）。
- **所有落盘键、去重键、日志键都带环境前缀**（`prod:5784` / `uat:1924`）：
  `WatchStore.orders` / `order_watches` / `scanned_max` / `notified` 的 key 全部如此。
  老档案（键里没有冒号）按生产读入，升级不丢跟踪状态（`test_legacy_state_without_env_reads_as_prod`）。
- **水位线每环境一条**（`scanned_max: {prod, uat}`）：共用一条会让 UAT 的 1932
  把生产的 5917 拉回去，之后每天全量重扫（`test_each_env_keeps_its_own_watermark`）。
- **只能订阅本人**：订阅归属由消息发送者 open_id 决定，Agent 不接受「帮别人订阅」。
- **人员匹配是全等且大小写不敏感**，不做包含匹配——`wang` 不能命中 `wangwei.wang`
  （单测 `test_mine_is_exact_match`）。
- **两种关注的语义不能混**：姓名订阅是「逐条任务」，单号关注是「整单两个节点」。
  退订/取消只影响各自那一条（`unsubscribe_cmdb_watch` vs `unwatch_cmdb_order`）。
- **「执行完成」包含失败**：用户问的是「发完了没有」，失败单独标红。
  空工单（任务还没填）算「未开始」，不说完成。
- **「新工单」看任务级 creator**（`sql_list[].creator` / `deploy_data[].creator`），
  不是子单 `client.creator`——实际要做的那条 SQL/那个发布才代表人。
- **取数分两档**：新工单全量读（必须，要判断创建人）；跟踪中只轮询轻量状态码。
  改回「每轮全量」会让请求量与耗时翻十倍（单测 `test_polling.py` 用调用计数钉死）。
  例外：**被关注的工单每轮强制全量**（整单阶段要按全部任务判定，含 SQL/NACOS）。
- **请求数只随「还有任务未结束的工单」增长，不随订阅人数增长**（2026-11-06）：
  实测 50 订阅者 / 93 张跟踪单、全部收口时每轮 1 次 tree + 0 次轮询；同一批里
  还有 4 张活着时是 1 + 3(探测) + 4 = 8 次 / ~0.7s。50 人订阅前同一场景是
  34 次 / ~2s（30 张全量 `data`）。单测 `test_50_subscribers_request_count_stays_flat`
  与 `test_request_count_scales_with_live_orders_not_subscribers` 把这条钉死。
- **按类型过滤是推送侧过滤**（`Subscriber.kinds`），不减少取数：漏拉会导致状态
  迁移看不出来，少推只是少打扰。空 = 全收（兼容 2026-11-06 之前的订阅记录）。
- **关注单的失败计数按人隔离**：`_on_watch_error(open_id=...)`，一次内网抖动
  不该把所有人的关注一起摘掉。
- **不改**：`client.py` 的状态码表（来自 CMDB 前端 chunk 反编译，见 skill 的
  status-machine.md）；改前先核对前端。
- **内网必须直连**：`client._http_json` 刻意不走 `urllib.urlopen`（会吃 `HTTP_PROXY`，
  Clash 代理下内网域名一律 502）。

## 5. 单源与派生物

- 单源：`app/domains/cmdb_watch/`（client/store/watcher/render/service/subscriber/progress）+
  `app/agents/handlers/cmdb_watch_handler.py` + `bot/agents/cmdb-watch-agent/agent.yaml` +
  `bot/prompts/agents/cmdb-watch-agent.md` + `app/config.py::CmdbWatchConfig` +
  `config.toml [projects.cmdb_watch]`
- 派生物：`logs/cmdb_watch/state.json`（订阅者 + 观察状态，gitignore）；
  `bot/prompts/INDEX.md`（`python -m app.eval.prompt_index` 生成）；
  `~/.cmdb-session` 与 cmdb-order-tracker skill **共用**（不要各存一份）

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-11-04 | 用户要求把 cmdb-order-tracker 的「选择我名字就提醒」内化成 bot 能力，卡片版式照「授权操作通知」（绿 header + 字段行 + 底部链接），三个时机全要、推私聊 | 本次改动（未提交） |
| 2026-11-04 | 实测 `OrderApi?action=data/detail` 免 session；`TotalOrderApi?action=tree` 与 `ArcheryApi?action=status` 需 sessionid；列表接口忽略分页参数 | 本文 §7 |
| 2026-11-04 | 用户反馈首版文案「不够专业」：去 emoji 前缀与口语，字段名改业务语言，任务清单改原生 table，工单标题裁掉日期 | 本次改动（未提交） |
| 2026-11-04 | 用户要求「所有人都能用，授权给 bot 即可」：名单从 config 迁到自助订阅（open_id→CMDB 用户名），一人一卡私聊推送 | 本次改动（未提交） |
| 2026-11-04 | 用户要求「关注 xx 工单的执行情况，开始执行/执行完成告诉我」：新增指定工单关注（`progress.py` 整单阶段聚合 + `OrderWatch` 状态 + 三工具），关注单每轮强制全量读 | 本次改动（未提交） |
| 2026-11-04 | 用户追问「不该每次都全量获取」：改为新单全量 + 跟踪单轻量状态轮询 + `refresh_sec` 全量校准；同时发现内网请求被 Clash 代理劫持成 502，改直连 | 本次改动（未提交） |
| 2026-11-06 | 用户要求「开放给所有人用」+「解决拉 CMDB 的性能问题」：订阅端补 `kinds` 按类型过滤（SQL/应用发布/Nacos 配置/定时任务，可多选）；取数端加「已收口退出轮询预算」+ 同轮 8 路并发 + 新单不重复问。实测 50 人订阅稳态 34 次/2s → 8 次/0.7s，全收口时 4 次/0.5s；state.json 50 人/100 单 54KB | 本次改动（未提交） |
| 2026-11-06 | 实测否掉了三个「更省」的猜想：`deploystatus` 不吃逗号分隔的多 id（回 int 解析错误）、`NacosApi` 没有 status 动作、列表标题里的发起人与任务级 `creator` 有 50% 不一致（40 张里 20 张）——所以**不做按标题预筛**，也不算「按需只拉订阅者相关单」 | 本文 §7 |
| 2026-11-07 | 用户反馈「CMDB 登录态失效」：`~/.cmdb.conf` 里本就有账号密码，改为 `client.login/renew_session_if_stale` 自动续期（`POST /apiservices/login/` 取 `Set-Cookie: sessionid`），`.env` 的 `CMDB_SESSION_ID` 清空以免遮蔽续期 | 本文 §7 |
| 2026-11-07 | 用户问「订阅还在吗」，bot 回「未订阅 / 0 人订阅 / 登录态缺失」，与 state.json 和主进程日志矛盾：dsh 的 MCP 子进程里影子 watcher 构造后从不 `load()`，`_ArchiveOnlyClient.session_id` 恒为空。修法=构造即读档 + session_id 读本地会话文件（不探活） | 本次改动（未提交） |
| 2026-11-13 | 用户要求「UAT 工单也要能订阅」：实测 `UatOrderApi` / `UatTotalOrderApi` / `UatArcheryApi` 与生产并行、读接口同样免 session、同一个 sessionid 两套列表都能读（生产 138 张 / UAT 192 张）；两套 order_id 独立自增 → 环境做成全链路一等参数（`Order.env` + 所有键加前缀 + 分环境水位线 + 卡片标题后缀与 UatOrderDetail 链接 + `watch_cmdb_order(env=)` / 链接自动识别） | 本次改动（未提交）、`test_uat_env.py` |
| 2026-11-13 | 用户反馈「CMDB 能力把网页登录挤掉」：日志里一天 40+ 次「CMDB 已重新登录」，用的是本人账号 `~/.cmdb.conf`，而 CMDB 单会话登录 → 每登一次就把浏览器踢下线（每 120s 一轮）。改为接 `CMDB_ALLOW_RELOGIN` 三态（默认 confirm 不登）+ 支持独立服务账号 `~/.cmdb-bot.conf`（存在即优先）+ 登录后先探活再落盘 + 修「续期成功却仍告警」的陈旧 `stats.errors` | 本文 §7、`test_session_policy.py` |

## 7. 已知坑

- 列表接口 `TotalOrderApi?action=tree` **不分页**（传 page/limit 被忽略），所以「新工单」
  只能按 id > 水位线判增量，且一次最多用返回的那批。
- `action=data` 内嵌的 `sql_status` 可能是旧值；轻量轮询看不到 SQL/NACOS 变化，
  由 `refresh_sec`（默认 900s）全量校准兜住——这两个的提醒最多晚一个校准周期。
- 无 session 时列表入口静默降级为 id 探测：功能仍在，只是发现晚一轮、每轮多 30 次
  探测请求（~1.7s）。启动日志会明确告警 `CMDB 登录态无效或缺失`。
- **后台不自动登录 CMDB**：CMDB 是单会话登录，用本人账号（`~/.cmdb.conf`）在后台登一次
  就把浏览器里那条顶掉，表现是「浏览器老是要重新登录」。故 `renew_session_if_stale`
  默认只降级 + 告警（`CMDB_ALLOW_RELOGIN=confirm`），「已重新登录」这句话只有配了
  独立服务账号（`~/.cmdb-bot.conf`）并显式设 `true` 时才该出现在日志里。
- 本机开着 Clash 时代理环境变量会劫持内网请求 → 502；排查「CMDB 挂了」前先确认走的是直连。
- `.env` 里的 `CMDB_SESSION_ID` 优先级高于 `~/.cmdb-session`，过期后要两处一起更新
  （否则改了文件也不生效）。
- **接口不能批量**：`OrderApi?action=data|detail|deploystatus` 的 `order_id` 只吃单个
  整数（传 `a,b` 会 500「invalid literal for int()」），所以「一次拉 N 张」做不到，
  能省的是「少问哪些单」+「并发问」。真正的并行收益：30 张 `deploystatus`
  串行 1468ms → 8 并发 265ms（实测，best of 3）。
- **轻量接口只回 deploy**：`action=deploystatus` 的返回里没有 `sql_status` /
  `nacos_status` / `cron_status`，`NacosApi` 也没有 status 动作。所以 SQL/NACOS/
  定时任务的完成提醒仍靠 `refresh_sec`（默认 900s）全量校准，最多晚一个周期。
- **发起人 ≠ 归属**：列表标题尾部 `-- xxx` 只是建单人，实测 40 张里 20 张的任务
  `creator` 与标题不一致（有人替别人建单）。任何「按标题预筛」都会漏单。
- **UAT 与生产是两个页面、两套编号**：`/order/UatOrderDetail?order_id=N` vs
  `/order/OrderDetail?order_id=N`。同 N 不代表同一张单，引用单号必须带页面名。
  卡片链接由 `render.order_url(order_id, base_url, env)` 按 `ENV_ORDER_PAGES` 拼。
- **UAT 列表比生产短**（实测 192 vs 138 条，UAT 侧 id 才到 1932）：冷启动对齐水位时
  两个环境各对各自的，别用「最新 id 较大者」去推另一个。
- **`UatArcheryApi` 参数与生产不同**：生产 `action=status&sql_id=N`，UAT 侧同一个
  动作会回「参数不能为空」（实测 2026-11-13）。目前 `sql_status()` 只用于诊断，
  提醒主链路不吃它——要加 UAT SQL 权威状态前先把这个接口摸清。
- **影子进程必须读档**：dsh 给每个会话起一份 `capabilities` MCP 子进程，里面的
  `CmdbWatcher` 不跑轮询，只当「看档案」的影子。它不 `load()` 时所有查询类工具
  都对着空档案作答——用户上一句刚订阅，下一句被告知没订阅，看着像功能坏了。
  同理 `session_id` 不能恒空：读 `~/.cmdb-session` 就够，探活属于主进程的事。

## 8. 演练记录

| 日期 | 需求 | 未预读实现？ | 一次通过？ | 返工原因 |
|---|---|---|---|---|
