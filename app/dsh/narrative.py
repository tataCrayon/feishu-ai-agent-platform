"""进度卡文案 — 工具事件 → 用户读得懂的「正在做什么」。

## 为什么单独一层

dsh 的 `tool_call` 帧里字段就三样：`title`（工具名）、`kind`、`status`。原来直接
把 `title` 贴到进度卡上，于是用户看到的是：

    获取市场信号 之前的形态：⚙️ mcp__capabilities__gather_signals
    ❌ mcp__capabilities__gather_signals
    ⚙️ web_fetch

这既暴露了内部实现名，又完全看不出「在干什么、为什么重试」。参考 dsh 自己的
会话界面：它展示的是**动作意图**（「正在检索资料」「正在分析数据」），失败时才
补一句原因。进度卡是给用户看的过程感，不是给开发看的调用栈。

## 分档判据

1. 显式映射（`_TOOLS`）：知道名字的工具给确定文案，这是绝大多数。
2. 尾段匹配（`_BY_TAIL`）：`mcp__<server>__<tool>` 的 server 名会随挂载方式变，
   只有尾段稳定，换了前缀也能说人话。
3. `kind` 兜底：dsh 会给 `read/edit/execute/fetch/search/think` 等类别。
4. 原文兜底：真不认识就退回 title，至少能定位——**不编造**（把 grep 说成
   「查询数据库」比显示 grep 更糟）。

失败不换句：同一动作保持同一措辞，只在状态上标 ❌ 并追加原因（「超时未响应」），
否则用户会以为模型换了件事做。
"""

from __future__ import annotations

# 工具名（title）→ 进行中的动作文案。键用 dsh 原始名，包含内置工具与 MCP 投影名。
_TOOLS: dict[str, str] = {
    # 内置
    "skill": "加载能力说明",
    "read": "读取资料",
    "write": "写入结果",
    "edit": "修改内容",
    "glob": "查找文件",
    "grep": "检索内容",
    "pwsh": "执行本机命令",
    "bash": "执行本机命令",
    "web_fetch": "抓取网页/接口",
    "web_search": "检索公开信息",
    "create_goal": "拆解为长任务",
    # 能力面（MCP capabilities）
    "mcp__capabilities__gather_signals": "获取市场信号",
    "mcp__capabilities__create_scheduled_task": "创建定时任务",
    "mcp__capabilities__list_tasks": "查看定时任务",
    "mcp__capabilities__cancel_task": "取消定时任务",
    "mcp__capabilities__pause_task": "暂停定时任务",
    "mcp__capabilities__resume_task": "恢复定时任务",
    "mcp__capabilities__get_task": "查看任务详情",
    "mcp__capabilities__list_team_skills": "读取团队能力目录",
    "mcp__capabilities__get_skill_status_board": "读取能力状态板",
    "mcp__capabilities__get_skill_status": "查询能力详情",
    "mcp__capabilities__find_similar_skills": "比对已有能力",
    "mcp__capabilities__review_skill": "评审能力说明",
    "mcp__capabilities__refresh_skill_catalog": "刷新能力目录",
    # 能力面（MCP feishu / domains）
    "mcp__feishu__read_conversation": "读取会话上下文",
    "mcp__feishu__read_doc": "读取飞书文档",
    "mcp__feishu__send_message": "发送消息",
    "mcp__feishu__reply_message": "回复消息",
    "mcp__feishu__chat_members": "读取群成员",
    "mcp__domains__skill_catalog_query": "查询能力目录",
    "mcp__domains__daily_report": "读取日报",
    "mcp__domains__list_schedules": "查看定时任务",
}

# 尾段索引：`mcp__<server>__<tool>` 的 server 名可变（换前缀、裸名调用），
# 只有尾段稳定。同名工具并存时以先登记者为准——文案相同，谁挂的都是同一件事。
_BY_TAIL: dict[str, str] = {}
for _full, _label in _TOOLS.items():
    _BY_TAIL.setdefault(_full.rsplit("__", 1)[-1], _label)

# dsh 的 tool kind → 动作文案（没登记名字时按类别说）
_KINDS: dict[str, str] = {
    "read": "读取资料",
    "edit": "修改内容",
    "execute": "执行命令",
    "fetch": "获取外部数据",
    "search": "检索信息",
    "think": "分析判断",
}

# 失败原因归一：原始报错对用户没有意义，只保留可行动的那几类
_FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    ("timed out", "超时未响应"),
    ("timeout", "超时未响应"),
    ("rate limit", "被数据源限流"),
    ("too many requests", "被数据源限流"),
    ("not found", "没找到对应内容"),
    ("permission", "没有访问权限"),
    ("unauthorized", "需要重新登录"),
    ("connection", "连接不上"),
    ("unreachable", "连接不上"),
)


def action_of(tool_name: str, kind: str = "") -> str:
    """工具名 → 动作文案（不含状态，供「进行中/已完成」共用同一措辞）。"""
    name = (tool_name or "").strip()
    if name in _TOOLS:
        return _TOOLS[name]
    if name in _BY_TAIL:
        return _BY_TAIL[name]
    if name.startswith("mcp__"):
        tail = name.rsplit("__", 1)[-1]
        if tail in _BY_TAIL:
            return _BY_TAIL[tail]
    if kind in _KINDS:
        return _KINDS[kind]
    return name or "处理中"


def failure_hint(output: str) -> str:
    """工具返回里的失败原文 → 一句人话；认不出返回空串（不加噪音）。"""
    text = (output or "").lower()
    for marker, hint in _FAILURE_HINTS:
        if marker in text:
            return hint
    return ""
