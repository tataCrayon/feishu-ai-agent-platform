# 功能简报：能力执行入口（capabilities MCP）

> 改这个功能前读这一页即可动手。与代码矛盾时以代码 + commit 为准，并立即回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：`docs/specs/_feature-brief-template.md`

## 1. 一句话

dsh 会话里的模型要能**真的动手**：技能目录只给「有哪些能力」，`capabilities` MCP server
给「怎么执行」——缺了它，模型会如实说「本会话没有这个执行入口」。

## 2. 主链路

```
main.py::_process_batch → DshGateway.handle(chat_id, caller={sender_id,…})
  → supervisor.open_session(chat_id, caller)
      → mcp_server_specs(env={PROMATE_CALLER_CHAT_ID/SENDER_ID/SENDER_NAME})
      → session/new（dsh 为**每个会话**拉起独立 MCP 子进程）
  → 模型调 mcp__capabilities__<tool>
      → app/mcp/capabilities_server.py::dispatch
          → 现场装配的 Handler.execute_tool(tool, args)
          → 回执 JSON（ok / reply / error）
```

## 3. 期望行为（可观察面）

- 数字先知：`gather_signals` 真取市场数据（不再回答「没有执行入口」）
- 订阅类提醒：绑定/取消/查状态，**绑定到发这条消息的人**（身份不接受模型口述）
- 定时任务：创建评估通过即纳入调度并回执卡片（**无审批门**，2026-11-08），暂停/恢复/取消按本人或 L2 鉴权
- 需要人工点按钮确认的整理动作不投影：走卡片链路，脱离飞书会话做不完
- 团队 Skill 目录：盘点/查重/审查/状态板

## 4. 约束与不变量（改前必读）

- **schema 原样透传**：用低层 `mcp.server.lowlevel.Server` 直接返回 `mcp.types.Tool`。
  FastMCP 装饰器会按 Python 签名重推 schema，`gather_signals` 的自由字典
  （`additionalProperties`）会被压掉，模型就无法传任意信号名
- **身份只从 env 来，不认工具参数**：工具参数是模型生成的，模型「听说」一个用户名
  就会填进去；订阅/关注必须绑真实发送者。dsh 每会话一个 MCP 进程 ⇒ env 天然按会话隔离
- **子进程没有 main 的对象**：watcher / scheduler service / skill catalog store
  都在 `registry()` 里按同一份 `config.toml` 就地装配；照搬 main 的「未接线请重启」
  等于把能力永久打死（配置明明开着，用户看到「功能关了」）
- **写操作不许绕过服务层**：定时任务创建必须走 `ScheduledTaskService.submit()`
  （建档 + 评估 + 发回执卡），不能直接写 store；暂停/恢复/取消必须校验
  sender_id = 创建人 或 L2。审批门已下线，评估门仍在（间隔 <10 分钟等照样驳回）
- **装配失败只能降级不能抛**：MCP server 起不来 → dsh 直接拒绝 `session/new`，
  用户看到「bot 完全不回话」，比少几个工具糟得多
- **Guard 的文案要能行动**：拒绝时说清「去哪里做」，不说「不支持」

## 5. 单源与派生物

- 单源：各 Handler 的 `tools` 类属性（工具名/描述/schema）+ `app/mcp/capabilities_server.py`
  （投影规则：`_NEEDS_LIVE_CHANNEL` / `_NEEDS_CALLER`）
- 派生物：`config.toml [projects.mcp.capabilities]` 挂载声明（`app/main.py::_dsh_mcp_specs`）

## 6. 证据锚点

- 2026-11-05 实测：会话工具从 8 → 26 个（22 个能力工具 + feishu/domains 的 8 个），
  数字先知 / 定时任务 / Skill 目录全部可达
- 2026-11-05 实测：`create_scheduled_task` 真发审批卡（当天档案落 `logs/scheduled_tasks/pending/`、
  飞书会话内可见「定时任务审批」卡片）——该审批门已于 2026-11-08 按用户要求下线，
  新建任务直接落 `approved/`，残留 pending 由调度循环收编
- 2026-11-05 迁移前反例：模型回答「本会话没有挂载该执行入口」「创建属写操作本期未开放」
- 2026-11-06 实测：真实 dsh 会话里模型自主调用 `mcp__capabilities__fetch_approval`
  → `process_order`，用短链 iXqq4KFo 走完「审批单 → SQL」全链（流程号 202610020012、
  命中案例 `客户销户`、7 条 SQL）。当天同时修掉两处会静默打断子进程的问题：
  dsh 不透传 `mcpServers[].cwd`（外部仓 `-m order_agent.mcp_server` 报 No module），
  以及 env 条目数组按名折叠（同名 PYTHONPATH 写两条会互相覆盖）。修法见
  `app/main.py::_dsh_mcp_specs` 与 `app/dsh/supervisor.py::mcp_server_specs`。
