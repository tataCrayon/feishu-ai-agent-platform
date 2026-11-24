# dsh 选型实测证据（Phase 0 / 2026-11-04）

> 这份文件存在的唯一理由：**保留那些「不实测就会选错」的数字**。
> 决策与理由在 `docs/adr/006-dsh-migration.md`，行为契约在 `docs/specs/20261104-dsh-migration.md`，
> 现状架构见 `CLAUDE.md`。这里只写当时测到了什么，不写应该怎么做。
>
> 实测环境：Windows 10 19045 / Node 24.18 / `@deepseek-ai/dsh` 0.2.1-rc.2
> 方法：ACP（`dsh --profile acp`）作唯一协议面，探针脚本驱动真实会话（`D:/tmp/acp-spike/`，非仓库内）

## 1. 协议闭环（选型前提）

```
initialize        → protocolVersion 1，agentCapabilities 含 close / list / resume
session/new       → sessionId + configOptions(model / reasoning_effort)
set_config_option → 运行时切模型，不需重启进程
session/prompt    → stopReason: end_turn
session/update    → agent_thought_chunk / tool_call / tool_call_update / agent_message_chunk / usage_update
```

一次列目录任务透出 21 个事件。**判定**：进度卡所需的事件粒度 ACP 已够，不必再从 JSONL 补。

## 2. 路由：技能目录就是路由候选集

三组对照，同一 76 例 golden、同一模型，只改技能可见范围：

| 技能可见范围 | 通过率 | 失败特征 |
|---|---|---|
| dsh 默认根（个人 40+ 技能 + 项目 11 个） | 80.3% | 抢答到个人目录里**同名近义**的技能 |
| 关默认根，但项目目录残留旧技能 | 89.5% | 不再被个人技能抢，但仍命中废弃技能 |
| `includeDefaultRoots: false` + **仅项目 11 个业务技能** | **94.7%** | 失败集中在 direct-answer / clarify 边界 |

（更早一次「把 agent description 手拼进提示词」的零 shot 实验是 70/72 = 97.2%，220s / 3.06s 每例。
那个数字**不是真实部署形态**，不作为基线。）

**结论**：`includeDefaultRoots: false` 是路由正确性的前提，不是可选优化；
技能目录必须与「可路由的业务能力」一一对应，工具型技能放进去就会抢路由 → 于是有了
`deploy/dsh/agentbiz/skills.allowlist` 这层白名单。

## 3. 能力面下沉 MCP

用 Python MCP server 暴露 `send_message` / `read_doc`，经 `session/new` 的 `mcpServers` 挂载，
模型成功调用；三红线在工具层返回 `REJECT:`（如 `chat_id 必须以 oc_/ou_ 开头`）。

**结论**：飞书能力下沉后，红线从「提示词约束」变成「工具层硬拒」，安全性上升。

## 4. 安全门禁（唯一未完全对齐的一项）

`bot/prompts/_golden/safety-rules.json` 12 例重跑：9/12 = 75%。三条「失败」全是判定口径差异——
golden 的 `expected` 是行为标签（「禁止越权发布」「自动脱敏」），探针强行二值化。

**结论**：safety 不能指望提示词，须落成工具层硬约束 + 少量语义 golden。

## 5. 多轮连续性

同一 session 连续两轮不重复任何上下文（T1 记工作目录 → T2 准确复述）。
`history_max_turns=6` 这类硬伤随会话常驻消失，上下文压缩交给 dsh 的 compaction。

## 6. 两个必须知道的坑

1. **dsh 会读全局个人资产**：让它回答「全局环境里有没有关于 Windows 路径的说明」，
   它准确引用了 `~/.agents/skills/deepseek-harness-installer/SKILL.md` 的行号内容。
   → 线上 bot 必须指 `DSH_AGENTS_HOME` 到自己目录 + 显式 `customSkillDirs`（已落在
   `deploy/dsh/agentbiz/cordis.patch.yml` 的 `agent-instructions: disabled` 与 `skill-filesystem` 配置）。
2. **旧 `bot/prompts/` 不能原样搬**：`router/system_prompt.md`（11K）大部分被技能目录取代；
   `shared/self-check.md` 被 dsh 的 QC 剥离逻辑取代。**要保留的是 golden 用例本身**，提示词重写。

## 7. 后果：目标架构（当时推定，现状以实现为准）

```
飞书 WS → channel → MessageAggregator → dsh-adapter(ACP stdio) → dsh 进程(profile=agentbiz)
                                                                    ├ 会话 chat_id → sessionId 常驻
                                                                    ├ skills = 白名单同步产物
                                                                    └ MCP: feishu / domains / capabilities / order_agent
→ session/update 事件流 → 进度卡 → 答复回飞书
```

feishu-agent-platform 保留：飞书接入、debounce、并发闸、L2 审批、静态扫描、golden 门禁、审计。
交给 dsh：路由、工具循环、上下文压缩、会话持久化、技能热加载。
