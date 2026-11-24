# 功能简报：会话上下文与多模态输入（session-context）

> 改这个功能前读这一页即可动手。与代码矛盾时以代码 + commit 为准，并立即回写本页。
> 制度：SPEC-20260924-fresh-loop ｜ 模板：`docs/specs/_feature-brief-template.md`

## 1. 一句话

让模型「看得见群里的来龙去脉、读得懂发来的文件」——用户说「刚才那个」「总结一下讨论」
或直接甩一份 xlsx/pptx/pdf 时，不需要先解释背景、也不需要改格式重发。

## 2. 主链路

```
飞书消息（附件）→ app/main.py::_process_batch
  → app/multimodal.py::download_media_bundle（下载 + 按后缀分流解析）
      → MediaBundle(parts=[image|text|note])
  → app/dsh/gateway.py::handle → app/dsh/media.py::build_prompt（→ ACP 块）
  → dsh 会话：附件正文已在 user 消息里

群历史（问「刚才说的」时由模型主动取）：
  → tool: mcp__feishu__read_conversation（chat_id 省略 = 当前会话）
  → app/mcp/feishu_server.py → app/protocols/feishu_send.py::recent_messages
      （im/v1/messages 倒序取 + 成员名翻译 + 卡片/flatten）
```

## 3. 期望行为（可观察面）

- 发一份 xlsx/docx/pptx/odt/pdf/md/csv/代码文件并问「总结一下」→ 直接读到正文
- 发图片（png/jpg/webp/gif/bmp）→ 模型看图作答（bmp 转 png，动图不缩）
- 「总结一下刚才的讨论」→ 先 `read_conversation` 再答，带时间与说话人
- 「上面那条链接讲了什么」→ `read_conversation` 找到链接 → `read_doc` 读正文
- 解析不了时**明说**原因与下一步（转格式 / 截图 / 分段发），不静默当空文件
- 卡片消息正文是图片渲染时，只报标题并注明「正文看不到」，不编内容

## 4. 约束与不变量（改前必读）

- **附件解析失败必须产生 note**：静默返回空串会让模型以为文件是空的，
  用户看到的是「它说我的表格是空的」——比报错难查十倍
- **截断必须写在正文里**：单份 3 万字 / 单条 6 万字上限，截断处写明「共 N 字，已取前 M」
- **编码顺序 utf-8 → gbk → utf-16 → charset 探测**：只试 utf-8 会漏掉 Windows 导出的
  GBK CSV，用户只能得到「无法解码」这种无从下手的失败
- **二进制要判死**：可执行文件/压缩包能被 utf-8「成功」解码成控制字符，模型会把乱码当正文
- **读会话用的是 bot 自己的可见范围**（tenant token）：不越界读别人的私聊；
  时间戳用飞书给的 `create_time`，不拿本机时钟过滤（机器时钟与飞书可差数小时）
- **附件后缀白名单有两处**：`app/gateway/channel/`（下载闸）与 `app/multimodal._DOC_EXTS`
  （解析闸），口径必须一致——只放行不解析＝「附件被拒」，只解析不放行＝根本收不到
- **`.doc` 依赖系统 antiword**（Git for Windows 自带）；缺失时报「另存为 .docx」，不说 traceback

## 5. 单源与派生物

- 单源：`app/multimodal.py`（解析器与上限）、`app/protocols/feishu_send.py`（读消息/成员/群列表）、
  `bot/prompts/router/dsh_routing_rules.md`（何时该调 `read_conversation`）
- 派生物：`app/dsh/media.py` 的 ACP 块（由 MediaBundle 转换，勿另立解析逻辑）

## 6. 证据锚点

- 2026-11-05 支持格式从 4 类扩到 43 类（xls/pptx/odt/rtf/html + 代码文本），
  加二进制判定与截断说明；`read_conversation` / `chat_members` / `list_chats` 进 feishu MCP
- 2026-11-05 实测：真实群里读最近 10 条并按人/时间总结（`oct_160e1f...`）
- 2026-10-xx 多模态首版（图片 + xlsx/docx/pdf）
