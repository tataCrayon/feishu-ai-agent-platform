"""PM 意图路由 — LLM 驱动的语义分流与结构化抽取

存在的理由
----------
`pm_daemon.process_user_query` 原本是 5 个 `if keyword in text` 串成的链。
措辞差一个字就走到完全不同的分支：

    你: "今日：1、私募周报；2、story排期"     → 不含"计划"→ 含"排期"→ 吐整篇晨报
    你: "同步日计划：1、私募周报；2、story排期" → 含"计划"→ 走切词 → 录出"同步日计划："这条空壳待办

用规则冒充理解，是"不智能"的根源。本模块只做一件事：
把一句自然语言，变成确定性的 (意图, 参数) 结构。

    text ──► route_intent() ──► IntentResult ──► dispatcher ──► handler

三条设计约束
------------
1. **LLM 只负责抽取，不负责生成业务数据。** todos 必须是用户真实说过的内容，
   prompt 中明令禁止编造。LLM 的输出是「理解结果」，不是「交付物」。
2. **不可用时降级，不静默。** LLM 超时/解析失败 → 回落正则路由，
   并置 fallback_used=True，让上层在回复里标注，避免用户以为是新逻辑在跑。
3. **不信任 LLM 输出直接入库。** 抽取结果必须过 normalize_todos()：
   去指令壳、去序号、去重、长度校验。这是最后一道闸。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger("pm_hub.intent_router")

# ── LLM 网关（OpenAI 兼容）────────────────────────────────────────────
# 凭据只放 .env（项目铁律）：`LLM_API_KEY` 是模型三要素的单源，
# `AIGW_API_KEY` 是 dsh 执行面用的同一个网关 key，这里作为后备。
LLM_BASE_URL = "https://aigw.example.com/v1/chat/completions"
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("AIGW_API_KEY", "")
LLM_MODEL = "glm-5.1"

ROUTE_TIMEOUT_SEC = 20
CHAT_TIMEOUT_SEC = 30


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

# SOP V2.3 七道闸门。gate 只允许取这些值或 None，不接受 LLM 自由发挥。
SOP_GATES: tuple[str, ...] = (
    "G1 需求冻结",
    "G2 方案评审",
    "G3 承接承诺",
    "G4 联调就绪",
    "G5 提测准入",
    "G6 代码冻结",
    "G7 发版投产",
)

# 意图枚举。命名用业务动作，不用技术术语。
INTENT_LOG_PLAN = "log_daily_plan"        # 录入今日/明日工作计划
INTENT_UPDATE_STATUS = "update_todo_status"  # 更新某条事项的状态
INTENT_DRAFT_NUDGE = "draft_nudge"        # 起草发给别人的催办话术
INTENT_QUERY_SCHEDULE = "query_schedule"  # 查排期/闸门/倒计时
INTENT_QUERY_OWNER = "query_owner"        # 找谁负责/AB角
INTENT_QUERY_KB = "query_knowledge"       # 查 SOP/流程/踩坑
INTENT_ESTIMATE = "estimate_effort"       # 工时评估
INTENT_CHAT = "chitchat"                  # 其他/闲聊/直接问

VALID_INTENTS = frozenset({
    INTENT_LOG_PLAN, INTENT_UPDATE_STATUS, INTENT_DRAFT_NUDGE, INTENT_QUERY_SCHEDULE,
    INTENT_QUERY_OWNER, INTENT_QUERY_KB, INTENT_ESTIMATE, INTENT_CHAT,
})

# 事项状态只允许这四个，和 sht001 表里的状态列对齐
STATUS_VALUES: tuple[str, ...] = ("未开始", "进行中", "已完成", "阻塞")


@dataclass
class TodoItem:
    """单条已归一化的待办。

    gate 为 None 表示这条待办与 SOP 闸门无关（如"私募周报"），
    渲染时不应出现任何方括号前缀。
    """
    title: str
    date_hint: str = "today"           # today | tomorrow | YYYY-MM-DD
    gate: Optional[str] = None
    note: Optional[str] = None


@dataclass
class IntentResult:
    """路由结果。上层 dispatcher 只依赖这个结构，不再碰原始文本。"""
    intent: str
    confidence: float = 1.0
    todos: list[TodoItem] = field(default_factory=list)
    query: Optional[str] = None        # 查询类意图的检索词
    target: Optional[str] = None       # update_todo_status: 第几条或事项名
    status: Optional[str] = None       # update_todo_status: 目标状态
    reasoning: str = ""                # LLM 自述理由，仅用于日志排障
    fallback_used: bool = False        # True 表示走了正则兜底


# ══════════════════════════════════════════════════════════════════════
# 底层 LLM 调用
# ══════════════════════════════════════════════════════════════════════

def _post_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    timeout: int = ROUTE_TIMEOUT_SEC,
) -> Optional[str]:
    """调用 LLM 网关，返回纯文本内容；任何失败返回 None（不抛异常）。"""
    try:
        resp = requests.post(
            LLM_BASE_URL,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning("LLM 网关非 200: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("LLM 调用失败: %s", e)
        return None


def call_llm_text(
    system_prompt: str, user_msg: str, timeout: int = CHAT_TIMEOUT_SEC
) -> Optional[str]:
    """通用问答（供 daemon 的闲聊/兜底分支复用）。失败返回 None，由上层给降级话术。

    timeout 可覆盖：批量生成（比如一次把一天的消息写成待办）输出长、耗时远超
    单轮问答，用默认 30s 会稳定超时 —— 那等于每次过审都退回原文。
    """
    return _post_chat(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": user_msg}],
        temperature=0.2,
        timeout=timeout,
    )


def _extract_json(raw: str) -> Optional[dict]:
    """从 LLM 输出里抠出 JSON 对象。

    网关偶尔会包 markdown 围栏或在前后加解释，所以不能直接 json.loads。
    策略：先剥围栏，再取第一个 '{' 到最后一个 '}'。
    """
    if not raw:
        return None
    text = raw.strip()
    # 剥 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 取最外层花括号
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败: %s | raw=%s", e, text[:300])
        return None


# ══════════════════════════════════════════════════════════════════════
# 归一化：不信任 LLM 输出
# ══════════════════════════════════════════════════════════════════════

# 「指令壳」——用户用来下达指令的包装语，不是待办本身。
# 例："同步日计划：" / "今日计划," / "以下是我的待办" / "记一下"
_SHELL_PATTERN = re.compile(
    r"^\s*(?:请|帮我|麻烦)?\s*"
    r"(?:同步|录入|记录|更新|添加|新增|记一下|登记)?\s*"
    r"(?:今日|今天|明日|明天|本日|本周|这周)?\s*"
    r"(?:的)?\s*"
    r"(?:日计划|工作计划|计划|待办|待办事项|日清|工作|如下|以下)\s*"
    r"(?:清单|列表)?\s*[:：,，。]?\s*$"
)

# 序号前缀："1、" "2." "①" "- " "• "
_INDEX_PREFIX = re.compile(r"^\s*(?:\d{1,2}\s*[、,.)）:：]|[①-⑳]|[-*•·])\s*")

# 结尾口语尾巴："暂时主要工作是这四项"
_TAIL_NOISE = re.compile(r"(?:，|,)?\s*(?:暂时|目前)?\s*主要工作(?:是|就是)?.*$")


def _clean_title(text: str) -> str:
    """清洗单条待办标题：去序号、去尾部口语、压空白。"""
    s = str(text or "").strip()
    s = _INDEX_PREFIX.sub("", s)
    s = _TAIL_NOISE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" ，,。；;：:")
    return s


def _normalize_gate(gate: Any) -> Optional[str]:
    """gate 只接受 SOP_GATES 中的值，其余一律 None。

    LLM 有时会返回 "提测"、"G5" 这类简写，做一次宽松匹配。
    """
    if not gate or not isinstance(gate, str):
        return None
    g = gate.strip()
    if not g or g.lower() in {"none", "null", "无", "n/a"}:
        return None
    # 精确命中
    if g in SOP_GATES:
        return g
    # 宽松匹配：取 "G5" 编号，或按中文名关键词对齐
    m = re.match(r"^(G[1-7])", g, re.I)
    if m:
        code = m.group(1).upper()
        for full in SOP_GATES:
            if full.startswith(code):
                return full
    for full in SOP_GATES:
        if full.split(" ", 1)[-1] in g:
            return full
    return None


def _normalize_status(raw: Any) -> Optional[str]:
    """状态只接受四个值。LLM 有时给"做完了""卡住了"，做一次宽松归并。"""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s.lower() in {"none", "null", "无", "n/a"}:
        return None
    if s in STATUS_VALUES:
        return s
    if "完成" in s or "做完" in s:
        return "已完成"
    if "阻塞" in s or "卡" in s:
        return "阻塞"
    if "进行" in s:
        return "进行中"
    if "未开始" in s:
        return "未开始"
    return None


def normalize_todos(raw_items: Any) -> list[TodoItem]:
    """把 LLM 返回的 todos 数组归一化成可信的 TodoItem 列表。

    这一步专门解决历史缺陷：`同步日计划：` 被当成一条待办入库。
    规则：去壳 → 去序号 → 长度校验 → 按标题去重。
    """
    if not isinstance(raw_items, list):
        return []

    out: list[TodoItem] = []
    seen: set[str] = set()

    for raw in raw_items:
        if isinstance(raw, str):
            title, gate, date_hint, note = raw, None, "today", None
        elif isinstance(raw, dict):
            title = raw.get("title") or raw.get("content") or ""
            gate = _normalize_gate(raw.get("gate"))
            date_hint = str(raw.get("date_hint") or "today").strip() or "today"
            note = raw.get("note")
        else:
            continue

        cleaned = _clean_title(title)
        # 空壳 / 纯指令语 过滤
        if not cleaned or _SHELL_PATTERN.match(cleaned):
            continue
        if len(cleaned) < 2:
            continue
        # 去重（保留首次出现的 gate/date 信息）
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)

        out.append(TodoItem(
            title=cleaned,
            date_hint=date_hint,
            gate=gate,
            note=str(note).strip() if note else None,
        ))

    return out


# ══════════════════════════════════════════════════════════════════════
# 路由 Prompt
# ══════════════════════════════════════════════════════════════════════

_ROUTER_SYSTEM_PROMPT = """你是示例科技 PMO 的意图路由器。你的唯一职责是把用户的一句话解析成结构化 JSON。

## 输出格式（严格遵守，只输出 JSON，不要 markdown 围栏，不要任何解释）

{
  "intent": "<意图>",
  "confidence": <0.0-1.0>,
  "todos": [{"title": "<待办>", "date_hint": "today|tomorrow|YYYY-MM-DD", "gate": "<闸门>|null", "note": null}],
  "query": "<检索词>|null",
  "target": "<第几条或事项名>|null",
  "status": "未开始|进行中|已完成|阻塞|null",
  "reasoning": "<一句话说明为什么这么归类>"
}

## intent 只能取以下值之一

- log_daily_plan      用户在告知/同步自己今天或明天要做的具体工作
- update_todo_status  用户在更新自己某条事项的完成情况（做完了 / 卡住了 / 开始了）
- draft_nudge         用户要机器人代笔写一条发给别人的催办/跟进话术
- query_schedule      用户在问版本排期、上线日、距上线几天、当前闸门
- query_owner         用户在问某模块/需求由谁负责、AB角是谁
- query_knowledge     用户在查 SOP、流程、规范、踩坑记录
- estimate_effort     用户要评估需求工时/人天
- chitchat            以上都不是（打招呼、闲聊、提问其他事）

## update_todo_status 的抽取规则

- `target` 是用户指代哪一条：可能是序号（"3"、"第3条"），也可能是事项名（"私募周报"）。
  原样保留用户说的，不要替用户补全成完整标题。
- `status` 只能是四个值之一。映射参考：
    完成 / 做完了 / 搞定 / 结束 / 已上线  → 已完成
    开始 / 在做 / 进行中                → 进行中
    卡住 / 阻塞 / 等外部 / 推不动        → 阻塞
    还没动 / 待开始 / 明天做            → 未开始
- 这个意图下 `todos` 必须是空数组，不要顺手把事项当新待办录进去。

## draft_nudge 的抽取规则

用户想让机器人**替自己给某个人发消息**时判为这个意图，典型说法：
"催一下 10.29"、"帮我问问进哥提测没"、"写个话术催测试"、"催办"。
`query` 填用户提到的范围限定（版本号/人名/需求名），没提就是 null。
这个意图下 `todos` 必须是空数组。
注意区分：用户说"X 完成了"是 update_todo_status，说"帮我催 X"才是 draft_nudge。

## todos 抽取铁律

1. **只抽用户真实说过的具体工作项。禁止编造、禁止补全、禁止推测。**
2. **剔除指令壳。** 像"同步日计划"、"今日计划"、"以下是我的待办"、"记一下"这类
   是下达指令的包装语，本身不是工作内容，绝对不能作为一条 todo。
3. **拆到原子级。** 一句话里用序号/分号/逗号并列的多件事，每一件独立成条。
4. **保留业务原名。** 不要改写成更"规范"的说法。"story排期"就是"story排期"。
5. 若不是 log_daily_plan 意图，todos 必须是空数组 []。

## gate 填写规则

gate 只在待办**明确涉及**质量闸门动作时才填，否则必须是 null。
可填值只有：G1 需求冻结 / G2 方案评审 / G3 承接承诺 / G4 联调就绪 / G5 提测准入 / G6 代码冻结 / G7 发版投产

判定参考：
- 提测/转测/送测/自测 → G5 提测准入
- 联调/接口对接/前后端对接 → G4 联调就绪
- 上线/发版/投产/灰度 → G7 发版投产
- 需求评审/PRD 确认 → G2 方案评审
- 周报整理、会议、客户沟通、写文档、排期梳理 → **null**（这些不是闸门动作）

## 示例（真实历史 case，务必对齐）

输入：同步日计划：1、私募周报；2、story排期
输出：{"intent":"log_daily_plan","confidence":0.97,"todos":[{"title":"私募周报","date_hint":"today","gate":null,"note":null},{"title":"story排期","date_hint":"today","gate":null,"note":null}],"query":null,"reasoning":"用户同步今日工作，两项均为非闸门的常规事项"}

输入：今日计划，1、与华夏银行客户对接上线生产事项；2、私募周报整理；3、Fundamental客户路演；4、私募需求评审  暂时主要工作是这四项
输出：{"intent":"log_daily_plan","confidence":0.96,"todos":[{"title":"与华夏银行客户对接上线生产事项","date_hint":"today","gate":"G7 发版投产","note":null},{"title":"私募周报整理","date_hint":"today","gate":null,"note":null},{"title":"Fundamental客户路演","date_hint":"today","gate":null,"note":null},{"title":"私募需求评审","date_hint":"today","gate":"G2 方案评审","note":null}],"query":null,"reasoning":"四项为今日工作，其中上线与需求评审涉及闸门"}

输入：10.29 还剩几天
输出：{"intent":"query_schedule","confidence":0.98,"todos":[],"query":"10.29","reasoning":"询问版本倒计时"}

输入：3 完成了
输出：{"intent":"update_todo_status","confidence":0.96,"todos":[],"query":null,"target":"3","status":"已完成","reasoning":"用户在标记第3条事项已完成"}

输入：私募周报阻塞了，等银行回复
输出：{"intent":"update_todo_status","confidence":0.94,"todos":[],"query":null,"target":"私募周报","status":"阻塞","reasoning":"用户在告知某事项被阻塞"}

输入：帮我催一下 10.29 还没提测的
输出：{"intent":"draft_nudge","confidence":0.95,"todos":[],"query":"10.29","reasoning":"用户要机器人代笔催办，限定 10.29 版本"}

输入：写个话术问问进哥提测没
输出：{"intent":"draft_nudge","confidence":0.93,"todos":[],"query":"冷浪进","reasoning":"用户要代笔向进哥催提测"}

输入：WebPortal 前端找谁
输出：{"intent":"query_owner","confidence":0.95,"todos":[],"query":"WebPortal 前端","reasoning":"询问模块负责人"}

输入：明天要提测公募1029的基金搜索
输出：{"intent":"log_daily_plan","confidence":0.93,"todos":[{"title":"公募1029基金搜索提测","date_hint":"tomorrow","gate":"G5 提测准入","note":null}],"query":null,"reasoning":"告知明日计划且明确涉及提测"}
"""


def _build_user_content(text: str) -> str:
    """拼装用户输入。附带当前日期，让 LLM 能正确解析"明天/下周一"。"""
    from datetime import date

    return f"今天日期：{date.today().isoformat()}\n用户消息：{text}"


# ══════════════════════════════════════════════════════════════════════
# 正则降级路由（LLM 不可用时的兜底，保持原有能力不失联）
# ══════════════════════════════════════════════════════════════════════

_FALLBACK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (INTENT_LOG_PLAN, ("待办", "计划", "今天做", "今日工作", "明日工作", "明天做")),
    (INTENT_DRAFT_NUDGE, ("催办", "催一下", "催下", "帮我催", "话术", "问一下进展", "提测了嘛")),
    (INTENT_ESTIMATE, ("评估", "工时", "人天")),
    (INTENT_QUERY_OWNER, ("找谁", "负责人", "负责什么", "AB角")),
    (INTENT_QUERY_KB, ("SOP", "规范", "流程", "踩坑", "最佳实践")),
    (INTENT_QUERY_SCHEDULE, ("排期", "雷达", "版本", "上线日", "闸门")),
)

# 状态更新的口语信号，按优先级排列（阻塞优先于进行中，避免"卡在进行中"被误判）
_STATUS_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"阻塞|卡住|卡了|推不动|等外部|被挡", "阻塞"),
    (r"完成|做完|搞定|已完|结束|上线了", "已完成"),
    (r"开始|在做|进行中|推进中|着手", "进行中"),
)

# 「今2」是晨报「今日计划」块的条目编号，机器人自己发出去的格式，
# 属于协议而不是语义。所以这条不进 LLM：LLM 很可能把它归一成 target="2"，
# 于是去 sht001 周计划里找第 2 条 —— 改错表。
_PLANNED_REF = re.compile(r"今\s*(\d{1,2})")

# 同一句话里出现这些动词，说明他要的是「催人」而不是「改自己状态」
_NUDGE_HINTS = ("催", "问一下", "问问", "发消息", "话术", "提醒")


def _planned_status_shortcut(text: str) -> Optional[IntentResult]:
    """「今2 完成」「今2 今3 阻塞」→ 直接判为日计划状态更新。"""
    if not _PLANNED_REF.search(text):
        return None
    if any(h in text for h in _NUDGE_HINTS):
        return None

    status = next((val for pat, val in _STATUS_SIGNALS if re.search(pat, text)), None)
    if not status:
        return None

    nos = _PLANNED_REF.findall(text)
    return IntentResult(
        intent=INTENT_UPDATE_STATUS,
        confidence=0.95,
        target=" ".join(f"今{n}" for n in nos),
        status=status,
        reasoning="命中日计划编号「今N」，直接改日计划表",
    )


# 从「3 完成了」里剥出序号
_LEADING_NO = re.compile(r"^\s*第?\s*(\d+)\s*[条项个]?\s*")

# 从「私募周报完成了」里剥掉状态词，剩下的当标题
_STRIP_STATUS = re.compile(
    r"(已经?|被)?(完成|做完|搞定|结束|上线|阻塞|卡住|卡了|开始|在做|进行中|推进中)"
    r"(了|啦|掉|了哈)?|，.*$|。.*$"
)


def _fallback_update_status(text: str) -> Optional[IntentResult]:
    """正则兜底识别状态更新。LLM 挂了也不能让「1 完成」失效。"""
    status = next((val for pat, val in _STATUS_SIGNALS if re.search(pat, text)), None)
    if not status:
        return None

    m = _LEADING_NO.match(text)
    if m:
        target = m.group(1)
    else:
        target = _STRIP_STATUS.sub("", text).strip(" ，,。的")
        # 剥完只剩一两个字的话不可信，宁可交给 LLM 再判一次
        if len(target) < 2:
            return None

    return IntentResult(
        intent=INTENT_UPDATE_STATUS,
        confidence=0.5,
        target=target,
        status=status,
        reasoning="正则兜底识别出状态更新",
        fallback_used=True,
    )


def _fallback_split_todos(text: str) -> list[TodoItem]:
    """正则切分待办（旧逻辑，作为 LLM 失败时的兜底）。

    与旧实现的差别：引导词黑名单补齐了"同步日计划"这类表述，
    且不再给未匹配项塞"日清待办"这类无意义标签。
    """
    normalized = text.replace("；", "\n").replace(";", "\n")
    normalized = re.sub(r"([，,]?\s*)([0-9]+[、\.])", r"\n\2", normalized)
    lines = [l.strip() for l in normalized.splitlines() if l.strip()]

    items: list[TodoItem] = []
    seen: set[str] = set()
    for line in lines:
        cleaned = _clean_title(line)
        cleaned = re.sub(
            r"^(?:同步|录入|记录)?\s*(?:今日|今天|明日|明天)?\s*"
            r"(?:日计划|工作计划|计划|待办)[:：，,\s]*",
            "", cleaned,
        ).strip()
        if not cleaned or _SHELL_PATTERN.match(cleaned) or len(cleaned) < 2:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(TodoItem(title=cleaned))
    return items


def fallback_route(text: str) -> IntentResult:
    """关键字规则路由。仅在 LLM 失败时使用。

    状态更新排在最前：它的表述最短、最容易撞上别的关键词
    （"开始做计划" 里既有"开始"又有"计划"），必须先判。
    """
    upd = _fallback_update_status(text)
    if upd:
        return upd

    for intent, keywords in _FALLBACK_RULES:
        if any(k in text for k in keywords):
            todos = _fallback_split_todos(text) if intent == INTENT_LOG_PLAN else []
            return IntentResult(
                intent=intent,
                confidence=0.4,
                todos=todos,
                query=text,
                reasoning=f"正则兜底命中关键词 {keywords}",
                fallback_used=True,
            )
    return IntentResult(
        intent=INTENT_CHAT, confidence=0.3, query=text,
        reasoning="正则兜底未命中任何规则", fallback_used=True,
    )


# ══════════════════════════════════════════════════════════════════════
# 对外主入口
# ══════════════════════════════════════════════════════════════════════

def route_intent(text: str) -> IntentResult:
    """把用户消息解析成 IntentResult。LLM 优先，失败降级正则。"""
    stripped = (text or "").strip()
    if not stripped:
        return IntentResult(intent=INTENT_CHAT, confidence=0.0, reasoning="空消息")

    shortcut = _planned_status_shortcut(stripped)
    if shortcut:
        return shortcut

    raw = _post_chat(
        [{"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
         {"role": "user", "content": _build_user_content(stripped)}],
        temperature=0.0,
    )
    payload = _extract_json(raw) if raw else None

    if not payload:
        logger.warning("LLM 路由失败，降级正则: %s", stripped[:60])
        return fallback_route(stripped)

    intent = str(payload.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        logger.warning("LLM 返回未知 intent=%r，降级正则", intent)
        return fallback_route(stripped)

    # 非录入意图强制清空 todos，避免 LLM 顺手塞东西
    todos = normalize_todos(payload.get("todos")) if intent == INTENT_LOG_PLAN else []

    try:
        confidence = float(payload.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8

    # 边界保护：判成录入计划但一条有效待办都抽不出来 → 说明没听懂，转闲聊
    if intent == INTENT_LOG_PLAN and not todos:
        logger.info("判定为录入计划但无有效待办，转 chat: %s", stripped[:60])
        return IntentResult(
            intent=INTENT_CHAT, confidence=confidence, query=stripped,
            reasoning="识别为计划录入但未抽取出有效工作项",
        )

    target = payload.get("target")
    target = str(target).strip() if target and str(target).strip().lower() not in {"none", "null"} else None
    status = _normalize_status(payload.get("status"))

    # 边界保护：判成状态更新但指代不明或状态非法 → 当作普通问答，不要瞎改表
    if intent == INTENT_UPDATE_STATUS and not (target and status):
        logger.info("判定为状态更新但缺 target/status，转 chat: %s", stripped[:60])
        return IntentResult(
            intent=INTENT_CHAT, confidence=confidence, query=stripped,
            reasoning="识别为状态更新但指代或状态不完整",
        )

    return IntentResult(
        intent=intent,
        confidence=confidence,
        todos=todos,
        query=payload.get("query") or (stripped if intent != INTENT_LOG_PLAN else None),
        target=target,
        status=status,
        reasoning=str(payload.get("reasoning") or ""),
    )