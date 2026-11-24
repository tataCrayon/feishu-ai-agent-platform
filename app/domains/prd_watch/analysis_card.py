"""需求分析卡片 — Story 分析结果（行网格版，与「变更提醒卡」同一套视觉语言）。

版式三段式，与 `render.build_notice_card` 对齐：

```
📋 需求分析 · 虚拟币申赎代币化票据（9 条）
────────────────────────────────────────
#7166946209 · 6-实现中 · TaaS v3.6.0
产品 李嫣然 ｜ 测试 曹钰 ｜ 技术 王美苑
PRD[打开] ｜ 设计稿 无 ｜ 稳定币&VA
────────────────────────────────────────
【PRD 自检】 8 条        ← 灰底表头
┃类型  ┃问题与建议
┃删除线┃8 个段落已划掉但仍留在正文
┃      ┃建议：确认后删除（或说明保留原因）
┃      ┃位置：账户管理 › 账户总览 ·[定位原文]
[✅采纳][✏️修正][🚫忽略][🙂有帮助][🙃判错了]
────────────────────────────────────────
【同族需求】…
```

为什么从「每条一段 markdown」改成行网格：上一版每条发现压成 3~4 行无边框段落，
几条之后读不出「哪行属于哪条」；网格有灰底表头与对齐列，扫读成本低一个量级，
且与变更提醒卡、会议整理卡是同一套观感（用户明确要求对齐）。

采纳/修正/忽略是**动作**（改状态、写评论），🙂/🙃 是**评分**（只记准确率）：
两类信号分开，否则「有多少人愿意动手」会被当成「判得准不准」。
"""

from __future__ import annotations

from typing import Any

from app.domains.prd_watch.analysis import Finding, StoryDigest
from app.domains.prd_watch.analysis_feedback import item_id_for

_MAX_ROWS = 10          # 与变更卡片同口径：多了刷屏（飞书 table 也是 10 行封顶）
_RELATED_ROWS = 9       # 同族列表行数（实测同前缀最多 9 条）
_QUOTE_MAX = 90
_SUGGEST_MAX = 110
_TITLE_MAX = 42

# 两列：左侧窄列（放编号 + 枚举词），右侧承载正文。
# 左列用固定像素而不是 weighted：weighted 会按比例分宽度，2/11 在中文会话里
# 仍占掉近三成，右侧正文被挤到中间、左半屏大片空白（实测截图）。
# 宽度按**内容最宽的一行**给，不是按枚举词给：评审卡的左列还要竖排 `阻断·一致性`
# 与「定位原文」链接，64px 会让它们折行，编号被挤到最下面去
# （实测截图：编号跑到卡片最下方，看着像不属于这一条）。
_LEFT_COL_WIDTH = "88px"
# 带「定位原文」的行再宽 8px：「定位原文」四个字 + 链接内边距。
_LEFT_COL_WIDTH_LOCATE = "96px"

# 评分按钮文案。👍/👎 在群聊里读起来像「社交点赞」，会被当成给需求/给同事打分，
# 而这里问的是「这条结论判得准不准」——换成 🙂/🙃 两组表情，语义只说判读质量。
# 同一份文案被两张卡片（分析卡 + 评审卡）共用，改文案只改这里。
FEEDBACK_UP = "🙂 有帮助"
FEEDBACK_DOWN = "🙃 判错了"


def _builder() -> Any:
    from lark_channel import CardBuilder

    return CardBuilder()


def ordered_findings(digest: StoryDigest) -> list[tuple[int, Finding]]:
    """卡片上的意见顺序与序号（自检在前、UI 差异在后），**登记簿与渲染共用**。

    两边各算一次编号会漂移：卡片显示 3 号、回调写进台账的却是 4 号，
    评论区就对不上是哪条了。
    """
    mechanical = [f for f in digest.findings if f.kind != "UI差异"][:_MAX_ROWS]
    ui = [f for f in digest.findings if f.kind == "UI差异"][:_MAX_ROWS]
    return [(i, f) for i, f in enumerate(mechanical + ui, start=1)]


def build_story_card(digest: StoryDigest, *, actions: bool = True) -> dict[str, Any]:
    """一条 Story 的分析卡片：头部信息 → 行网格发现清单 → 同族 → 脚注。"""
    b = _builder()
    b.config(width_mode="fill")
    title = plain(digest.prd_title or digest.title or digest.story_id)
    head = f"📋 需求分析 · {title[:26]}"
    if digest.findings:
        head += f"（{len(digest.findings)} 条）"
    b.header(head)

    # ① 讲清这条需求：是什么 → 谁负责 → 文档在哪
    b.markdown("\n".join(_meta_lines(digest, title)))

    ordered = ordered_findings(digest)
    mechanical = [(i, f) for i, f in ordered if f.kind != "UI差异"]
    ui = [(i, f) for i, f in ordered if f.kind == "UI差异"]

    # ② 自检清单（灰底表头 + 逐条网格 + 逐条按钮）
    b.raw({"tag": "hr"})
    total_mech = len([f for f in digest.findings if f.kind != "UI差异"])
    if mechanical:
        b.markdown(_section_head("PRD 自检", total_mech,
                                 "每条可点回原文；按钮只改状态/记评分，不改正文"))
        b.raw(grid_header())
        for index, finding in mechanical:
            for element in _grid_row(finding, index):
                b.raw(element)
            if actions:
                b.raw(build_action_row(item_id_for(digest.story_id, index)))
        if total_mech > len(mechanical):
            b.markdown(f"<font color='grey'>另有 {total_mech - len(mechanical)} 条未展示"
                       "（与变更卡同口径，单卡最多 10 条）</font>")
    else:
        b.markdown(_section_head("PRD 自检", 0, ""))
        b.markdown("未发现可证明的问题 —— 数量 / 集合 / 删除线 / 术语 / 状态覆盖 / "
                   "放行词 / 一致性 / 结构 八类检查均为空。")

    # ③ PRD↔UI：有差异逐条列；没比成就说为什么（不假装比过）
    if ui:
        b.markdown(_section_head("PRD↔UI 差异", len(ui), "文本层：字段 / 入口 / 状态 / 文案"))
        b.raw(grid_header())
        for index, finding in ui:
            for element in _grid_row(finding, index):
                b.raw(element)
            if actions:
                b.raw(build_action_row(item_id_for(digest.story_id, index)))
    elif digest.ui_checked:
        b.markdown(_section_head("PRD↔UI", 0, "文本层未发现差异"
                                 "（颜色 / 间距 / 视觉层级不在本期范围）"))
    else:
        reason = "；".join(e for e in digest.ui_errors[:2] if e) or "未获取到设计稿文本层素材"
        b.markdown(_section_head("PRD↔UI", 0, f"未比对 —— {reason}"))

    # ④ 会议 + 同族 + 脚注
    b.markdown(_meeting_line(digest))
    related = _related_lines(digest)
    if related:
        b.markdown("\n".join(related))
        if actions and digest.related:
            # 一组按钮对应整个候选集（不是每条一组）：9 组按钮会变成按钮墙，
            # 而「要不要继续看同族」本来就是一个决定，不需要逐条确认。
            b.markdown("<font color='grey'>要把同族需求一起分析吗？"
                       "点「一起看」后把目标 Story 链接发我。</font>")
            b.raw(build_related_buttons(str(digest.related[0].get("story_id") or "")))
    tips = _tip_lines(digest)
    if tips:
        b.markdown("<font color='grey'>" + " ".join(tips) + "</font>")
    return dict(b.to_dict())


# ── 网格 ─────────────────────────────────────────────────────────


def _section_head(name: str, count: int, hint: str) -> str:
    """分段小标题：`【PRD 自检】 8 条` + 一行灰字提示。"""
    head = f"**【{name}】**" + (f" {count} 条" if count else "")
    return head + (f"\n<font color='grey'>{hint}</font>" if hint else "")


def grid_header(*, width: str = "") -> dict[str, Any]:
    """灰底表头：左列窄、右列撑满（与变更卡的 `_grid_header` 同一形态）。"""
    left_width = width or _LEFT_COL_WIDTH
    return {"tag": "column_set", "flex_mode": "none", "background_style": "grey",
            "columns": [
                {"tag": "column", "width": left_width,
                 "vertical_align": "top",
                 "elements": [{"tag": "markdown", "content": "**类型**"}]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "vertical_align": "top",
                 "elements": [{"tag": "markdown", "content": "**问题与建议**"}]},
            ]}


def grid_row(label: str, index: int, lines: list[str],
             *, jump_url: str = "", locate_text: str = "定位原文",
             width: str = "", code: str = "") -> dict[str, Any]:
    """一格两列：左列固定窄（编号 + 枚举词），右列承载正文。

    `code` 给了就**排在最前**（如 `B1`）：序号是这个条目在卡片上的身份，
    压在枚举词与链接下面，扫读时反而找不到它（实测截图：序号跑到左列最后一行，
    看着像不属于这一条）。顺序固定为 编号 → 枚举词 → 定位原文。

    `jump_url` 给了就在左列挂一个可点的「定位原文」——评审卡片每条都要能跳回
    PRD 原文，而卡片里其余链接都挤在正文列，长度不一，扫读时找不到入口。
    没有链接时左列只有编号与枚举词，不留空行。

    左列宽度按**内容最宽的一行**给：带「定位原文」的行要更宽，否则链接折行、
    编号被挤走。
    """
    head: list[str] = []
    if code:
        head.append(f"**{code}**")
    head.append(f"`{label}`")
    if not code:
        head.insert(0, f"<font color='grey'>{index}</font>")
    left = "\n".join(head)
    if jump_url:
        left += f"\n<font color='grey'>[{locate_text}]({jump_url})</font>"
    left_width = width or (_LEFT_COL_WIDTH_LOCATE if jump_url else _LEFT_COL_WIDTH)
    return {"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": left_width, "vertical_align": "top",
         "elements": [{"tag": "markdown", "content": left}]},
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
         "elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
    ]}


def _grid_row(finding: Finding, index: int) -> list[dict[str, Any]]:
    """一条发现 = **一格两列**（类型列只放序号+枚举词，正文列放标题/建议/原文/位置）。

    为什么不再拆成「标题行 + 详情行」两行网格：两行左列都是空的，行间就多出一块
    空高度，一条意见看着像两段（实测截图：类型列下半个卡片是空白）。收进一格后
    两列顶部对齐，行高由内容决定。
    """
    kind = finding.kind or "检查"
    right_lines = [f"**{clip(finding.title, _TITLE_MAX)}**"]
    if finding.suggestion:
        right_lines.append(f"<font color='grey'>建议：</font>"
                           f"{clip(finding.suggestion, _SUGGEST_MAX)}")
    if finding.quote:
        right_lines.append(f"<font color='grey'>原文：</font>"
                           f"{clip(finding.quote, _QUOTE_MAX)}")
    where = finding.section or "未定位章节"
    right_lines.append(f"<font color='grey'>位置：{where}</font>")
    return [grid_row(kind, index, right_lines, jump_url=finding.jump_url)]


# ── 按钮 ─────────────────────────────────────────────────────────


def build_action_row(cr_id: str, *, compact: bool = False) -> dict[str, Any]:
    """每条意见一行按钮：动作（采纳/修正/忽略）+ 评分（🙂 有帮助 / 🙃 判错了）。

    `compact=True` 只出「采纳 / 忽略 / 有帮助 / 判错了」四个：飞书单卡元素数有硬上限，
    多一列按钮就少一条结论，而「修正」本来就是低频动作（要人工补文案），
    缺它时回执里会说明改走文字流程——按钮少一个，卡片就能多放几条结论。

    动作与评分混排但语义分明：前几个带 `act`，后两个带 `rate`——
    回调层按 key 认领，不会把「这意见判错了」误当成「忽略这条意见」。
    """
    def btn(label: str, payload: dict[str, Any], style: str = "default",
            confirm: dict[str, Any] | None = None) -> dict[str, Any]:
        node: dict[str, Any] = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": payload,
            # 2.0 卡片必须声明 behaviors 才会触发回调（与 gateway 的垫片同口径）
            "behaviors": [{"type": "callback", "value": payload}],
        }
        if confirm:
            node["confirm"] = confirm
        return node

    def act(name: str, **extra: Any) -> dict[str, Any]:
        return {"cr": cr_id, "act": name, **extra}

    # 注意：CardKit **2.0 不支持 `action` 容器**（实测 200861 unsupported tag action），
    # 所以仍用 column_set，但列宽给 `auto` 并 `flex_mode="flow"`：按钮按内容宽度
    # 从左依次排布，不像 weighted 那样把整行撑满、在中间留出大片空白。
    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "small",
            "columns": [
                {"tag": "column", "width": "auto", "elements": [btn("✅ 采纳", act("adopt"), "primary")]},
                # 修正：compact 模式下不给按钮（低频，且要人工补一句文案）
                *([] if compact else [
                    {"tag": "column", "width": "auto",
                     "elements": [btn("✏️ 修正", act("fix"))]}]),
                {"tag": "column", "width": "auto", "elements": [btn("🚫 忽略", act("ignore", dest="ghost"), "danger", {
                    "title": {"tag": "plain_text", "content": "确认忽略？"},
                    "text": {"tag": "plain_text",
                             "content": "这条不写 PRD 评论，只做记录。"}})]},
                # 打分：不产生任何写操作，只喂准确率（误报多的规则据此收窄）
                {"tag": "column", "width": "auto",
                 "elements": [btn(FEEDBACK_UP, {"cr": cr_id, "rate": "useful"})]},
                {"tag": "column", "width": "auto",
                 "elements": [btn(FEEDBACK_DOWN, {"cr": cr_id, "rate": "false"})]},
            ]}


# ── 头部信息 / 会议 / 同族 / 脚注 ────────────────────────────────


def _meta_lines(digest: StoryDigest, title: str) -> list[str]:
    """三行：标题+ID+状态 · 参与人 · PRD/设计稿/业务线。"""
    line1 = f"**{plain(digest.title or digest.story_id)}**"
    # 状态与节点常是同一个词（`6-实现中` / `实现中`），只留一个
    node = digest.node if digest.node and digest.node not in (digest.status or "") else ""
    bits = [f"#{digest.story_id}" if digest.story_id else "", digest.status, node]
    line1 += " · " + " · ".join(b for b in bits if b)

    lines = [line1]
    if digest.people:
        lines.append(f"<font color='grey'>{digest.people}</font>")

    docs: list[str] = []
    if digest.prd_url:
        docs.append(f"PRD [{'查看' if len(title) > 12 else title}]({digest.prd_url})")
    else:
        docs.append("PRD <font color='grey'>无</font>")
    if digest.design_url:
        docs.append(f"设计稿 [查看]({digest.design_url})")
    else:
        docs.append("设计稿 <font color='grey'>无</font>")
    for label, value in (("业务线", _biz(digest.business)), ("优先级", digest.priority)):
        if value:
            docs.append(f"{label} {value}")
    lines.append("｜".join(docs))
    return lines


def _meeting_line(digest: StoryDigest) -> str:
    if digest.meetings:
        names = "、".join(
            (f"[{clip(m.get('title', ''), 16)}]({m.get('url', '')})"
             if m.get("url") else clip(m.get("title", ""), 16))
            for m in digest.meetings[:2])
        more = f" 等 {len(digest.meetings)} 场" if len(digest.meetings) > 2 else ""
        return f"**会议**：{names}{more}"
    if digest.meeting_errors:
        return f"<font color='grey'>**会议**：检索失败（{digest.meeting_errors[0][:40]}）</font>"
    return "<font color='grey'>**会议**：按需求名未检索到相关妙记</font>"


def _related_lines(digest: StoryDigest) -> list[str]:
    """同族需求按线索分组，**每条都是可点蓝链**。

    两条线索（同前缀 = 同一产品线；同迭代 = 同一 Sprint 内一起上线的需求）
    分开列：它们的含义不同，混在一起会让人以为「同前缀」等于「同版本」。
    同一需求可能两组都命中，按 story_id 去重后只留在先出现的那组。
    """
    if not digest.related:
        return []
    groups: dict[str, list[dict[str, str]]] = {}
    for r in digest.related[:_RELATED_ROWS]:
        groups.setdefault(r.get("basis") or "相关需求", []).append(r)
    lines: list[str] = []
    for basis, rows in groups.items():
        lines.append(f"**{basis} · {len(rows)} 条**")
        for r in rows:
            sid = str(r.get("story_id") or "")
            title = clip(r.get("title") or sid, 26)
            # 可点蓝链：MQL 不返回 URL，由 meegle 层用 project_key+id 拼好
            link = f"[{title}]({r['url']})" if r.get("url") else title
            bits = [link]
            if r.get("status"):
                bits.append(f"`{r['status']}`")
            if r.get("design_url"):
                bits.append("🎨")
            lines.append("· " + " ".join(bits))
    if len(digest.related) > _RELATED_ROWS:
        lines.append(f"<font color='grey'>· 另有 {len(digest.related) - _RELATED_ROWS} 条未列</font>")
    return lines


def build_related_buttons(story_id: str) -> dict[str, Any]:
    """同族条目的「要不要一起看」按钮：只回话，不写任何需求关系。"""
    def btn(label: str, rel: str, style: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": style,
            "value": {"sid": story_id, "rel": rel},
            "behaviors": [{"type": "callback", "value": {"sid": story_id, "rel": rel}}],
        }

    return {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "small",
            "columns": [
                {"tag": "column", "width": "auto",
                 "elements": [btn("🔗 一起看", "analyze", "primary")]},
                {"tag": "column", "width": "auto", "elements": [btn("暂不", "skip")]},
            ]}


def _tip_lines(digest: StoryDigest) -> list[str]:
    tips: list[str] = []
    for err in digest.errors[:2]:
        tips.append(f"· {err}")
    if digest.design_url:
        tips.append("· 设计稿链接已取到，可直接说「对一下 PRD 和设计稿」")
    tips.append("· 只做可证明的检查（数量/集合/删除线/术语/状态覆盖/放行词/一致性/结构/PRD↔UI），"
                "不改 PRD 正文")
    # 灰字标注：这张卡片是 easyprd 能力的测试产出，不是正式结论
    tips.append("· 🧪 easyprd 能力测试")
    return tips


def _biz(raw: str) -> str:
    """业务线字段是 option_id（Meegle 只回 id），映射不出就原样显示。"""
    return _BIZ_NAMES.get(raw, raw)


# 与 Meegle `business` 字段选项一一对应（实测 option_id → 名称）
_BIZ_NAMES = {
    "68909ce35145d70392e01705": "FRP",
    "6a14f9e4afeec00ad328e37d": "稳定币&VA",
    "69240cf49a2157041b572029": "公募基金&投顾",
    "693faf165b312b4d1fcd26f1": "票据",
    "693fb1c6867f3b58b4313e21": "债券",
    "693faee5cb106a8a754ef043": "私募基金",
    "68909cd1e56f4345b6443a9d": "FinEAM",
    "68909c36616da79b21ab0fc4": "中后台运营",
    "6a4e40a7ec9a91326087258c": "公司业务",
    "692965cb99040a47b77fccc9": "客户运营",
    "69f958a6a69839078c395233": "ESOP",
    "69296502171aa350676e8d07": "底层模块",
    "6a14f9d1dbb54acac204b424": "保险",
    "6a4e3fc96b3891d05b756807": "系统平台输出",
    "6a4e3fe70b419edacb89d769": "AI产品平台",
    "6a4e3ff17bdcb830918fbc86": "星智通",
    "6a14f987b62014d39b70da0e": "AI基础设施",
    "68909c5f0ce417510d299960": "财富公版-渠道",
    "68909c72abf53e28f0fb3d6e": "财富公版-webportal",
}


def plain(text: str) -> str:
    """卡片里显示的标题：把 `&` 换成「和」。

    CardBuilder 会把 `&` 转义成 `&amp;`（卡片里看见实体名比看见「和」难读），
    而标题本身就是给人看的短文本，换成中文不影响语义。
    """
    return str(text or "").replace("&amp;", "和").replace("&", "和")


def clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def card_text(card: dict[str, Any]) -> str:
    """卡片里的人话文本（单测断言用；实现与 render.card_text 同源）。"""
    from app.domains.prd_watch.render import card_text as _render_text

    return _render_text(card)
