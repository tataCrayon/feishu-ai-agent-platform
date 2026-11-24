# Prompt 索引

> 所有系统提示词集中于此目录，版本与变更可追溯。
> **单源是各 prompt 文件的 frontmatter**；下面的表格是派生物，由脚本生成。

<!-- BEGIN:generated -->

> **本区由 `python app/eval/prompt_index.py` 生成，勿手工编辑表格。**
> 单源 = 各 prompt 的 frontmatter（version / description / depends_on / token_estimate）。
> 改 prompt 后运行该脚本；`prompt_eval.py` 会校验本区是否与 frontmatter 一致。

## 路由引擎（router/）

> 编排层运行时加载，路径由 `PromptLoader.load_router_*` 硬引用

| 文件 | 版本 | Token | depends_on | 用途 |
|------|------|-------|------------|------|
| `router/decompose_system_prompt.md` | v1.1.0 | ~630 | `shared/output-format@1.1.0` | 复杂任务分解引擎提示词 — 将多步骤请求拆解为有序子任务（子 Agent 清单由 Registry 单源注入） |
| `router/direct_answer_system_prompt.md` | v1.5.0 | ~450 | `shared/persona@1.0.0`<br>`shared/output-format@1.1.0` | 主编排 Agent 无合适专业 Agent 时的自答提示词 — 路由未命中时的诚实自答终态，注入多轮历史 |
| `router/dsh_routing_rules.md` | v2.4.0 | ~1480 | — | dsh 会话的系统提示词（personaSuffix 注入）— 规定能力边界、分派与澄清判据、代发消息（卡片/文本两条出口与署名口径）、日报卡片出口、读会话上下文与附件处理、取数与收尾纪律。不含「首行能力名」契约（v2.0.0 移除，全仓无消费方） |
| `router/system_prompt.md` | v1.16.0 | ~2200 | `shared/output-format@1.1.0` | 意图识别路由系统提示词（ManageAgent）— 单意图路由 + 多意图并行分派（多次 tool_calls）+ 自答兜底；置信度自报 + 意图澄清反问 + 意图预判；能力盘点双出口（自我介绍→help / 团队资产盘点→skill-catalog） |

## Agent 角色提示词（agents/）

> 由 `agent.yaml` 的 `prompt_ref` 单源引用

| 文件 | 版本 | Token | depends_on | 用途 |
|------|------|-------|------------|------|
| `agents/dev-bot.md` | v2.13.0 | ~2280 | `shared/safety-rules@1.0.0`<br>`shared/output-format@1.1.0`<br>`shared/persona@1.0.0`<br>`shared/self-check@1.0.0` | dev-bot 主业务 Agent 的完整系统提示词 |
| `agents/digital-oracle-agent.md` | v1.2.0 | ~900 | `shared/safety-rules@1.0.0`<br>`shared/output-format@1.1.0`<br>`shared/persona@1.0.0`<br>`shared/self-check@1.0.0` | digital-oracle-agent 角色提示词 — 只用市场交易数据做概率预测与信号交叉验证 （引擎 vendoring 自 komako-workshop/digital-oracle，MIT） |
| `agents/schedule-agent.md` | v1.6.0 | ~600 | `shared/output-format@1.1.0`<br>`shared/persona@1.0.0`<br>`shared/self-check@1.0.0` | schedule-agent 角色 prompt — 定时任务创建（评估通过即生效）、生命周期操作与查询 |
| `agents/skill-catalog-agent.md` | v1.3.0 | ~620 | `shared/output-format@1.1.0`<br>`shared/persona@1.0.0`<br>`shared/self-check@1.0.0` | skill-catalog-agent 角色 prompt — 只读团队 Skill 目录，资产盘点、查询、查重、审查、规范状态 |

## 回复模板（replies/）

| 文件 | 版本 | Token | depends_on | 用途 |
|------|------|-------|------------|------|
| `replies/help_reply.md` | v2.3.0 | ~320 | — | help 帮助清单回复文本（备用模板，运行时由 _help_node 动态生成） |

## 公共基座（shared/）

> 经 `depends_on`（带 `@版本` pin）注入到引用方正文末尾

| 文件 | 版本 | 用途 |
|------|------|------|
| `shared/output-format.md` | v1.1.0 | 公共输出格式约束 — 按意图分级的篇幅 + 表格优先 + 表情动态化 |
| `shared/persona.md` | v1.0.0 | 公共人格基座 — 靠谱同事人设：平级、直接、结论先行 + 禁语清单 |
| `shared/safety-rules.md` | v1.1.0 | 公共安全规则 — 权限矩阵 + 写操作确认 + 脱敏要求 + 审计 |
| `shared/self-check.md` | v1.0.0 | 执行类 Agent 输出前的自评要求 — 末尾产出结构化 QC 标记，编排层只观测不裁决 |

## Golden 测试数据集

`_golden/` — 提示词变更门禁的评估数据集。`prompt_eval.py`（静态 + 结构，score < 80% 阻断）与 `router_intent_eval.py`（行为级，score < 85% 阻断）双层门禁。

| 套件 | 层 | ref | 用例数 | 数据集版本 |
|------|---|-----|:---:|------|
| `digital-oracle-agent.json` | 静态 | agents/digital-oracle-agent.md | 6 | 1.0.0 |
| `direct-answer.json` | 静态 | router/direct_answer_system_prompt.md | 6 | 1.0.0 |
| `router-intent.json` | 行为级 | router/system_prompt.md | 31 | 3.0 |
| `router-system.json` | 静态 | router/system_prompt.md | 3 | 1.2.0 |
| `safety-rules.json` | 静态 | shared/safety-rules | 12 | 2.1 |
| `schedule-agent.json` | 静态 | agents/schedule-agent.md | 12 | 1.2.0 |
| `skill-catalog-agent.json` | 静态 | agents/skill-catalog-agent.md | 9 | 1.1.0 |

> 共 7 个套件。

<!-- END:generated -->
## 加载机制

使用 `app/prompts/loader.py` 的 `PromptLoader` 类加载所有提示词。

```python
from app.prompts.loader import PromptLoader

# 运行时真源（单文件，历史版本由 git 承载）
router_prompt = PromptLoader.load_router_system_prompt()   # router/system_prompt.md
decompose = PromptLoader.load_router_decompose_prompt()    # router/decompose_system_prompt.md

# 结构化加载：path + frontmatter 元数据 + content_hash
artifact = PromptLoader.load_artifact("shared/safety-rules")
# artifact.meta.version == "1.0.0"
# artifact.content_hash == 正文 SHA-256 前 16 位

# 公共基座注入：depends_on 带 `@版本` pin，运行时拼到正文末尾

# 缓存清除
PromptLoader.invalidate()                 # 全量
PromptLoader.invalidate("shared/persona") # 局部
```

## 版本管理规范

- **major**：结构性变更，破坏缓存前缀 → 需触发缓存预热
- **minor**：新增指令，不影响结构 → 缓存前缀不变
- **patch**：措辞调整，不改变语义 → 缓存前缀不变

改版本号时**同时**更新该文件的 `changelog:`（首条 `version`/`date`/`change` 与
`updated:` 保持一致），再跑一次 `python app/eval/prompt_index.py` 刷新本文件。

## 依赖 pin 规范

`depends_on` 必须带版本（`shared/output-format@1.0.0`），而不是裸名。运行时
`PromptLoader._inject_dependencies()` 会比对 pin 与文件实际版本，漂移则告警——
单文件模式下没有可切换的旧版本，pin 的价值在于**让漂移在日志里可见**。

## 门禁

```bash
python app/eval/prompt_index.py --check   # INDEX 与 frontmatter 是否一致
python app/eval/prompt_eval.py            # golden 套件，score < 80% 阻断
python app/eval/router_intent_eval.py     # 行为级回归，score < 85% 阻断
```
