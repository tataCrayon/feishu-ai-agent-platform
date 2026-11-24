# 功能简报：日报卡片（daily-card）

> 改这个功能前读这一页即可动手，不必预读实现。与代码矛盾时以代码 + commit 为准并回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：docs/specs/_feature-brief-template.md

## 1. 一句话

用户说「发我今天的日报」时，bot 发一张**结构固定**的卡片：要点头写日期、按 API 板块
分区、每一条标题都是能点开的蓝链——而不是让模型每轮自由发挥成一段文本。

## 2. 主链路

```
dsh 模型（会话内，按 SKILL.md「日报交付」段）
  → MCP 工具 daily_report_card(date_iso="")
      → domains_server：current_chat_id() 取本会话（env 注入，不取模型参数）
      → aihot.get_daily_report()：cache.json 的 report 字段 → API → 落缓存
      → daily/card.build_daily_card()：CardBuilder 组装（header 日期 + 分区 + 条目链接）
      → feishu_send.send_card：tenant token，按 oc_/ou_ 选 receive_id_type
  → 卡片进会话；模型正文只回一句确认
```

失败面：`send_card` 抛错 → 回执 `sent=false` + `push_text`（当日推送版原文），模型发文本兜底；
对端无当日成品 → `available=false`，模型如实说，不换日期顶替。

## 3. 期望行为（可观察面）

- 卡片头部：「技术日报要点 · 2026-11-20」+ 副标题「AI HOT 精选 · 共 N 条」。
- 正文：一句 lead 导读 → 各板块（API 原始顺序）→ 快讯 → 灰色尾注（AI HOT 出处）。
- **每条标题都是 `[标题](链接)`**：优先 `links.aihot` 站内条目页，缺失回落 `links.original`，
  两个都空才纯文本（不拼死链）。
- 板块超过 8 条 / 快讯超过 6 条：写「另有 N 条」，不静默截断。
- 底部「查看完整日报」按钮只在 `report.links.aihot` 存在时出现。

## 4. 约束与不变量（改前必读）

- **结构在代码里，不在提示词里**：模型只能决定「发哪天」。让模型拼卡片 JSON 会踩
  `invalid card content`（同 `docs/features/notify-card.md` 的结论）。
- **顺序不得重排**：sections 顺序就是编辑的意图，不是排行榜；`score` 不参与排序
  （证据：`bot/skills/aihot/SKILL.md` 输出约定、`tests/domains/test_daily_card.py`）。
- **卡片发出后正文不复述要点**：同一条会话里贴两遍是噪音；规则写在
  `bot/prompts/router/dsh_routing_rules.md`「日报」段。
- **缓存三视图同源**：push_text / markdown / report 在同一 cache.json 条目里同生同灭，
  否则卡片会拿旧板块渲今天的日期（证据：`app/domains/daily/store.py::cache_report`）。
- **不改**：不要为卡片另写一条取数链路（会与 `daily_report` 的缓存/降级分叉）；
  不要在卡片里加 `<at>` 或按钮 besides「查看完整日报」。

## 5. 单源与派生物

- 单源：`app/domains/daily/card.py`（渲染）、`app/mcp/domains_server.py::daily_report_card`（出口）、
  `app/domains/daily/aihot.py::get_daily_report`（取数）、`bot/skills/aihot/SKILL.md`（模型侧怎么用）
- 派生物：`deploy/dsh/agentbiz/skills/aihot/`（`python deploy/dsh/sync_skills.py` 生成）

## 6. 证据锚点

- 2026-11-20 · 日报格式漂移（同一天一张纯文本要点卡、一张 markdown 卡片）→ 结构收进代码，
  新增 `card.py` + `daily_report_card` + 路由规则「日报」段 · commit `<待补>`，简报建立
