"""prd-watch-agent handler — easyprd 手动入口。

两条链路共用同一个 `PrdWatchService`（同一 watcher、同一 CR 台账、同一 metrics），
所以 Handler 只做「把用户这句话翻译成服务调用 + 把结果说成人话」：

| 用户说 | 工具 | 能力 |
|---|---|---|
| 监听在跑吗 / 为什么没写评论 | `prd_watch_status` | 状态与计数 |
| 把刚才讨论整理进 PRD | `flush_prd_watch` | 立刻处理当前群缓冲 |
| 整理这场会（贴妙记/纪要链接） | `ingest_meeting` | 会议素材 → 变更建议 |
| 这个需求讨论到哪了 | `analyze_requirement` | 知识地图（PRD/会议/变更/同族需求） |
| PRD 和设计稿对一下 | `review_prd_ui` | Figma 文本层差异 → 同一套评论/卡片 |

能力边界（与设计稿 §9.2 一致，2026-11-16 补充后仍不变）：
- 不写 PRD 正文（只有评论）；不猜群↔Story、会议↔Story、PRD↔PRD 绑定；
- 台账/凭证没配就明说没配，不假装做完了。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.handlers import AgentHandler, ToolResult
from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch.models import BAND_FLAGGED
from app.domains.prd_watch.watcher import PrdWatcher
from app.orchestrator.context import get_caller_context

logger = logging.getLogger("agents.handlers.prd_watch")

# 单轮同步等待上限：旁路窗口最长 window_sec，这里给足余量但不无限等
_FLUSH_WAIT_SEC = 150.0
# 会议/对账各要一次模型调用 + 可能的 lark-cli / Figma 取数，给足的等待上限
_HEAVY_WAIT_SEC = 170.0

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "flush_prd_watch",
            "description": "立刻处理当前群已攒下的讨论并产出 PRD 变更建议（不等攒窗结束）。"
                "用户说「把刚才讨论的变更整理进 PRD / 现在就看这条有没有变更 / "
                "重新识别一次」时使用。只在被授权监听的群生效。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prd_watch_status",
            "description": "查 easyprd 全部子能力状态：群监听、会议映射/自动扫描、"
                "CR 台账是否持久化、知识台账、Figma 是否可用，以及候选/丢弃/待确认计数。"
                "用户问「PRD 监听有没有在跑 / 识别得准不准 / 为什么没写评论」时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_meeting",
            "description": "把一场会议（妙记链接、智能纪要 docx 链接，或妙记 token）整理成 "
                "PRD 变更建议：读摘要/纪要 → 识别变更 → 写 PRD 评论 + 发群卡片 + 落知识台账。"
                "用户说「整理这场会 / 把会议纪要里的变更提进 PRD / 这场的结论落到 PRD」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "link": {"type": "string",
                             "description": "妙记或纪要的飞书链接（也接受裸 token）"},
                    "story_id": {"type": "string",
                                 "description": "可选。这条素材归属的 Story ID；"
                                                "不填则用当前群绑定的全部 Story 判归属"},
                },
                "required": ["link"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_story",
            "description": "给一条飞书项目 Story 链接（或 Story ID）做需求分析："
                "取 Story/PRD/设计稿（Figma，Story 字段或设计工单）/相关会议/同族需求，"
                "并对 PRD 做机械自检（枚举数量、链集合、删除线残留、术语、状态覆盖），"
                "每条结论带原文位置与可点跳转，推一张带「采纳/修正/忽略」按钮的卡片。"
                "用户说「分析这个需求 / 这个需求有什么问题 / 评审前先看看 PRD」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "story_ref": {"type": "string",
                                  "description": "Story 链接（/story/detail/<id>）或纯数字 Story ID"},
                },
                "required": ["story_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_requirement",
            "description": "查一个需求的知识地图：状态/PRD/最新结论摘要/变更历史/历次会议，"
                "以及父版本与相关需求（同版本多需求的关联）。"
                "用户问「这个需求讨论到哪了 / 最新结论是什么 / 变过几次 / "
                "和哪些需求是一伙的」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "Story ID、Story 链接或需求名关键词"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review_prd_ui",
            "description": "把 PRD 与 UI 设计稿（Figma）做文本层对账：字段/入口/状态/文案"
                "两边缺了或对不上，产出差异并以 PRD 评论 + 群卡片的形式等采纳。"
                "用户说「PRD 和设计稿对一下 / UI 有没有漏做 / 这版稿子和 PRD 一致吗」时使用。"
                "只覆盖文本层，颜色/间距/视觉层级不在范围内（会如实说明）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "figma_link": {"type": "string",
                                   "description": "Figma 文件/画板链接"},
                    "story_id": {"type": "string",
                                 "description": "可选。对应 Story ID；不填则用当前群绑定的 PRD"},
                },
                "required": ["figma_link"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bind_prd_story",
            "description": "把一条飞书项目 Story 登记进本地台账：绑定到某个群（或私聊会话），"
                "并从 Meegle 自动取标题 / PRD 文档 token / 参与人。之后这个群里的讨论"
                "就能进 PRD 变更识别，analyze_requirement 也能查到它。"
                "用户说「把这条需求记到台账 / 让 bot 盯这条需求 / 这条 Story 归到我们群」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "story_ref": {"type": "string",
                                  "description": "Story 链接（/story/detail/<id>）或纯数字 Story ID"},
                    "chat_id": {"type": "string",
                                "description": "可选。绑定的群 chat_id（oc_…）；不填 = 当前会话"},
                },
                "required": ["story_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "group_prd_stories",
            "description": "把几条 Story 归成一个「版本组」写进本地台账：同一条迭代/同一次上线的"
                "需求放一组，之后按组提问能一次看全。只做分组与登记，不改 Story 本身。"
                "用户说「这几个需求是同一个版本 / 把它们组成一组 / 建个台账」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_name": {"type": "string",
                                   "description": "组名（如版本号「TaaS v3.6.0」或需求簇名）"},
                    "story_refs": {"type": "array", "items": {"type": "string"},
                                   "description": "Story 链接或 ID 列表（至少 2 条）"},
                    "note": {"type": "string",
                             "description": "可选。为什么归为一组（迭代名 / 上线批次 / 依赖关系）"},
                },
                "required": ["group_name", "story_refs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ledger_dump",
            "description": "看本地台账里存了什么（只读）：绑定 / CR 台账 / 需求地图 / 会议 / 变更记录。"
                "用户问「台账里有什么 / 现在绑了哪些需求 / 那些变更建议去哪了」时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string",
                              "enum": ["binding", "cr", "story", "meeting", "change"],
                              "description": "要看的表，默认 binding"},
                    "limit": {"type": "integer",
                              "description": "最多几条，默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_review_card",
            "description": "把**你判出来的** PRD 评审结论渲染成一张卡片发到当前会话（纯渲染，不做判断）。"
                "结论（级别/标题/原文/建议/证据）由你按 prd-review 系列 Skill 判好后传进来；"
                "这个工具只负责排版 + 登记，好让卡片上的「采纳」能把评论锚到原文块。"
                "没有 Story、只贴了一篇 PRD 时也能用——这正是它和 analyze_story 的分工："
                "analyze_story 跑机械自检，本工具呈现你的四维评审结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prd": {"type": "string",
                            "description": "PRD 链接（wiki/docx）。用于定位块与拼定位链接"},
                    "title": {"type": "string", "description": "文档标题；不填用 PRD 首块"},
                    "story_id": {"type": "string",
                                 "description": "可选。关联 Story ID；有它按钮回调与统计更好归因"},
                    "items": {
                        "type": "array",
                        "description": "评审结论列表。级别只有 阻断/待确认/建议 三种；"
                                       "「待确认」= 判不出来但必须人拍板，「建议」= 能开发但会返工",
                        "items": {
                            "type": "object",
                            "properties": {
                                "level": {"type": "string",
                                          "enum": ["阻断", "待确认", "建议"]},
                                "kind": {"type": "string",
                                         "description": "判定维度：一致性/可执行性/验收/证据"},
                                "title": {"type": "string", "description": "≤30 字，说清差什么"},
                                "quote": {"type": "string",
                                          "description": "PRD 原文逐字引用（≤60 字），"
                                                         "用于锚定原文块；锚不上就留空"},
                                "section": {"type": "string", "description": "原文所在章节"},
                                "issue": {"type": "string",
                                          "description": "冲突点/缺口；代码不一致必须写 文件:行号"},
                                "suggestion": {"type": "string", "description": "该怎么改"},
                                "evidence": {"type": "string",
                                             "description": "证据出处：文件:行号 / 另一篇文档标题"},
                            },
                            "required": ["level", "title"],
                        },
                    },
                    "note": {"type": "string",
                             "description": "脚注：哪几项没取证、哪个表没读到（如实写）"},
                },
                "required": ["prd", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_prd_blocks",
            "description": "按块读一篇 PRD 的正文（只读），每块带 block_id / 块类型 / 文本。"
                "用户贴了 PRD 链接要评审、或要核对「某句话在文档哪个位置」时使用。"
                "block_id 可直接拼成 `{文档URL}#{block_id}` 的定位链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string",
                            "description": "PRD 链接（wiki/docx）或 obj_token"},
                    "max_blocks": {"type": "integer",
                                   "description": "最多多少块，默认 200"},
                },
                "required": ["doc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_prd_sheets",
            "description": "读 PRD 正文里**内嵌的电子表格**（字段定义表 / 状态映射表这类）。"
                "正文 markdown 不会包含 sheet 内容，评审前必须单独读，否则等于没看字段规则。"
                "先用 read_prd_blocks 拿到正文，里面的 sheet 标签会给出 token 与 sheet_id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "spreadsheet_token": {"type": "string",
                                          "description": "内嵌表格的 token"},
                    "sheet_id": {"type": "string",
                                 "description": "工作表 id"},
                    "range": {"type": "string",
                              "description": "A1 记法范围，如 A1:J80；默认 A1:J100"},
                },
                "required": ["spreadsheet_token", "sheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_codebase",
            "description": "在本机代码仓库里精确检索关键词（只读，grep 固定字符串），"
                "返回文件路径 + 行号 + 该行内容。用于给 PRD 评审找代码证据：字段名、枚举值、"
                "状态常量、接口名是否真的存在、与 PRD 写的是否一致。"
                "**只返回命中，不做任何判断** —— 一致性结论由你读完片段后下。",
            "parameters": {
                "type": "object",
                "properties": {
                    "terms": {"type": "array", "items": {"type": "string"},
                              "description": "检索词列表（代码标识符或中文专名），建议 3~8 个"},
                    "root": {"type": "string",
                             "description": "可选。单个仓库根目录；不填用默认多根"},
                    "roots": {"type": "array", "items": {"type": "string"},
                              "description": "可选。多个仓库根（绝对路径）。一次需求常横跨 "
                                             "hub-platform/rwa-engine/wealth-service，只搜一个根会出现"
                                             "「检索成功但未命中」——那不是代码里没有"},
                    "per_term": {"type": "integer",
                                 "description": "每个词最多取几条，默认 3"},
                },
                "required": ["terms"],
            },
        },
    },
]


class PrdWatchAgentHandler(AgentHandler):
    """prd-watch-agent 处理器。"""

    agent_name = "prd-watch-agent"
    tools = TOOLS

    def __init__(self, config: RouterConfig,
                 prd_config: PrdWatchConfig | None = None,
                 watcher: PrdWatcher | None = None,
                 service: Any = None):
        super().__init__(config)
        self._pc = prd_config or PrdWatchConfig()
        self._watcher = watcher
        # 会议/知识台账/UI 对账都要 service（绑定解析、CR 台账、kb），watcher 不够用
        self._service = service

    def bind_watcher(self, watcher: PrdWatcher | None) -> None:
        """main 启动时注入共享实例（未注入时工具给「未启用」回执，不自建一套）。"""
        self._watcher = watcher

    def bind_service(self, service: Any) -> None:
        """注入服务（同一 PrdWatchService；未注入时会议/对账类工具给未接线回执）。"""
        self._service = service

    async def execute_tool(self, tool_name: str, args: dict) -> ToolResult:
        if tool_name == "prd_watch_status":
            return self._status()
        if tool_name == "flush_prd_watch":
            return await self._flush()
        if tool_name == "ingest_meeting":
            return await self._ingest_meeting(args)
        if tool_name == "analyze_story":
            return await self._analyze_story(args)
        if tool_name == "analyze_requirement":
            return await self._analyze(args)
        if tool_name == "review_prd_ui":
            return await self._review_ui(args)
        if tool_name == "bind_prd_story":
            return await self._bind_story(args)
        if tool_name == "group_prd_stories":
            return await self._group_stories(args)
        if tool_name == "ledger_dump":
            return await self._ledger_dump(args)
        if tool_name == "render_review_card":
            return await self._render_review_card(args)
        if tool_name == "read_prd_blocks":
            return await self._read_blocks(args)
        if tool_name == "read_prd_sheets":
            return await self._read_sheets(args)
        if tool_name == "grep_codebase":
            return await self._grep(args)
        return ToolResult(error=f"未知工具: {tool_name}")

    def _guard(self) -> str:
        """共用前置检查，返回人话拒绝理由（空串=可用）。"""
        if not self._pc.enabled:
            return ("easyprd 未启用（config.toml `[projects.prd_watch].enabled=false`）。"
                    "启用前需要在台账里配好「群 ↔ Story ↔ PRD 文档」绑定，"
                    "绑定由管理员维护，我不会替它猜。")
        if self._watcher is None:
            return "监听器未接线（bot 启动时未挂载），请重启后再试。"
        return ""

    def _status(self) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        snap = self._service.snapshot() if self._service is not None else \
            self._watcher.snapshot()  # type: ignore[union-attr]
        m = snap.get("metrics", {})
        lines = [
            f"📋 easyprd 状态 · {'⏸ dry_run（只识别不写评论）' if snap['dry_run'] else '▶ 真写评论'}",
            f"群监听：授权 {snap['watch_chats']} 个；当前在攒的窗口："
            f"{len(snap.get('open_windows') or {})} 个",
            f"会议：显式映射 {snap.get('minutes_mapped', 0)} 场；"
            f"自动扫描 {'开' if snap.get('meeting_auto') else '关'}",
            f"CR 台账：{snap.get('cr_store', '?')}｜知识台账：{snap.get('kb', '?')}"
            f"｜设计稿：{snap.get('ui', '?')}",
            f"旧台账：{snap.get('ledger')}",
            snap.get("analysis_feedback") or "分析意见反馈：暂无评分",
            f"识别提示词：v{snap.get('prompt_version')}",
            "",
            f"累计：看到消息 {m.get('seen', 0)} 条 / 无绑定跳过 {m.get('no_binding', 0)} / "
            f"变更候选 {m.get('candidates', 0)} / 低于阈值丢弃 {m.get('dropped', 0)} / "
            f"待人工确认 {m.get('flagged', 0)} / 未定位段落 {m.get('unanchored', 0)} / "
            f"已出评论 {m.get('emitted', 0)}",
        ]
        if m.get("errors"):
            lines.append(f"⚠️ 调用异常 {m['errors']} 次（多为网关或评论权限，看 bot 日志）")
        if snap["dry_run"]:
            lines.append("💡 dry_run 阶段：误报率看清楚后把 `dry_run=false` 才真写评论。")
        return ToolResult(data={"reply": "\n".join(lines)})

    async def _flush(self) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "")
        if not chat_id:
            return ToolResult(error="拿不到当前会话，请在群里 @ 我后再触发")
        if chat_id not in self._pc.watch_chats:
            return ToolResult(data={
                "reply": "⚠️ 这个群不在 easyprd 授权名单里（`[projects.prd_watch].watch_chats`），"
                "我没听过它的讨论，所以无内容可整理。要接入请先由管理员加白名单并配好台账绑定。"})
        import asyncio

        watcher: PrdWatcher = self._watcher  # type: ignore[assignment]
        try:
            result = await asyncio.wait_for(watcher.flush_now(chat_id),
                                            timeout=_FLUSH_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={
                "reply": f"⏳ 识别超过 {_FLUSH_WAIT_SEC:.0f}s 未完成（多半是 PRD 太长或网关慢），"
                         "稍后再试一次；已攒的讨论内容还在窗口里没丢。"})
        except Exception as e:
            logger.warning("flush failed: %s", e)
            return ToolResult(error=f"处理失败：{str(e)[:120]}")

        if not result.changes:
            return ToolResult(data={
                "reply": f"🔍 本窗口 {result.candidates} 条变更候选，"
                f"低于阈值的 {result.dropped} 条已丢弃——**没有可写进 PRD 的变更**。"
                "（排期/人员/纯讨论都不算 PRD 变更，我按设计稿的判据不凑数）"})
        head = ("⏸ dry_run：已识别但未写评论（确认识别质量用）"
                if self._pc.dry_run else "✅ 已写入 PRD 评论，等产品在评论下回复确认")
        return ToolResult(data={"reply": f"{head}\n\n{_changes_text(result)}"})

    async def _ingest_meeting(self, args: dict) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        if self._service is None:
            return ToolResult(error="服务未接线（会议整理需要 PrdWatchService），请重启后再试")
        link = str(args.get("link") or "").strip()
        if not link:
            return ToolResult(error="请给出妙记或智能纪要的链接")
        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "")
        # 不指定 Story 时由 watcher 用「当前群绑定的全部 Story」判归属；
        # 私聊里没有群绑定，明确说清楚而不是拿空 chat_id 去查
        story_id = str(args.get("story_id") or "").strip()
        if not story_id and not chat_id.startswith("oc_"):
            return ToolResult(data={
                "reply": "⚠️ 私聊里没有群绑定，请指定 `story_id`，或在绑定过该需求的群里再说一次。"})
        import asyncio

        try:
            mat, result = await asyncio.wait_for(
                self._service.run_meeting(chat_id, story_id, link=link),
                timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={
                "reply": f"⏳ 整理超过 {_HEAVY_WAIT_SEC:.0f}s 未完成（会议纪要通常很长），"
                         "稍后再试一次；素材已读到，不会重复读。"})
        except Exception as e:
            logger.warning("ingest_meeting failed: %s", e)
            return ToolResult(error=f"整理失败：{str(e)[:140]}")
        if not mat.ok:
            return ToolResult(data={
                "reply": "⚠️ 这场会没有读到可用素材：\n  · "
                        + "\n  · ".join((mat.errors or ["未知原因"])[:4])
                        + "\n（妙记读不到常见原因是权限：他人在自己名下录的会需要授权；"
                          "智能纪要多半能读，可以换纪要链接再试）"})
        if result is None:
            return ToolResult(data={"reply": f"ℹ️ 会议「{mat.title}」之前已整理过，本次未重复处理。"})
        if not result.changes:
            return ToolResult(data={
                "reply": f"🔍 已读取会议「{mat.title}」（{mat.source_label}），"
                        f"识别出 {result.candidates} 条候选，"
                        f"达阈变更 0 条——**没有需要写进 PRD 的变更**。"})
        head = ("⏸ dry_run：已识别但未写评论" if self._pc.dry_run
                else "✅ 已写入 PRD 评论，等产品在评论下确认")
        return ToolResult(data={
            "reply": f"{head}\n来源：{mat.source_label}\n\n{_changes_text(result)}"})

    async def _analyze_story(self, args: dict) -> ToolResult:
        """Story 级分析：机械自检 + 卡片。卡片发到当前会话，回执给模型念结论。"""
        if self._service is None:
            return ToolResult(error="服务未接线（需求分析需要 PrdWatchService），请重启后再试")
        ref = str(args.get("story_ref") or "").strip()
        if not ref:
            return ToolResult(error="请给出 Story 链接或 Story ID")
        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "")
        import asyncio

        try:
            text, digest, card = await asyncio.wait_for(
                self._service.analyze_story(ref), timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 分析超过 {_HEAVY_WAIT_SEC:.0f}s 未完成"
                                             "（PRD 较长或妙记搜索慢），稍后再试。"})
        except Exception as e:
            logger.warning("analyze_story failed: %s", e)
            return ToolResult(error=f"分析失败：{str(e)[:140]}")
        if digest is None:
            return ToolResult(data={"reply": text})
        # 卡片发到提问的会话（群里发群、私聊发私聊）；发不出去也要把结论念出来
        sent = ""
        if card is not None and chat_id and self._service is not None:
            try:
                await self._service.send_card(chat_id, card)
                sent = "\n\n（已发分析卡片：每条意见下可点「采纳」写 PRD 评论，"\
                       "「🙂 有帮助 / 🙃 判错了」打分喂准确率）"
            except Exception as e:
                logger.warning("分析卡片发送失败: %s", str(e)[:120])
                sent = f"\n\n（卡片发送失败：{str(e)[:60]}，以下为文字版结论）"
        return ToolResult(data={"reply": text + sent})

    async def _analyze(self, args: dict) -> ToolResult:
        if self._service is None:
            return ToolResult(error="知识台账未接线，请重启后再试")
        key = str(args.get("key") or "").strip()
        if not key:
            return ToolResult(error="请给出 Story ID、Story 链接或需求名关键词")
        try:
            text, _story = await self._service.analyze_requirement(key)
        except Exception as e:
            logger.warning("analyze_requirement failed: %s", e)
            return ToolResult(error=f"查询失败：{str(e)[:140]}")
        return ToolResult(data={"reply": text})

    async def _review_ui(self, args: dict) -> ToolResult:
        reason = self._guard()
        if reason:
            return ToolResult(data={"reply": f"⚠️ {reason}"})
        if self._service is None:
            return ToolResult(error="服务未接线（UI 对账需要 PrdWatchService），请重启后再试")
        link = str(args.get("figma_link") or "").strip()
        if not link:
            return ToolResult(error="请给出 Figma 文件或画板链接")
        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "")
        story_id = str(args.get("story_id") or "").strip()
        import asyncio

        try:
            note, result = await asyncio.wait_for(
                self._service.review_ui(chat_id, story_id, link),
                timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 对账超过 {_HEAVY_WAIT_SEC:.0f}s 未完成，稍后再试。"})
        except Exception as e:
            logger.warning("review_prd_ui failed: %s", e)
            return ToolResult(error=f"对账失败：{str(e)[:140]}")
        if result is None:
            return ToolResult(data={"reply": note or "⚠️ 没有可比较的内容。"})
        if not result.changes:
            return ToolResult(data={
                "reply": f"🔍 文本层比对完成：{result.candidates} 条候选，"
                        "达阈差异 0 条——**这一期 PRD 与稿子在字段/入口/文案上对得上**。"
                        "（颜色/间距/视觉层级不在本期范围）"})
        head = ("⏸ dry_run：已识别差异但未写评论" if self._pc.dry_run
                else "✅ 差异已写入 PRD 评论，等产品确认")
        return ToolResult(data={
            "reply": f"{head}\n范围：仅文本层（字段/入口/状态/文案）\n\n"
                     f"{_changes_text(result)}"})


    # ── 本地台账入口（绑定 / 分组 / 导出）──
    async def _bind_story(self, args: dict) -> ToolResult:
        if self._service is None or getattr(self._service, "local", None) is None:
            return ToolResult(error="本地台账未开启（config.toml `[projects.prd_watch].local_ledger`）")
        ref = str(args.get("story_ref") or "").strip()
        if not ref:
            return ToolResult(error="请给出 Story 链接或 Story ID")
        ctx = get_caller_context() or {}
        chat_id = str(args.get("chat_id") or ctx.get("chat_id") or "").strip()
        if not chat_id:
            return ToolResult(error="没有会话上下文，无法确定绑定到哪个群")
        import asyncio

        try:
            binding, note = await asyncio.wait_for(
                self._service.bind_story(ref, chat_id), timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 登记超过 {_HEAVY_WAIT_SEC:.0f}s 未完成，稍后再试。"})
        except Exception as e:
            logger.warning("bind_prd_story failed: %s", e)
            return ToolResult(error=f"登记失败：{str(e)[:140]}")
        if binding is None:
            return ToolResult(data={"reply": f"⚠️ 没登记成功：{note}"})
        return ToolResult(data={"reply": note})

    async def _group_stories(self, args: dict) -> ToolResult:
        if self._service is None or getattr(self._service, "local", None) is None:
            return ToolResult(error="本地台账未开启（config.toml `[projects.prd_watch].local_ledger`）")
        name = str(args.get("group_name") or "").strip()
        refs = [str(x).strip() for x in (args.get("story_refs") or []) if str(x).strip()]
        if not name or len(refs) < 2:
            return ToolResult(error="请给出组名和至少 2 条 Story（单条用 bind_prd_story 即可）")
        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "").strip()
        import asyncio

        try:
            text = await asyncio.wait_for(
                self._service.group_stories(name, refs, chat_id=chat_id,
                                            note=str(args.get("note") or "")),
                timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 分组超过 {_HEAVY_WAIT_SEC:.0f}s 未完成，稍后再试。"})
        except Exception as e:
            logger.warning("group_prd_stories failed: %s", e)
            return ToolResult(error=f"分组失败：{str(e)[:140]}")
        return ToolResult(data={"reply": text})

    async def _ledger_dump(self, args: dict) -> ToolResult:
        if self._service is None or getattr(self._service, "local", None) is None:
            return ToolResult(error="本地台账未开启（config.toml `[projects.prd_watch].local_ledger`）")
        table = str(args.get("table") or "binding").strip()
        limit = int(args.get("limit") or 20)
        import asyncio

        text = await asyncio.to_thread(self._service.local.dump, table, limit)
        stats = await asyncio.to_thread(self._service.local.stats)
        summary = "｜".join(f"{k} {v}" for k, v in stats.items())
        return ToolResult(data={"reply": f"{text}\n\n台账规模：{summary}"})


    # ── 取证工具（只读；判断留给 Skill）─────────────────────────────
    async def _render_review_card(self, args: dict) -> ToolResult:
        """把执行面判好的结论排版成卡片（**不产生任何判断**）。

        为什么不做成「让模型吐卡片 JSON」：CardKit 的列宽/灰底/按钮 behaviors 有
        一堆平台约束（2.0 不支持 `action` 容器、按钮必须带 behaviors 才回调），
        让模型拼 JSON 一定会有一次拼错，错一次整张卡发不出去。所以判断归 Skill，
        样式归这里，两边只通过本工具的参数契约对接。
        """
        if self._service is None:
            return ToolResult(error="服务未接线（评审卡片需要 PrdWatchService），请重启后再试")
        prd = str(args.get("prd") or "").strip()
        raw_items = args.get("items") or []
        if not prd:
            return ToolResult(error="请给出 PRD 链接")
        if not isinstance(raw_items, list) or not raw_items:
            return ToolResult(error="items 为空：请把你判出的结论逐条传进来")

        import asyncio

        from app.domains.prd_watch.anchor import BlockAnchor
        from app.domains.prd_watch.dedupe import locator_terms
        from app.domains.prd_watch.review_card import (
            ReviewBundle,
            ReviewItem,
            build_review_cards,
            order_items,
            to_analysis_items,
            verdict_text,
        )
        from app.protocols.lark_cli import LarkCli

        items = [ReviewItem(
            level=str((it or {}).get("level") or "建议").strip(),
            kind=str((it or {}).get("kind") or "").strip(),
            title=str((it or {}).get("title") or "").strip(),
            quote=str((it or {}).get("quote") or "").strip(),
            section=str((it or {}).get("section") or "").strip(),
            issue=str((it or {}).get("issue") or "").strip(),
            suggestion=str((it or {}).get("suggestion") or "").strip(),
            evidence=str((it or {}).get("evidence") or "").strip(),
        ) for it in raw_items if isinstance(it, dict)]
        items = [it for it in items if it.title]
        if not items:
            return ToolResult(error="items 里没有带 title 的结论")

        # PRD 坐标：wiki 链接要换成 docx obj_token 才能读块/写评论；读不到不阻断出卡，
        # 只是「采纳」会退回整篇评论（卡片上仍可点，回执会说清楚）。
        prd_url = prd
        token = ""
        doc_title = str(args.get("title") or "").strip()
        anchor = BlockAnchor()
        try:
            info = await asyncio.wait_for(
                LarkCli().inspect_url(prd), timeout=_HEAVY_WAIT_SEC)
            token = str(info.get("obj_token") or info.get("token") or "")
            prd_url = prd
            doc_title = doc_title or str(info.get("title") or "")
        except Exception as e:
            logger.info("评审卡片 PRD 解析失败（链接可能不是飞书文档）: %s", str(e)[:120])
        if token:
            try:
                blocks = await asyncio.wait_for(
                    anchor._blocks_with_tags(token), timeout=_HEAVY_WAIT_SEC)
            except Exception as e:
                logger.info("评审卡片读块失败（仍出卡，采纳退整篇评论）: %s", str(e)[:120])
                blocks = []
            if blocks and not doc_title:
                doc_title = str(blocks[0].get("text") or "").strip()
            # 逐条锚原文：quote 命中块就记 block_id + 直达链接，锚不上留空（不猜块）。
            # 与讨论链路的 `_locate_change` 同一套三级兜底：quote → section 里的关键词
            # 共现。只试逐字 quote 时，模型综合表述（`问题` 里写的、原文没这句）
            # 会整条锚空，卡片上就没有「定位原文」——而那段原文其实找得到。
            for item in items:
                bid = ""
                if item.quote:
                    try:
                        bid = await asyncio.wait_for(
                            anchor.locate(token, item.quote), timeout=_HEAVY_WAIT_SEC)
                    except Exception:
                        bid = ""
                if not bid:
                    terms = locator_terms(f"{item.title}{item.issue}{item.suggestion}", limit=5)
                    if len(terms) >= 2:
                        try:
                            bid = await asyncio.wait_for(
                                anchor.locate_by_terms(token, terms),
                                timeout=_HEAVY_WAIT_SEC)
                        except Exception:
                            bid = ""
                if not bid:
                    continue
                item.block_id = bid
                item.jump_url = f"{prd_url.split('#')[0]}#{bid}"
                if not item.section:
                    try:
                        item.section = await anchor.section_of(token, bid)
                    except Exception:
                        item.section = ""

        bundle = ReviewBundle(
            title=doc_title, prd_url=prd_url, prd_token=token,
            story_id=str(args.get("story_id") or "").strip(),
            items=items, note=str(args.get("note") or "").strip())
        try:
            self._service.card_actions.feedback.register_items(to_analysis_items(bundle))
            cards = build_review_cards(bundle)
        except Exception as e:
            logger.warning("评审卡片渲染失败: %s", str(e)[:140])
            return ToolResult(error=f"卡片渲染失败：{str(e)[:140]}")

        ctx = get_caller_context() or {}
        chat_id = str(ctx.get("chat_id") or "")
        if not chat_id:
            return ToolResult(data={"reply": "⚠️ 没有会话上下文，卡片没发出去（结论在回执里）\n"
                                             + verdict_text(bundle)})
        # 逐张发、逐张记录成败：飞书对元素数有硬上限，一张失败不能带走后面几张，
        # 也不能把「一张都没发出去」说成「已发卡片」。
        sent_ok, failures = 0, []
        for index, card in enumerate(cards, start=1):
            try:
                await self._service.send_card(chat_id, card)
                sent_ok += 1
            except Exception as e:
                logger.warning("评审卡片发送失败(第 %d/%d 张): %s",
                               index, len(cards), str(e)[:160])
                failures.append(f"第 {index} 张：{str(e)[:80]}")
        if sent_ok == len(cards):
            sent = (f"已发评审卡片 {sent_ok} 张到本会话" if sent_ok > 1
                    else "已发评审卡片到本会话")
        elif sent_ok:
            sent = f"⚠️ 只发出 {sent_ok}/{len(cards)} 张：{'；'.join(failures)}"
        else:
            sent = f"⚠️ 卡片一张都没发出去：{'；'.join(failures)}"

        codes = "、".join(code for code, _ in order_items(items))
        anchored = sum(1 for it in items if it.block_id)
        lines = [f"🧭 评审卡片：{sent}",
                 f"结论 {len(items)} 条（{codes}）；原文锚定 {anchored}/{len(items)} 条",
                 verdict_text(bundle)]
        if not token:
            lines.append("⚠️ PRD 坐标没解析出来：卡片上的「采纳」会退回整篇评论")
        return ToolResult(data={"reply": "\n".join(lines)})

    async def _read_blocks(self, args: dict) -> ToolResult:
        """PRD 正文按块读（带块类型）。这是评审唯一正文来源。"""
        doc = str(args.get("doc") or "").strip()
        if not doc:
            return ToolResult(error="请给出 PRD 链接或 obj_token")
        limit = max(10, min(int(args.get("max_blocks") or 200), 600))
        import asyncio

        from app.domains.prd_watch.anchor import BlockAnchor

        anchor = BlockAnchor()
        try:
            blocks = await asyncio.wait_for(anchor._blocks_with_tags(doc),
                                            timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 读取超过 {_HEAVY_WAIT_SEC:.0f}s 未完成，稍后再试。"})
        except Exception as e:
            logger.warning("read_prd_blocks failed: %s", e)
            return ToolResult(error=f"读取失败：{str(e)[:160]}")
        if not blocks:
            return ToolResult(data={"reply": "⚠️ 没读到块：链接不是飞书文档，或本机没有读权限。"})
        lines: list[str] = [f"📄 共 {len(blocks)} 块（展示前 {min(limit, len(blocks))} 块）"]
        for b in blocks[:limit]:
            bid = str(b.get("block_id") or "")
            tag = str(b.get("tag") or "")
            text = " ".join(str(b.get("text") or "").split())
            if not text:
                continue
            lines.append(f"[{tag}] {bid}\n{text[:500]}")
        return ToolResult(data={"reply": "\n\n".join(lines)})

    async def _read_sheets(self, args: dict) -> ToolResult:
        """内嵌表格读值（正文 markdown 不含 sheet 内容，漏读 = 没看字段规则）。"""
        token = str(args.get("spreadsheet_token") or "").strip()
        sheet_id = str(args.get("sheet_id") or "").strip()
        if not token or not sheet_id:
            return ToolResult(error="需要 spreadsheet_token 与 sheet_id")
        rng = str(args.get("range") or "A1:J100").strip()
        import asyncio

        from app.protocols.lark_cli import _run

        try:
            payload = await asyncio.wait_for(_run([
                "sheets", "+cells-get", "--spreadsheet", token,
                "--range", f"{sheet_id}!{rng}", "--as", "user",
                "--format", "json", "--include", "value",
            ]), timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 读表超过 {_HEAVY_WAIT_SEC:.0f}s 未完成。"})
        except Exception as e:
            logger.warning("read_prd_sheets failed: %s", e)
            return ToolResult(error=f"读表失败：{str(e)[:160]}")
        data = payload.get("data") if isinstance(payload, dict) else None
        ranges = (data or {}).get("ranges") or []
        if not ranges:
            return ToolResult(data={"reply": "⚠️ 表里没读到内容（sheet_id 可能不对，或表是空的）"})
        out: list[str] = []
        for r in ranges:
            for row in r.get("cells") or []:
                vals = []
                for cell in row:
                    value = cell.get("value") if isinstance(cell, dict) else cell
                    vals.append("" if value is None else str(value).replace("\n", " "))
                line = " | ".join(vals).strip(" |")
                if line:
                    out.append(line)
        return ToolResult(data={"reply": "\n".join(out) or "⚠️ 表里没有非空单元格"})

    async def _grep(self, args: dict) -> ToolResult:
        """代码检索（只读）。取证而非判断：回命中位置，判断交给 Skill。"""
        terms = [str(t).strip() for t in (args.get("terms") or []) if str(t).strip()]
        if not terms:
            return ToolResult(error="请给出至少一个检索词")
        import asyncio

        from app.domains.prd_watch import code_search

        root = str(args.get("root") or "").strip()
        roots = [str(r).strip() for r in (args.get("roots") or []) if str(r).strip()]
        per_term = max(1, min(int(args.get("per_term") or 3), 8))
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(code_search.search_terms, terms,
                                  root=(root or None), roots=(roots or None),
                                  per_term=per_term),
                timeout=_HEAVY_WAIT_SEC)
        except TimeoutError:
            return ToolResult(data={"reply": f"⏳ 检索超过 {_HEAVY_WAIT_SEC:.0f}s 未完成，减少检索词再试。"})
        except Exception as e:
            logger.warning("grep_codebase failed: %s", e)
            return ToolResult(error=f"检索失败：{str(e)[:160]}")
        if not hits:
            return ToolResult(data={"reply": f"🔍 {len(terms)} 个词都没有命中。"
                                             "（没命中 ≠ 代码里没有：可能在其他仓库，或命名不同）"})
        roots_hit = sorted({h.path.split("/")[0] for h in hits if "/" in h.path})
        lines = [f"🔍 {len(hits)} 处命中（{len(terms)} 个检索词；"
                 f"涉及仓库 {', '.join(roots_hit) or '未知'}）"]
        for h in hits:
            lines.append(f"{h.path}:{h.line}\n{h.text}   ← 命中 {h.why}")
        return ToolResult(data={"reply": "\n\n".join(lines)})


def _changes_text(result: Any) -> str:
    """WatchResult → 逐条列表（带 CR 与低置信标记）。"""
    body = "\n".join(
        f"  · {p.cr_id or ''} {p.title or p.suggested_text[:24]}"
        f"{'（⚠️待确认）' if p.band == BAND_FLAGGED else ''}"
        f"{'｜未定位段落' if not p.located else ''}"
        for p in result.changes)
    return f"{body}\n\n统计：{result.summary()}"


def build(config, ctx: dict) -> PrdWatchAgentHandler:
    """自动发现约定工厂：easyprd 配置从启动期共享资源取。

    watcher/service 不在这里注入——它们需要发消息能力（channel 就绪后才存在）。
    main 在 channel 建好后调 bind_watcher / bind_service 补注入。
    """
    return PrdWatchAgentHandler(config, prd_config=ctx.get("prd_watch"))
