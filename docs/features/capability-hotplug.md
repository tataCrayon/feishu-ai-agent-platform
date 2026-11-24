# 功能简报：能力热插拔（管理员飞书自助接入）

> 改这个功能前读这一页即可动手。与代码矛盾时以代码 + commit 为准，并立即回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：`docs/specs/_feature-brief-template.md`

## 1. 一句话

把「接入新能力（Agent / Skill / MCP 声明）」从登服务器放文件 + 重启，降为一次群聊交互：
管理员把能力包发进会话 → 静态扫描 → 审批卡片 → 点「接入」→ 落盘 + 热生效，全程留痕。

## 2. 主链路

```
飞书文件消息（zip / agent.yaml / SKILL.md，≤2MB）
  → main.py::_handle_capability_package（[projects.capability].enabled 时挂载）
  → app/ops/capability/ingest.py::ingest
      → scanner.scan（凭据 / 危险指令 / zip-slip / ${ENV} 占位符；high → 直接拒）
      → store 建档案（bot/inbox/，gitignore）
      → approval_card → channel.send_card（提交人与审批人都须 L2）
  → 用户点「接入」→ main.py::_on_capability_card_action
      → HotReloader.deploy(rec, pkg_dir)
          → 覆盖前把旧版备份进 bot/inbox/archive/（D4）
          → 落盘 bot/agents/ 或 bot/skills/
          → registry.reload + deploy/dsh/sync_skills.py（能力不进白名单 = 模型看不见）
          → python -m app.agents.generator 生成 settings.json
          → 回切链路 CrewOrchestrator.rebuild()（dsh 启用时不参与消息路由）
      → 卡片原地刷成「已接入」+ 生效范围说明
```

## 3. 期望行为（可观察面）

- 管理员发包 → 回「已收到，正在扫描」→ 扫描通过才发审批卡；high 风险直接拒并说明命中项。
- 非 L2 发来的包 → 提示无权限，不建档案、不发审批卡。
- 点「接入」→ 卡片刷成「✅ 已即时生效（进程内路由/编排）」或「新会话生效（skill / cc-connect）」。
- 回执里列出缺的环境变量（`.env` 里没配的 `${ENV}`），不假装已生效。
- 覆盖同名能力时，旧版备份存在 `bot/inbox/archive/`，回执给备份路径。
- 超过 `max_pkg_mb`（默认 2MB）→ 拒收，不下载。

## 4. 约束与不变量（改前必读）

- **D1 提交与审批都须 L2**：可以是同一人，但双方身份都要过 `get_user_permission().level >= approver_min_level`。
- **D2 MCP 只经新建 Agent 包承载**：不给既有 agent 追加 `mcp_servers`——动已验证能力，风险面大。
- **D3 high 自动拒，无强制通过入口**：误杀走线下改包重提。扫描是静态的，最终把关是审批人。
- **D4 覆盖前先备份**：旧版进 `bot/inbox/archive/` 再落盘新版。
- **凭据铁律**：能力包里只许写 `${ENV}` 占位符；明文凭据一律 high 拒。
- **生效边界**：agent 类即时生效；skill / cc-connect 类对**已存活会话**不生效（会话缓存 SKILL 清单），
  卡片必须如实写「新会话生效」，不许写「已生效」。
- **dsh 链路额外的坑**：新能力不进 `deploy/dsh/agentbiz/skills.allowlist`，dsh 的路由候选集里就没有它
  ——模型的表现是「这个我做不了」，不是报错。
- **「不改」清单**：不要为了「热插拔更顺」放宽 D1/D3；不要给既有 agent 追加 MCP（D2）。

## 5. 单源与派生物

- 单源：`app/ops/capability/`（ingest / scanner / store / approval_card / reloader）
- 派生物：`bot/agents/*/`、`bot/skills/*/`（审批落盘）、`.claude/settings.json`（generator 生成，gitignore）、
  `deploy/dsh/agentbiz/skills/`（`sync_skills.py` 生成，gitignore）

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-09-12 | D1-D4 评审确认：双方 L2、MCP 只走新建 Agent 包、high 无强制通过、覆盖先备份 | 本文 §4（原始决策记录在 git 历史 `docs/design/20260912-capability-hotplug.md`） |
| 2026-09-12 | 部署形态确认为本机单机单进程：inbox / graph 重建都在单进程内，无需共享存储 | 同上 |

## 7. 已知坑

- 扫描器只做静态检查：包里带一个能跑的脚本不等于它行为正确，审批人仍是最后一道闸。
- `.env` 没配的 `${ENV}` 不会阻塞接入，但对应 MCP/数据源会起不来——回执必须把降级项写出来。
- 热重建失败时保留旧图（原子替换），但落盘已经发生——用户看到「已接入」不等于功能可用，
  失败分支必须把实际异常回报到卡片。
