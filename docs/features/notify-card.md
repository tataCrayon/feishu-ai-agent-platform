# 功能简报：代发通知卡（bot 替人给别人发卡片）

> 改这个功能前读这一页即可动手。与代码矛盾时以代码为准，并回写本页。

## 1. 一句话

用户说「给某某发个通知」，bot 能发一张**结构受控**的卡片给对方，卡片副标题写明
「来自 {真实发送者} 的消息」。

## 2. 主链路

```
dsh 模型（会话内）
  → MCP 工具 send_card_message(chat_id, title, body, from_name?, fields?, note?, template?)
      → feishu_server 校验：chat_id 前缀 / 字段长度 / template 白名单
      → notify_card.build_notify_card：CardBuilder 组装 + <at> 剥除 + 体积上限
      → feishu_send.send_card：tenant token + 按 oc_/ou_ 选 receive_id_type
```

## 3. 期望行为（可观察面）

- 收件人收到一张卡片：头部标题 + 副标题「来自 XXX 的消息」+ 正文 + 若干
  `**字段：** 值` 行 + 脚注（note · 时间）。
- 模型只需要给纯文本，**不能也不该自己拼卡片 JSON**（工具签名里没有 card 参数）。
- 署名取 `PROMATE_CALLER_SENDER_NAME`（会话建立时由 `supervisor._caller_env` 写进
  MCP 子进程 env）；env 缺失才会用模型传的 `from_name`。

## 4. 约束与不变量（改前必读）

- **结构由代码钉死**，不开放 `card_json` 参数。飞书卡片差一处字段就整卡拒收，
  回执只有 `invalid card content`，模型拿着改不动（证据：
  `tests/test_mcp_feishu_card.py::test_bad_args_rejected_with_actionable_reason`）。
- **`<at>` 标签一律剥掉**：lark_md 认 `<at id=all></at>`，留着等于把「@全体」交给
  模型（证据：`test_at_all_is_stripped_from_body_and_fields`）。
- **超长一律拒绝，不截断**：截断出来的卡片看着正常、用户读到半句话。
- **身份取 env 不取参数**：与 `subscribe_cmdb_watch` 同一口径——模型能从对话里「听说」
  一个人名，但卡片上「来自谁」必须是真发了这条消息的人（证据：
  `test_card_header_marks_the_real_sender`）。
- **体积检查是防线不是常规路径**：字数上限（标题 60 / 正文 2000 / 12 字段 × 200）
  已经把最坏合法输入压在 ~14KB，远低于 28KB 上限。若体积拒收真被触发，说明字数
  上限被放开了，`test_worst_case_legal_card_stays_under_the_size_limit` 会先红。

## 5. 单源与派生物

- 单源：`app/protocols/notify_card.py`（校验口径 + 卡片结构）、
  `app/mcp/feishu_server.py::send_card_message`（工具面）、
  `app/protocols/feishu_send.py::send_card`（HTTP 出口）
- 派生物：无

## 6. 证据锚点

| 日期 | 事件（为什么变成现在这样） | 锚点 |
|---|---|---|
| 2026-11-11 | 用户实测「给李四发个消息卡片」，模型如实回「接口只支持文本，卡片发不了」——工具面确实没有卡片出口 → 新增 `send_card_message` | 本次改动（未提交） |

## 7. 已知坑

- 卡片正文不可选中复制（与过程卡同一个代价）。
- 机器人只能发给「它够得着的」会话：不知道 open_id 就先 `chat_members` 从共同群查，
  查不到必须如实说，不许猜 ID。
- `template` 只收飞书 header 配色白名单里的值，写错会在工具层被拒——这是有意为之，
  飞书对非法颜色只回「参数错误」，模型无从修起。
