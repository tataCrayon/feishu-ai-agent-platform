## 2026-11-24

### 定时任务口径纠偏：删掉「须管理员审批」残句（`router/dsh_routing_rules.md` v2.2.0 → v2.3.0，`router/system_prompt.md` v1.14.0 → v1.15.0，`schedule-agent/agent.yaml` v1.0.0 → v1.1.0）

- **背景**：审批门 2026-11-08 已下线（`agents/schedule-agent.md` v1.6.0 有记录，代码侧
  `store.new_task` 直接落 `approved/`、`cards.py` 无按钮）。但**路由期**的两份文本漏改，
  仍写着「须管理员审批」——描述是模型判断「这是什么能力」的唯一输入，写着审批它就对用户
  说「等审批」，与真实行为矛盾。
- **改动**：`dsh_routing_rules.md` / `system_prompt.md` 的定时任务域改为「评估通过即生效，
  无需审批」；`schedule-agent/agent.yaml` 的 `description` 同步（它同时是 `/help` 能力清单
  与路由工具的 description）。
- **顺带补边界**：`dsh_routing_rules.md` 写明一次性提醒不在能力面内（宿主只支持
  `every` / `daily` 两种周期），要求如实说明、不假装已排上——此前「30 分钟后提醒我」
  既没有能力承接、也没有一句口径告诉模型该怎么答。
- **golden**：`_golden/schedule-agent.json` 两条 `expected_behavior` 去掉审批表述
  （该字段只提取函数名作关键字，不影响判分，但留着会误导下一个读者）。
- 门禁：`prompt_eval` / `prompt_index --check`。

## 2026-11-25 — 公开版收敛

- 去掉本仓不带的业务能力相关条目（订制业务域、内部案例与实测细节），保留与
  框架本身有关的历史：注入方式、能力契约、门禁口径、卡片渲染。
- 提示词当前状态见 `bot/prompts/INDEX.md`；逐文件历史见各自 frontmatter 的
  `changelog` 段，完整历史见 git log。
