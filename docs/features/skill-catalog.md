# 功能简报：团队 Skill 目录

> 改这个功能前读这一页即可动手。与代码矛盾时以代码为准，并回写本页。

## 1. 一句话

飞书里问「团队已经有哪些 Skill、各自什么规范状态、我想做的会不会重复、某个 Skill 缺什么」，Bot 用一天一刷的快照回答，并给出该文件所在分支的 Git 链接。规范状态（草稿/生产/未验证/下线候选/已下线）由八项治理字段齐全度 + 复审日期在渲染时推导，Bot 只投影不下结论。

## 2. 主链路

```
飞书问句 → skill-catalog-agent
  → skill_catalog_handler.py::execute_tool
  → CatalogStore.load / refresh
  → CatalogScanner.scan（仅过期或手动刷新时 git fetch）
  → query.py 拼文本（状态由 status.py 按当天日期推导）→ 原样回复
```

## 3. 期望行为

- 正常：回复含快照时间、已扫分支、跳过分支、Git 链接。
- 「有哪些」且没点名分支：只给每支数量，不贴全部 description。
- 「我想做 Nacos 查询」：列出最接近的 1～3 条，不说「可以立项」。
- 「审查 nacos-guard」：缺 owner / 明确不做 / 复审日 / version 就列缺项，不打通过或不通过。
- 「技能建设情况」→ get_skill_status_board 总表：状态计数 + 八项缺口 Top + 近 30 天提交（git log --all，含在飞分支）+ 分支在飞。
- 「XX 技能什么状态」→ get_skill_status 状态卡：五状态之一 + 八项逐项打勾 + 依据句。
- 异常：刷新失败仍用上一份快照，并写明失败。没有快照就说没有，不编造。
- 用户说「刷新 Skill 目录」才强制 fetch。普通查询不 fetch。

## 4. 约束与不变量

- 目录只存 frontmatter 字段，不存正文（2026-09-29 约定：正文是执行细则，会把查重带偏）。
- 同名 Skill 按分支分列，不合并。master 不是唯一事实源。
- 默认跳过 addNewSkill、addNewSkill_v1、addNewSkill_v2、merge_all、init。跳过名单必须出现在回复里。
- Bot 不安装、不写回、不拦截立项。
- git 访问必须清掉代理环境变量（内网 GitLab，与 merge-check 的 trust_env=False 同一类坑，2026-09-17）。
- **不改**：不要把团队仓 Skill 同步进 `bot/skills/`。那是机器人自己的执行包。
- **状态只推导不存储**（2026-09-30）：快照只存事实字段，derive_status 在渲染时按当天日期算——复审到期即使不重扫也自动降级，这是状态变化不是提醒。
- 状态升级只能靠作者补 frontmatter 字段走 MR 合入；Bot 不代填、不代升。
- master 可能长期停更：活动证据用 `git log --all`（2026-09-30 实测 master 停在 2026-05-07，近 30 天提交全在分支上）。
- 旧快照兼容：CatalogSnapshot/Entry.from_dict 对缺新字段的历史 JSON 全部默认空串/空表，不崩、状态回落草稿。

## 4.1 规范状态机（`status.py` 单源）

**人工只填证据字段，状态由 scanner 在渲染时推导**。八项治理字段的键名与判定：

| 项 | frontmatter 键 | 口径 |
|---|---|---|
| 目的 | `description` | 已有，可直接用，不重复存 |
| 明确不做 | `not_do`（模型层 `out_of_scope`） | 边界外的动作 |
| 具名负责人 | `owner` | 写人名，不写团队 |
| 自治上限 | `autonomy` | 自主 / 批准后执行 / 只建议 |
| 允许的工具 | `tools` | 最小集合 |
| 成功标准 | `success_criteria` | 可观察的达成判据 |
| 停止后找谁 | `escalate_to` | 升级人 |
| 下次复审日 | `review_due`（模型层 `review_by`） | 过期即状态变化 |

```
新出现 → 草稿     八项缺任一
       → 生产     八项全 + 复审未到期
       → 未验证   生产但复审已过期（≤90 天，自动降级，无需人操作）
       → 下线候选 过期超一个复审周期（90 天）且无使用证据、无提交产出
       → 已下线   frontmatter status 强制（归档不删除，移出默认上下文）
```

- `status` 不人工填（除强制下线）；复审过期是**状态变化，不是提醒**。
- 「调用记录」Cursor 侧无遥测：先用复审时人工申报 `last_usage_evidence`；
  连续一个周期无证据 → 下线候选。
- 状态升级只能靠作者补 frontmatter 走 MR；Bot 不代填、不代升。

## 5. 单源与派生物

- 单源：`app/domains/skill_catalog/`（status.py = 状态机单源）、`config.toml [projects.skill_catalog]`
- 派生物：`logs/skill_catalog/snapshot.json`（运行时生成，勿手改、勿入库）

## 6. 证据锚点

| 日期 | 事件 | 锚点 |
|---|---|---|
| 2026-09-29 | 团队仓按分支建设，master 停在 2026-05-07；目录必须带分支 | 本地 `git ls-remote` 对照 |
| 2026-09-29 | 查重读 description，不读正文 | 本页 §4 |
| 2026-09-30 | 规范状态层上线：八项字段→五状态机，渲染时推导；94 个 Skill 实测 94 草稿（治理缺口 94/94 缺 owner 等）；状态机 8 例验收 + 旧快照兼容通过 | 本页 §4.1；原始设计稿见 git 历史 `docs/design/20260930-skill-status-bot.md` |

## 7. 已知坑

- 开发机若在 10.35 网段，内网 GitLab 可能不可达。现象是刷新失败，绕法是换网络后再说「刷新 Skill 目录」。旧快照仍可回答。
- `.cursor/skills/` 与 `skills/*/skill.md` 可能是同一能力的两份。目录两份都列，审查时不要当成两个产品。
- 活动证据含在飞分支的提交（--all），展示文案要写「含在飞分支」，别写成 master 提交数。

## 8. 演练记录

| 日期 | 需求 | 未预读实现？ | 一次通过？ | 返工原因 |
|---|---|---|---|---|
| 2026-09-29 | 初建 | 否（本页与实现同批写入） | — | — |
| 2026-09-30 | 规范状态层扩展 | 否（本页 §4/§6 同 commit 更新） | 是 | — |
