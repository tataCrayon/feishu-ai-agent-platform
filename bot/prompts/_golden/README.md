# Golden 测试数据集

> 用于提示词/路由变更门禁的评估数据集。每个 golden 条目是一个「输入 → 期望行为」对。
> 分两层：**静态**（检查提示词是否编码了语义，秒级无网络）+ **行为级**（真实调用
> RouterLLM 断言实际路由选择，约 2 分钟/批）。工程化管理：分套件、有基线、修订留痕。

## 说明

- 目录作为数据集载体（`.json` 文件），由变更门禁脚本读取。
- 数据集与 prompt 的关联由本表的 `ref` 列承载（frontmatter 的 `golden_ref` 字段已于 2026-10-12 移除——双源无消费者）。
- 静态：`python app/eval/prompt_eval.py`；行为级：`python app/eval/router_intent_eval.py`。

## 数据格式

```json
{
  "ref": "shared/safety-rules",
  "version": "1.0.0",
  "cases": [
    {
      "input": "SELECT * FROM t_rwa_product",
      "expected_tool": "route_to_db_agent",
      "expected_behavior": "执行只读查询，限制结果行数",
      "expected": "SQL SELECT 限制行数",
      "history": [{"role": "user", "content": "..."}],
      "tags": ["db", "safe", "v2"],
      "note": "改期望时必填修订原因（审计用）"
    }
  ]
}
```

## 现有套件（2026-09-12 基线：静态 97%，行为级见下）

| 文件 | 层 | ref | 用例数 | 覆盖 |
|------|---|-----|:---:|------|
| `router-intent.json` | **行为级** | router/system_prompt.md | 65 | 11 路由目标正例 + 资产盘点分流 + 模糊→clarify + 否定 + 英文 + 多轮 + 多意图 |
| `router-system.json` | 静态 | router/system_prompt.md | 14 | 8 路由目标 + 歧义消解 |
| `direct-answer.json` | 静态 | router/direct_answer_system_prompt.md | 6 | 自答场景 + 不编造 + 超范围引导 |
| `daily-agent.json` | 静态 | agents/daily-agent.md | 6 | 多源查询 + 先列表再读取 |
| `schedule-agent.json` | 静态 | agents/schedule-agent.md | 12 | 创建红线 + 审批语义 + 口语化目标解析 |
| `skill-catalog-agent.json` | 静态 | agents/skill-catalog-agent.md | 9 | 资产盘点 + 查询/查重/审查 + 规范状态 |
| `merge-check-agent.json` | 静态 | agents/merge-check-agent.md | 5 | branch_type 映射 + 只读边界 |
| `digital-oracle-agent.json` | 静态 | agents/digital-oracle-agent.md | 6 | 概率预测方法论 + 信号菜单 |
| `safety-rules.json` | 静态 | shared/safety-rules | 12 | 权限矩阵 + SQL 拦截 + 脱敏 + 发布安全 |

> `judge-agent.json` / `recover-reroute.json` 已随 judge+recover 节点移除（2026-10-12）。

## 门禁运行

```bash
# 静态层（秒级，无网络；任一套件 <80% 阻断）
python app/eval/prompt_eval.py

# 行为级（真实 LLM，约 2 分钟；<85% 阻断；网络不可达 exit 2）
NO_PROXY="aigw.example.com" python app/eval/router_intent_eval.py
NO_PROXY="aigw.example.com" python app/eval/router_intent_eval.py --case-tags clarify  # 只跑模糊类

# spec 验收（R7：静态 AC + 可选 LLM）
python app/eval/verify_spec.py --with-llm
```

## 维护约定

1. **新增用例先查重**（input 唯一），tags 带 `vN` 批次标注；行为级新用例先人工确认
   期望合理（跑单条观察真实行为）再入库。
2. **改期望必须留痕**：`note` 字段写明修订原因。分歧时先判断是模型错了还是用例错了——
   用例错改用例 + note；模型错修 prompt（并跑双层门禁回归）。
3. **基线记录**：分数与 FAIL 归因写进本文件下方的「已知行为基线」段；行为级分数突降 >5%
   必须归因后才能合入。
4. **领域覆盖矩阵**保持更新（golden 用例的 `tags` 即矩阵来源），每个路由目标
   至少 1 正例；模糊→clarify、英文、多轮、多意图四类专项用例不得为空。

## 已知行为基线（2026-09-12 观察值，供后续回归对比）

- 清晰专业请求 confidence 0.85-0.98；模糊请求 0.3-0.5（低于阈值 0.6 → 降级 clarify）
- 泛 AI 咨询（"XX 模型哪家强"）倾向 dev-bot（agent.yaml AI 咨询域）而非 direct_answer
- AI 行业动态类问题 daily-agent 与 dev-bot 边界模糊，均属合理路由
- 多轮指代含明确实体词（如表名）时路由稳定；纯指代（"刚才那个"）依赖 history 质量
