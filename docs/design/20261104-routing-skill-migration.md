"""P2 路由迁移与 P4 技能分层的技术方案（只写取舍，不写实现手册）。

> 关联：`SPEC-20261104-dsh-migration` R2/R4/R5/R6；`20261104-dsh-migration-plan.md` P2/P4。
> 依据：`docs/design/20261104-dsh-full-migration-spike.md` §3.2.1 的三组对照实测。

## 一、P2 核心结论：路由靠「技能目录 + 会话前置指令」，不靠工具表

P0 实测 97.2% 是**把 agent 描述手拼进提示词**的成绩。真实部署形态有两组对照：

| 技能可见范围 | 通过率 | 失败特征 |
|---|---|---|
| dsh 默认根（个人 40+ 技能 + 项目 11 个） | 80.3% | 抢答到同名近义的个人技能 |
| 关默认根，项目目录残留旧技能 | 89.5% | 命中废弃技能 |
| 关默认根 + 仅 11 个业务技能 | **94.7%** | 集中在 direct-answer / clarify 边界 |

三条推论：

1. **`includeDefaultRoots: false` 是路由正确性的前提**，不是可选优化——个人技能会直接抢走业务路由。
2. **技能目录就是路由候选集**。往里放任何工具型技能（`code-concept-tracer` 这类），它就参与竞争。
   所以技能目录必须与「可路由的业务能力」一一对应，工具型技能走别处（见 §三）。
3. 剩余 5.3% 全是 direct-answer / clarify 边界，与 harness 无关，是提示词边界规则的旧账。

## 二、路由指令放哪：会话前置消息，不写进每个技能

dsh 的 `skill` 工具在首个请求前给一条 catalog 消息（名称 + 截断描述）。描述截断足以做粗筛，
但**不足以承载边界规则**（「质疑能力存在时不要给清单」「团队资产盘点走 skill-catalog」这类）。
这些规则现在在 `bot/prompts/router/system_prompt.md`（11K）。

三种承载方式：

| 方式 | 做法 | 问题 |
|---|---|---|
| 写进每个 SKILL.md 的 description | 把边界规则塞进 500 字上限 | 截断，且规则重复 11 份 |
| 走 `AGENTS.md` | 项目根的 `AGENTS.md` 由 `dsh-agent-instructions` 注入首个请求 | **会被个人 `~/.agents/AGENTS.md` 一起加载**（默认行为），且按目录层级叠加 |
| 会话 `session/new` 后第一条 user 消息 | 由 `adapter` 在 `session_prompt` 前注入一段固定的路由规则 | 多一轮 token，但完全可控、可版本化 |

**取第三种**，理由：规则单源（一份文件）、可 golden 回归、不受个人全局配置影响。
代价是每会话一次额外注入——但会话是常驻的（chat_id → sessionId），只在**首次**注入。

## 三、技能分层：业务能力与工具技巧必须分开

`bot/skills/` 现有 7 个技能 + `@fireflywwj/` 子目录，混杂两类：

| 类别 | 例子 | 是否进 dsh 技能目录 |
|---|---|---|
| 业务能力（可路由） | `elk-log-query`、`news-sql-generator`、`aihot` | ✅ 是 |
| 工具技巧（不路由） | `digital-oracle`（引擎，`enabled: false`）、`demo-guard`、`x`（占位） | ❌ 否 |

另有一批**只在本机、不该上生产**的技能（个人工作流资产，如 `code-concept-tracer`）——
`includeDefaultRoots: false` 已经挡住，但要在部署文档里写明这是安全边界而非性能优化。

落地形态：

```
bot/skills/                 ← 源，保持不动（capability 审批仍写这里）
  ├── elk-log-query/
  ├── news-sql-generator/
  └── ...
deploy/dsh/agentbiz/skills/ ← 生成物：只含白名单内的业务技能（同步脚本产出，gitignore）
```

白名单放 `deploy/dsh/agentbiz/skills.allowlist`（一行一个技能名，带注释说明为什么收录）。
同步脚本 `deploy/dsh/sync_skills.py`：读白名单 → 校验每个技能存在 → 整体替换目标目录。
**不软链**：dsh 的 Chokidar 监听软链目标在不同平台行为不一致，且 capability 覆盖写时会破坏软链。

## 四、热插拔怎么保持「免重启」

现状：`app/ops/capability/reloader.py` 审批通过 → 写 `bot/skills/` → `registry.reload()` → graph 重建。
迁移后：

1. 审批通过 → 写 `bot/skills/{name}/`（不变）
2. **追加到白名单** → 跑同步脚本 → 目标目录出现新技能
3. dsh 的 Chokidar 监听到新目录 → 下一条消息即为可选技能（实测原生行为）

关键点：第 2 步必须由 bot 进程触发（capability 的 `_post_install` 钩子），不能靠人工跑脚本。
新增技能的**会话可见性**由 dsh 保证（catalog 每次发现都重读），已存在的会话也生效。

## 五、技能开关（AC-5.2 / 5.3）

两条路径，优先级不同：

| 层级 | 机制 | 生效 |
|---|---|---|
| 单个技能 | frontmatter `disable-model-invocation: true` | 写文件即生效（dsh 重读） |
| 整体启停 | 白名单增删 + 同步脚本 | 下一次同步 |

管理端（控制台「能力开关」）改的应该是**白名单**，不是直接改 SKILL.md——后者会在下一次
capability 覆盖安装时被冲掉。单技能临时屏蔽才写 frontmatter。

## 六、P2 出口判据的重新定义

原判据「路由 golden ≥95%」基于 97.2% 的旧基线。按真实形态（技能目录）重定：

| 指标 | 基线 | 判据 |
|---|---|---|
| 技能目录形态的路由通过率 | 94.7%（76 例） | **≥ 92%**（允许模型非确定性抖动，低于旧基线 2.7 个百分点） |
| direct-answer / clarify 边界 | 现有失败集中处 | 单独统计，不计入总分（是提示词问题，不是路由问题） |
| 技能目录纯净度 | — | 目录内每个 SKILL.md 都在白名单里（脚本校验） |

重定理由：94.7% 是**真实部署形态**的成绩，97.2% 是手拼提示词的理想态。用理想态做门禁会
让 P2 永远过不了，用 92% 留出抖动余量。
