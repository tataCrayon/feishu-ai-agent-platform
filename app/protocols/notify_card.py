"""通知卡片的渲染与参数校验 —— 结构由这里钉死，模型只填纯文本。

## 为什么不让模型自己拼卡片 JSON

飞书卡片是结构化 JSON，字段错一处就整卡发送失败（回执只有一句
`invalid card content`），而模型生成的 JSON 常常差在 `tag` 名、嵌套层级这些
肉眼看不出来的地方；模型拿到「无效」也改不动，用户什么都收不到。这里把结构
交给构造函数，模型只填标题 / 正文 / 字段这些**纯文本**——合法由代码保证，
不由模型自觉保证。

## 身份不能由模型填

`from` 优先取 MCP 子进程 env 里的真实发送者（`PROMATE_CALLER_SENDER_NAME`），
模型传的值只在 env 缺失时兜底。与 `supervisor._caller_env` 同一口径：模型可以
从对话里「听说」一个人名，但卡片上写「来自某人」的必须是真发了这条消息的人。

## 两个附加约束

- **`<at>` 标签一律剥掉**：卡片的 lark_md 认得 `<at id=all></at>`，留着就等于
  把「@所有人」交给模型，一张卡片能惊动整个群。
- **整卡序列化体积设上限**：超了飞书整卡拒收，报错却只说体积问题，用户只会
  看到「发送失败」。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "CARD_TEMPLATES",
    "DEFAULT_TEMPLATE",
    "build_notify_card",
    "check_card_args",
    "sanitize_markdown",
    "sender_of",
]

TZ = timezone(timedelta(hours=8))

# 飞书 header 模板色白名单：写错一个颜色飞书只回「参数错误」，列出来让模型改得动。
CARD_TEMPLATES: frozenset[str] = frozenset({
    "blue", "wathet", "turquoise", "green", "yellow",
    "orange", "red", "carmine", "violet", "purple", "indigo", "grey",
})
DEFAULT_TEMPLATE = "blue"

TITLE_MAX = 60
BODY_MAX = 2000
NOTE_MAX = 300
FIELDS_MAX = 12
LABEL_MAX = 20
VALUE_MAX = 200
# 飞书卡片 JSON 体积上限约 30KB；留余量，超了是整卡拒收而不是截断。
CARD_MAX_BYTES = 28000

_AT_RE = re.compile(r"</?at\b[^>]*>", re.IGNORECASE)


def sanitize_markdown(text: str) -> str:
    """剥掉 `<at>` 标签（@所有人 的唯一入口），其余 markdown 原样保留。"""
    return _AT_RE.sub("", text or "").strip()


def sender_of(env_name: str = "", fallback: str = "") -> str:
    """卡片上「来自谁」：真实发送者优先，模型给的名字只兜底。"""
    clean = (env_name or "").strip()
    if clean:
        return clean
    return (fallback or "").strip() or "AgentMate"


def check_card_args(
    title: str,
    body: str,
    *,
    fields: dict[str, str] | None = None,
    note: str = "",
    template: str = DEFAULT_TEMPLATE,
) -> str:
    """参数校验：返回拒绝文案，空串=通过。

    一律**不静默截断**：截断出来的卡片看上去正常，用户读到的是半句话，
    比直接告诉他「超长了，拆一下」糟得多。
    """
    if not (title or "").strip():
        return "title 不能为空"
    if len(title.strip()) > TITLE_MAX:
        return f"title 长度 {len(title.strip())} 超过上限 {TITLE_MAX} 字，请精简"
    if not (body or "").strip():
        return "body 不能为空"
    if len(body.strip()) > BODY_MAX:
        return f"body 长度 {len(body.strip())} 超过上限 {BODY_MAX} 字，请精简或用 fields 分条"
    if len(note or "") > NOTE_MAX:
        return f"note 长度 {len(note)} 超过上限 {NOTE_MAX} 字"
    if template not in CARD_TEMPLATES:
        return (f"template 取值非法：{template!r}，可选 "
                + " / ".join(sorted(CARD_TEMPLATES)))
    rows = fields or {}
    if len(rows) > FIELDS_MAX:
        return f"fields 最多 {FIELDS_MAX} 行，收到 {len(rows)} 行"
    for label, value in rows.items():
        if not (label or "").strip():
            return "fields 的标签不能为空"
        if len(label.strip()) > LABEL_MAX:
            return f"fields 标签 {label!r} 超过 {LABEL_MAX} 字"
        if len(str(value or "")) > VALUE_MAX:
            return f"fields 值 {label!r} 超过 {VALUE_MAX} 字，长内容请写进 body"
    return ""


def build_notify_card(
    *,
    title: str,
    body: str,
    sender: str,
    fields: dict[str, str] | None = None,
    note: str = "",
    template: str = DEFAULT_TEMPLATE,
    when: datetime | None = None,
) -> dict[str, Any]:
    """渲染一张通知卡：头部标题 + 「来自 XXX 的消息」副标题 + 正文 + 字段 + 脚注。

    调用前必须先过 `check_card_args`——这里不做二次校验，只负责把**已合法的
    参数**组装成结构正确的卡片（`json.dumps` 体积超限时抛 ValueError）。
    """
    from lark_channel import CardBuilder

    stamp = (when or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M")
    b = CardBuilder()
    b.header(_one_line(title, TITLE_MAX), subtitle=f"来自 {sender} 的消息",
             template=template)
    b.markdown(sanitize_markdown(body))
    for label, value in (fields or {}).items():
        b.markdown(f"**{label.strip()}：** {sanitize_markdown(str(value))}")
    footer = f"{sanitize_markdown(note)} · {stamp}" if note.strip() else stamp
    b.footer(footer)

    card = b.to_dict()
    size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    if size > CARD_MAX_BYTES:
        raise ValueError(f"卡片体积 {size} 字节超过上限 {CARD_MAX_BYTES}，请精简正文")
    return card


def _one_line(text: str, limit: int) -> str:
    """标题必须单行：换行会把 header 顶成两行，飞书按第一个换行截断。"""
    clean = " ".join(sanitize_markdown(text).split())
    return clean if len(clean) <= limit else clean[:limit] + "…"
