"""easyprd 卡片按钮回调 — 采纳 / 忽略 / 修正，直接在群里点，不用去 PRD 回复。

为什么做按钮：原闭环要求产品跑到 PRD 评论里回复「采纳」，
按钮把这一步搬到卡片上；评论回复那条路**保留**（有人就在文档里看，两种入口都认）。

红线不变（设计稿 §6）：点「采纳」只回写状态与通知，**不改 PRD 正文**。

敏感操作的处理（这里必须守住，别被"顺手做全"诱导）：
- 「采纳」可逆（只改状态 + 评论标记），点一下即执行；
- 「修正」要写新表述进 PRD 评论，**可逆性差** → 不弹输入框直接发一条「请回复」
  的引导消息，由人把改后的表述发出来，再落评论（避免卡片里一个手滑就发出错文案）。
  这条待修正状态只绑「点修正的那个人 + 那个会话」，别人回话不会被当成修正文案。
- 「忽略」会 resolve 评论（对产品可见的关闭动作）→ 二次确认按钮。

设计成**纯函数 + 一个注入回调**：`build_decision_buttons(cr_id)` 产按钮 JSON，
`handle_card_action(cfg, ledger, lark, event, on_change)` 处理事件。这样单测无需飞书。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.cr_store import CrStore
from app.domains.prd_watch.ledger import COLUMNS, Ledger
from app.protocols.lark_cli import LarkCli

logger = logging.getLogger("prd_watch.card_actions")

# 一条待修正状态的生命周期：超时失效，避免几天后一句闲聊被当成修正文案落进 PRD
FIX_TTL_SEC = 1800.0

# 按钮 value 里的动作名 → 台账状态与评论回复文案（与 decisions._MARKS 保持一致）
_ACTIONS: dict[str, tuple[str, str]] = {
    "adopt": ("已采纳", "✅ 已采纳 —— PRD 正文请产品/研发按建议更新（我不代改正文）。"),
    "ignore": ("已忽略", "❌ 已忽略 —— 本条不再跟进。"),
    "fix": ("已调整", "🔄 已调整 —— 以本条修正后的表述为准。"),
}

# 文字指令 → 动作。卡片被转发后按钮点不动（飞书把转发消息当只读，点了只弹
# 「这是转发消息，无法直接操作」），但**编号能当指令用**：人把「采纳 B1」抄回
# 自己的会话，走的是与按钮完全相同的副作用（写评论 / 只记录 / 只记准确率）。
# 中英文都收：用户不会记得哪个词是按钮上的。词表与正则同源，卡片脚注与解析
# 都从这里取，免得「卡片上教的写法」和「真正认的写法」两处漂移。
COMMAND_WORDS: dict[str, str] = {
    "采纳": "adopt", "adopt": "adopt",
    "忽略": "ignore", "ignore": "ignore",
    "修正": "fix", "fix": "fix",
    "有用": "useful", "有帮助": "useful", "useful": "useful",
    "判错": "false", "判错了": "false", "false": "false",
}
# 「采纳 B1」/「adopt B1」/「B1 采纳」/「B1 adopt」/「有用 Q2」；编号是字母+数字。
# 编号在前时英文词也能认——两种顺序对人都自然，只认一种会让人觉得「有时灵有时不灵」。
COMMAND_RE = re.compile(
    r"^\s*(?:"
    r"(?P<word_zh>采纳|忽略|修正|有用|有帮助|判错|判错了)\s*(?P<code1>[BbQqSs]\d{1,2})"
    r"|(?P<code2>[BbQqSs]\d{1,2})\s*(?P<word_any>采纳|忽略|修正|有用|有帮助|判错|判错了"
    r"|adopt|ignore|fix|useful|false)"
    r"|(?P<word_en>adopt|ignore|fix|useful|false)\s*(?P<code3>[BbQqSs]\d{1,2})"
    r")\s*$",
    re.IGNORECASE,
)


def parse_plain_command(text: str) -> tuple[str, str]:
    """→ (动作, 卡片编号)；不是指令返回 ("", "")。

    只认极短的一句（`采纳 B1`），不做自然语言理解：这条路径是**按钮的替代**，
    判宽了会把群里正常讨论（「我觉得 B1 采纳一下」）当成指令去写 PRD 评论。
    """
    m = COMMAND_RE.match(str(text or "").replace("，", " ").replace(",", " "))
    if m is None:
        return "", ""
    code = (m.group("code1") or m.group("code2") or m.group("code3") or "").strip().upper()
    word = (m.group("word_zh") or m.group("word_any") or m.group("word_en") or "")
    return COMMAND_WORDS.get(word.strip().lower(), ""), code


def command_action(word: str) -> str:
    """指令词 → 动作名（卡片文案与解析共用同一份词表）。"""
    return COMMAND_WORDS.get(str(word or "").strip().lower(), "")


def command_hint() -> str:
    """卡片脚注里的那行操作提示：只用词表与编号格式，不另写一套文案。"""
    return "采纳 B1 / 忽略 B1 / 有用 B1 / 判错 B1"



def build_review_buttons(cr_id: str) -> dict[str, Any]:
    """一条变更的操作按钮行（卡片里每条变更下方一个）。

    `value` 透传 `{cr: CR编号, act: 动作}`；按钮组用 column_set 横排，避免竖着占三行。
    直接拼 JSON 不走 CardBuilder：render 要 import 本函数，反向 import 会成环。
    """
    def btn(label: str, act: str, style: str = "default",
            confirm: dict[str, Any] | None = None) -> dict[str, Any]:
        node: dict[str, Any] = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": {"cr": cr_id, "act": act},
            # 2.0 卡片必须声明 behaviors 才会触发回调（与 gateway._ensure_button_behaviors 同口径）
            "behaviors": [{"type": "callback", "value": {"cr": cr_id, "act": act}}],
        }
        if confirm:
            node["confirm"] = confirm
        return node

    # flex_mode="flow" + 列宽 auto：按钮按内容宽度横排、左对齐。
    # 用 weighted/默认 flex_mode 时按钮列会把整行撑满，按钮之间留出大片空白
    # （分析卡片实测同一问题）。
    return {
        "tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "small",
        "columns": [
            {"tag": "column", "width": "auto",
             "elements": [btn("✅ 采纳", "adopt", "primary")]},
            {"tag": "column", "width": "auto",
             "elements": [btn("✏️ 修正", "fix")]},
            {"tag": "column", "width": "auto",
             "elements": [btn("🚫 忽略", "ignore", "danger",
                              {"title": {"tag": "plain_text", "content": "确认忽略？"},
                               "text": {"tag": "plain_text",
                                        "content": "会在 PRD 上 resolve 这条评论，产品侧可见。"}})]},
        ],
    }


def extract_related(event: Any) -> tuple[str, str]:
    """→ (StoryID, 关联动作)。分析卡片的「同族需求」按钮用，非本类返回空串。

    与 `extract_action` 分开是为了让两类卡片各自自证身份：本函数只认 `sid`，
    变更卡片的 `cr` 不会被误领，反之亦然。
    """
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict):
        return "", ""
    inner = value.get("value") if isinstance(value.get("value"), dict) else value
    cand = inner if isinstance(inner, dict) else {}
    return str(cand.get("sid") or ""), str(cand.get("rel") or "")


def extract_action(event: Any) -> tuple[str, str]:
    """→ (条目编号, 动作)。不是本模块的卡片返回两个空串（多 handler 聚合分发时不误认领）。

    编号既可能是讨论变更的 `CR-…`，也可能是分析意见的 `AN-…`：两者都带 `act`
    走同一套状态机外壳，具体副作用由 `handle` 按前缀分流。
    """
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict):
        return "", ""
    # 有的回调包装形态会再套一层 {"value": {...}}，两种都认
    inner = value.get("value") if isinstance(value.get("value"), dict) else value
    cand = inner if isinstance(inner, dict) else {}
    return str(cand.get("cr") or ""), str(cand.get("act") or "")


def extract_rating(event: Any) -> tuple[str, str]:
    """→ (条目编号, `useful`/`false`)。只认 `rate` 键：动作按钮没有这个 key。"""
    action = getattr(event, "action", None)
    value = getattr(action, "value", None) or {}
    if not isinstance(value, dict):
        return "", ""
    inner = value.get("value") if isinstance(value.get("value"), dict) else value
    cand = inner if isinstance(inner, dict) else {}
    rate = str(cand.get("rate") or "")
    if rate not in ("useful", "false"):
        return "", ""
    return str(cand.get("cr") or ""), rate


def is_analysis_id(item_id: str) -> bool:
    """`AN-`/`RV-` 前缀 = 分析/评审意见（讨论变更走 `CR-`）。两条链路的副作用不同。

    两者副作用完全一样（采纳=在原文块写评论、忽略=只记录、评分=只记准确率），
    区别只在编号来源，所以共用一个判定；分开成两个函数只会让调用方漏判一种。
    """
    prefix = str(item_id or "").upper()
    return prefix.startswith(("AN-", "RV-"))


class CardActionHandler:
    """群卡片按钮处理：身份/权限由调用方（main）决定是否放行，这里只管状态机。"""

    def __init__(self, config: PrdWatchConfig, ledger: Ledger,
                 *, lark: LarkCli | None = None, cr_store: Any = None,
                 feedback: Any = None):
        self._cfg = config
        self._ledger = ledger
        # CR 表（决策状态机主库）。构造时按配置建，未配坐标 → 内存替身。
        self._cr = cr_store or CrStore(config.cr_base_token, config.cr_table)
        self._lark = lark or LarkCli()
        # 分析意见登记簿 + 打分区（AN- 条目）。懒建也行，但显式建能让单测注入临时目录。
        if feedback is None:
            from app.domains.prd_watch.analysis_feedback import AnalysisFeedback

            feedback = AnalysisFeedback()
        self.feedback = feedback
        # open_id → (cr_id, 截止时间)：点过「修正」待补文案的人。
        # **按人不按会话**：卡片可以被转发到别的群/私聊，转发后回调带回的是
        # 转发目标会话 id，用 (chat_id, open_id) 做 key 就永远对不上，
        # 用户照着提示回复新表述却被告知「没有待修正的条目」。
        # 修正人本来就唯一（30 分钟窗口、只认点按钮那个人），不带上会话反而更准。
        self._pending_fix: dict[str, tuple[str, float]] = {}

    # ── 文字指令：按钮点不动时的退路（转发后的卡片必走这条）───────
    async def handle_command(self, text: str, *, open_id: str = "") -> str:
        """`采纳 B1` / `忽略 B1` / `有用 B1` / `判错 B1` → 与按钮完全相同的副作用。

        为什么要有这条：飞书把**转发来的消息**当只读，按钮点击只弹「这是转发消息，
        无法直接操作」——那是客户端语义，应用侧改不了。但编号是可见的，人能把它
        抄回自己的会话，效果和点按钮一样。

        编号歧义（多篇文档都有 B1）时不猜：列出候选，让人补全。
        返回空串表示「不是指令」，调用方据此放行给正常消息链路。
        """
        act, code = parse_plain_command(text)
        if not act or not code:
            return ""
        items = self.feedback.find_by_code(code)
        if not items:
            return (f"⚠️ 没有编为 **{code}** 的结论（可能已过期，或这轮评审没登记）。"
                    "重新让我评一次这份 PRD 就有新卡片了。")
        if len(items) > 1:
            ids = "、".join(it.item_id for it in items[:5])
            return (f"⚠️ **{code}** 在 {len(items)} 篇里都有，分不清是哪一条。"
                    f"请用完整编号再发一次：{ids}")
        item = items[0]
        if act == "fix":
            self.arm_fix(item.item_id, open_id)
            return (f"✏️ 请回复一条消息，写明 {item.item_id} 的修正后表述"
                    f"（{int(FIX_TTL_SEC // 60)} 分钟内有效）。")
        if act in ("useful", "false"):
            self.feedback.record(item.item_id, act, operator=open_id)
            verdict = "判得准" if act == "useful" else "判错了"
            return f"{'🙂' if act == 'useful' else '🙃'} 已记录：{item.item_id} {verdict}。"
        if act == "ignore":
            self.feedback.record(item.item_id, "ignore", operator=open_id)
            return f"🚫 已记录：{item.item_id} 不落 PRD 评论。"
        return await self._adopt_analysis(item, operator=open_id)

    async def handle(self, event: Any, *, on_fix_prompt=None,
                     reply=None) -> str:
        """执行一次按钮动作，返回给点按者看的一句话（调用方负责发出）。

        `on_fix_prompt(chat_id)`：修正场景下引导用户回复新表述；
        `reply(chat_id, text)`：把结果回给群/私聊。
        """
        cr_id, act = extract_action(event)
        if not cr_id or act not in _ACTIONS:
            return ""                                   # 不是本卡片的按钮
        if is_analysis_id(cr_id):
            return await self._handle_analysis(cr_id, act, event)
        if act == "fix":
            self._arm_fix(cr_id, event)
            return (f"✏️ 请直接回复一条消息，写明 **{cr_id}** 的修改后表述，"
                    "我会把它作为新的评论建议落到 PRD 上（不覆盖原评论）。"
                    f"（{int(FIX_TTL_SEC // 60)} 分钟内有效）")
        # 先取行再改状态：改完之后 pending 里就查不到这条了
        rows = await self._rows([cr_id])
        state, mark = _ACTIONS[act]
        # 状态落两处：
        # 1) CR 表（状态机主库）——决策能否跨重启、能否被轮询看到全靠它；
        # 2) 旧台账（绑定表 + 变更记录）——灰度期两套并存，写失败不阻断。
        ok = await self._cr.set_status(cr_id, state, note="群卡片按钮")
        if not ok:
            ok = await self._ledger.set_decision(cr_id, state, note="群卡片按钮")
        if not ok:
            return f"⚠️ 台账未更新（{cr_id}），可能台账未配置；状态没落库。"
        doc = str(rows[0].get("PRD文档token") or "") if rows else ""
        comment_id = str(rows[0].get(COLUMNS["comment_id"]) or "") if rows else ""
        if doc and comment_id:
            try:
                await self._lark.reply_comment(doc, comment_id, mark,
                                               identity=self._cfg.comment_identity)
                if act == "ignore":
                    await self._lark.resolve_comment(doc, comment_id,
                                                     identity=self._cfg.comment_identity)
            except Exception as e:
                logger.warning("[prd_watch] 按钮回写评论失败 %s: %s", cr_id, str(e)[:100])
                return f"{mark}\n（PRD 评论回写失败：{str(e)[:60]}，状态已记在台账）"
        logger.info("[prd_watch] 卡片按钮 %s → %s", cr_id, state)
        if not (doc and comment_id):
            return f"{mark}\n台账已记为「{state}」。（这条没有对应 PRD 评论，未回写文档）"
        return f"{mark}\n台账已记为「{state}」。"

    # ── 分析意见（AN- 条目）：采纳写评论、忽略只记录、评分只记准确率 ────
    async def _handle_analysis(self, item_id: str, act: str, event: Any) -> str:
        item = self.feedback.get(item_id)
        if item is None:
            return (f"⚠️ 找不到 {item_id} 的登记记录（bot 可能在发卡后重启过且日志被清）。"
                    "请让我重新分析这条需求再点按钮。")
        operator = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
        if act == "ignore":
            self.feedback.record(item_id, "ignore", operator=operator)
            return f"🚫 已记录：{item_id} 不落 PRD 评论。"
        if act == "adopt":
            return await self._adopt_analysis(item, operator=operator)
        # fix：分析意见的「修正」不是改状态，是让产品给出他认为对的表述
        self._arm_fix(item_id, event)
        return (f"✏️ 请回复一条消息，写明你希望这条意见改成什么表述，"
                f"我会把**你的表述**作为 PRD 评论落下去（{item_id}，"
                f"{int(FIX_TTL_SEC // 60)} 分钟内有效）。")

    async def _adopt_analysis(self, item: Any, *, operator: str = "") -> str:
        """采纳 → 在**意见原文所在块**上写一条 PRD 评论。

        与讨论变更的采纳不同：那里是「回复已有评论」（评论是 watcher 先写好的），
        这里没有评论，所以是新增一条。仍守红线：只评论，不改正文。
        """
        if not item.prd_token:
            return "⚠️ 这条意见没有 PRD 坐标（Story 上的需求文档读不到），无法写评论。"
        body = item.comment_body() if hasattr(item, "comment_body") else ""
        try:
            await self._lark.add_comment(
                item.prd_token, body, block_id=item.block_id,
                identity=self._cfg.comment_identity,
                link_url=item.prd_url)
        except Exception as e:
            logger.warning("[prd_watch] 分析意见评论写入失败 %s: %s", item.item_id, str(e)[:120])
            return f"⚠️ PRD 评论写入失败：{str(e)[:100]}\n（正文没有任何改动）"
        self.feedback.record(item.item_id, "adopt", operator=operator)
        where = f"「{item.section}」段落" if item.section else "PRD"
        return (f"✅ 已采纳：在{where}写下评论（{item.item_id}）。\n"
                "PRD 正文请按建议人工更新 —— 我不改正文。")

    # ── 打分：只记准确率，不产生任何写操作 ───────────────────────────
    async def handle_rating(self, event: Any) -> str:
        item_id, rate = extract_rating(event)
        if not item_id or not rate:
            return ""
        if self.feedback.get(item_id) is None:
            return f"⚠️ 找不到 {item_id} 的登记记录，这条评分没记上。"
        operator = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
        self.feedback.record(item_id, rate, operator=operator)
        if rate == "useful":
            return f"🙂 已记录：{item_id} 判得准。"
        return (f"🙃 已记录：{item_id} 判错了。这条样本会用来收窄对应规则；"
                "如果你愿意，回一句「为什么错」，我把它记到反馈里。")

    # ── 修正的第二段：人补上文案后落评论 ─────────────────────────────
    def _arm_fix(self, cr_id: str, event: Any) -> None:
        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not open_id:
            return
        self._pending_fix[open_id] = (cr_id, time.time() + FIX_TTL_SEC)

    def arm_fix(self, item_id: str, open_id: str) -> bool:
        """文字指令版「修正」：没有事件对象时记下「这个人待补文案」。

        与按按钮等价，只是按钮回调能拿到 `operator`、文字指令只有调用方给的 open_id。
        """
        if not item_id or not open_id:
            return False
        self._pending_fix[open_id] = (item_id, time.time() + FIX_TTL_SEC)
        return True

    def pending_fix(self, open_id: str) -> str:
        """该人有没有待修正的 CR（过期即清，返回空串）。

        `chat_id` 不参与判定：见 `_pending_fix` 的注释（转发后会话 id 会变）。
        """
        hit = self._pending_fix.get(open_id)
        if hit is None:
            return ""
        cr_id, deadline = hit
        if time.time() > deadline:
            self._pending_fix.pop(open_id, None)
            return ""
        return cr_id

    async def handle_related(self, event: Any) -> str:
        """同族需求按钮：把「要不要把它一起分析」的答复变成一句可执行的后续。

        只回话、**不写任何关系字段**：Meegle 的需求关联是人工台账，自动建关系会污染
        需求网络。这里的价值是把「悄悄列 9 条候选」变成「你确认哪条，我再跑一次分析」。
        """
        sid, rel = extract_related(event)
        if not sid or rel not in ("analyze", "skip"):
            return ""
        if rel == "skip":
            return f"🙂 已记下：暂不关联 #{sid}。"
        return (f"✅ 已确认要把 #{sid} 一起看。"
                f"\n把它的 Story 链接发我（或直接说「看下 #{sid}」），我按同一套检查再跑一次；"
                "两篇的差异我会作为「跨需求冲突」另列。")

    async def apply_fix(self, chat_id: str, open_id: str, text: str) -> str:
        """把回复的消息当作修正后的表述，落一条新评论并结掉这条 CR。

        为什么新发一条评论而不是改原评论：飞书没有编辑评论的开放能力，且原建议是
        产品对着讨论给出的判断依据，覆盖掉就失去了"为什么改"的上下文。
        """
        cr_id = self.pending_fix(open_id)
        if not cr_id:
            return ""
        content = (text or "").strip()
        if not content:
            return ""
        if is_analysis_id(cr_id):
            return await self._apply_analysis_fix(cr_id, chat_id, open_id, content)
        rows = await self._rows([cr_id])
        if not rows:
            self._pending_fix.pop(open_id, None)
            return f"⚠️ 找不到 {cr_id} 的台账行（可能台账未配置），修正未落评论。"
        self._pending_fix.pop(open_id, None)
        row = rows[0]
        doc = str(row.get("PRD文档token") or "")
        comment_id = str(row.get(COLUMNS["comment_id"]) or "")
        title = str(row.get("变更标题") or "")
        body = (f"🔄 修正后的建议表述（{cr_id}）\n"
                f"变更项：{title or '（未命名）'}\n"
                f"修正为：{content}\n"
                "（原建议保留在上一条评论里，便于对照）")
        state, mark = _ACTIONS["fix"]
        ok = await self._cr.set_status(cr_id, state,
                                       note=f"群内修正：{content[:60]}")
        if not ok:
            ok = await self._ledger.set_decision(cr_id, state,
                                                 note=f"群内修正：{content[:60]}")
        if not ok:
            return f"⚠️ 台账未更新（{cr_id}），修正没落库、也没写评论。"
        if not doc:
            return f"{mark}\n台账已记为「{state}」，但台账行里没有 PRD 文档坐标，评论未写。"
        # 落在**原评论那个块**上，而不是整篇：整篇评论会飘到文档顶部，产品得挨个找。
        # 台账行里没有块 id（多维表格列名由管理员定，加列会让整行 upsert 失败），
        # 所以从评论列表反查一次；查不到就退回整篇，不猜块。
        block_id = ""
        if comment_id:
            try:
                for c in await self._lark.list_comments(
                        doc, identity=self._cfg.comment_identity):
                    if c.get("comment_id") == comment_id:
                        block_id = str(c.get("block_id") or "")
                        break
            except Exception as e:
                logger.info("[prd_watch] 反查原评论块失败(%s): %s", cr_id, str(e)[:80])
        try:
            await self._lark.add_comment(doc, body, block_id=block_id,
                                         identity=self._cfg.comment_identity)
        except Exception as e:
            logger.warning("[prd_watch] 修正评论写入失败 %s: %s", cr_id, str(e)[:120])
            return f"{mark}\n台账已记为「{state}」，但 PRD 评论写入失败：{str(e)[:60]}"
        if comment_id:
            try:
                await self._lark.reply_comment(doc, comment_id, mark,
                                               identity=self._cfg.comment_identity)
            except Exception:
                logger.debug("[prd_watch] 原评论回复失败 %s", cr_id, exc_info=True)
        logger.info("[prd_watch] 卡片修正 %s → %s", cr_id, state)
        return f"{mark}\n台账已记为「{state}」，修正后的表述已写成 PRD 评论。"

    async def _apply_analysis_fix(self, item_id: str, chat_id: str, open_id: str,
                                  content: str) -> str:
        """分析意见的「修正」：把**人给的表述**写成评论，不再沿用机器的建议。"""
        self._pending_fix.pop(open_id, None)
        item = self.feedback.get(item_id)
        if item is None or not item.prd_token:
            return f"⚠️ 找不到 {item_id} 的 PRD 坐标，修正未落评论。"
        body = (f"🤖 Bot EasyPRD 分析意见 · {item_id}（人工修正后）\n"
                f"【发现的问题】\n  {item.title}\n"
                f"【当前PRD原文】\n  {item.quote or '（未定位到原文）'}\n"
                f"【产品给的表述】\n  {content}\n"
                "  （机器原建议不写入：以本条为准）")
        try:
            await self._lark.add_comment(item.prd_token, body, block_id=item.block_id,
                                         identity=self._cfg.comment_identity,
                                         link_url=item.prd_url)
        except Exception as e:
            logger.warning("[prd_watch] 修正评论写入失败 %s: %s", item_id, str(e)[:120])
            return f"⚠️ PRD 评论写入失败：{str(e)[:100]}，修正没落库。"
        self.feedback.record(item_id, "adopt", operator=open_id)
        return f"🔄 已按你的表述写好评论（{item_id}）。PRD 正文仍需人工更新。"

    async def _rows(self, cr_ids: list[str]) -> list[dict]:
        """按 CR 编号取台账行（按钮回调没有行数据，只能按编号回查）。

        先查 CR 表（新形态，带评论 id / 块 id / 分带），查不到再退回旧台账——
        灰度期两种数据都在，只认一种会让老卡片点不动。
        """
        wanted = set(cr_ids)
        try:
            for cr_id in cr_ids:
                row = await self._cr.get(cr_id)
                if row is not None:
                    return [_cr_as_row(row)]
        except Exception:
            pass
        try:
            pending = await self._ledger.pending_rows()
        except Exception:
            return []
        return [r for r in pending if str(r.get(COLUMNS["cr_id"])) in wanted]


def _cr_as_row(row: Any) -> dict:
    """CR 行 → 旧台账行形态（下游按 `PRD文档token` / `评论ID` 取坐标）。"""
    return {
        COLUMNS["cr_id"]: row.cr_id,
        COLUMNS["comment_id"]: row.comment_id,
        "PRD文档token": row.prd_token,
        "变更标题": row.title,
        "StoryID": row.story_id,
        "关联群chat_id": row.chat_id,
    }
