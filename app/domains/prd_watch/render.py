"""easyprd 文案渲染 — PRD 评论正文 + 群通知卡片（纯函数，无 IO）。

格式契约的唯一出处是 docs/features/easyprd-prd-watch.md §4，
本文件是它的代码实现；改格式先改设计稿，再改这里，否则门禁单测会指出两者不一致。

卡片形态对齐**飞书项目机器人**：彩色 header + 「空间 · 类型 · #ID」
副标题 + 蓝色可点标题 + `**字段：** 值` 逐行 + @人 + 底部按钮。不是纯文本堆叠。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domains.prd_watch.card_actions import build_review_buttons
from app.domains.prd_watch.models import BAND_FLAGGED, ChangePoint

TZ = timezone(timedelta(hours=8))

# 低置信度必须在评论里显式声明来源，不得冒充定论（设计稿 §4.3）
_FLAG_NOTE = "⚠️ 此条由 AI 自动识别，置信度较低，请人工确认是否为正式变更"
_NEW_CLAUSE_MARK = "（未在PRD中定位到对应段落，此为建议新增）"
_DELETE_MARK = "【建议删除本段】"

# 评论署名。飞书评论不显示"机器人"标记（user 身份下署名是登录人，bot 身份下也只是
# 应用名），不写清来源，读者会以为是提这条的人自己写的。
# 版式与卡片对齐（变更依据 → 改什么 → 凭什么 → 谁来定），但**不用 markdown**：
# 评论正文是纯文本，`**加粗**` / `[文字](url)` 会原样显示成星号和括号（实测），
# 链接靠 link 元素单独渲染成可点。
_SIGN = "🤖 Bot EasyPRD 自动评论"
_ROLE_NOTE = "本条由 Bot EasyPRD 根据讨论自动生成，未经人工确认，不作为定论；正文不会被自动修改。"


def make_cr_id(story_id: str, seq: int, when: datetime | None = None) -> str:
    """CR-{YYYYMMDD}-{StoryID}-{当日序号}，全链唯一键（评论→卡片→台账）。"""
    now = when or datetime.now(TZ)
    return f"CR-{now.strftime('%Y%m%d')}-{story_id}-{seq:03d}"


def render_comment(
    cp: ChangePoint,
    *,
    cr_id: str,
    story_title: str,
    story_id: str,
    source: str,
    occurred_at: str,
    discussion_url: str = "",
    story_url: str = "",
) -> str:
    """渲染一条 PRD 评论正文（纯文本，第一屏先说来源）。

    三种边界（设计稿 §4.3）：
    - 新增条款：原文位置写「未定位到对应段落」
    - 删除条款：建议位置固定写「【建议删除本段】」
    - 低置信：末尾追加 ⚠️ 声明

    `discussion_url` / `story_url` 在这里只作为纯文本列出（人眼可复制），
    可点的版本由调用方通过 `add_comment(link_url=comment_primary_link(...))`
    渲染成一个 link 元素——正文里写 URL 在客户端不会变成链接。
    """
    # 三处边界各自独立判定，别写成嵌套三元（可读性差且易漏分支）
    if cp.is_new_clause or not cp.current_text.strip():
        current = _NEW_CLAUSE_MARK
    else:
        current = cp.current_text.strip()
    if cp.change_type == "删除条款":
        suggested = _DELETE_MARK
    else:
        suggested = cp.suggested_text.strip()

    scope = [f"变更类型：{cp.change_type}"]
    if cp.modules:
        scope.append(f"涉及模块：{'、'.join(cp.modules)}")
    if cp.compliance and cp.compliance != "无":
        scope.append(f"合规影响：{cp.compliance}")

    who = f"{story_title}（#{story_id}）" if story_title else f"#{story_id}"
    lines = [
        f"{_SIGN} · {cr_id}",
        "",
        "【变更依据】",
        f"来源：{source or '未知来源'} · {occurred_at}",
        f"涉及需求：{who}",
        "",
        "【改什么】",
        f"  {cp.title or '（未命名）'}",
        f"  {' · '.join(scope)}",
        "",
        "当前PRD原文：",
        f"  {current}",
        "",
        "建议调整为：",
        f"  {suggested or '（未给出可替换表述）'}",
        "",
        "【凭什么】",
    ]
    if cp.evidence_quote:
        lines.append(f"  「{_one_line(cp.evidence_quote)}」")
        lines.append(f"  ——{cp.speaker or '未知'}，{cp.scenario or '讨论'}")
    else:
        lines.append(f"  （未取到讨论原话，场景：{cp.scenario or '未知'}）")
    if cp.rationale:
        lines.append(f"  {cp.rationale}")

    lines += [
        "",
        "【谁来定】",
        "  待产品确认：回复「采纳」/「忽略」/「调整：说明」，或在群卡片上直接点按钮",
        f"  {_ROLE_NOTE}",
    ]
    if cp.band == BAND_FLAGGED:
        lines.append(f"  {_FLAG_NOTE}")
    # 链接以纯文本列一行（人眼可复制）；可点的那个由调用方用 link 元素渲染
    shown = [(label, url) for label, url in
             (("原始讨论", discussion_url), ("需求", story_url)) if url]
    if shown:
        lines.append("")
        for label, url in shown:
            lines.append(f"{label}：{url}")
    return "\n".join(lines)


def comment_primary_link(discussion_url: str, story_url: str) -> str:
    """评论末尾要渲染成可点元素的那**一个**链接：优先原始讨论，其次 Story。

    只给一个：评论里挂两条 URL，读者还得在纯文本里挑，不如直接给最该点的那条。
    """
    return discussion_url or story_url or ""


def _one_line(text: str) -> str:
    """引用压成一行：评论里的原话换行会打断「——说话人」那行的归属。"""
    return " ".join(str(text).split())


# ── 飞书交互卡片（对齐飞书项目机器人的观感）──────────────────────────
#
# 为什么不再发纯文本：纯文本没有「空间 · 类型 · #ID」的层次、标题点不进 Story、
# 按钮无从谈起，产品在同一群里已经习惯飞书项目机器人的样子，两种形态并存会被当成
# 两种可信度。卡片结构照抄那个层次：标题行 → 灰色来源行 → 蓝色可点标题 → 字段行 →
# @人 → 查看详情。
#
# header 不给 template（白底）而不是彩色条：飞书项目机器人的提醒卡片就是白底加粗标题，
# 彩色 header 在我们群里会比它更抢眼，反而像告警。

_SPACE_FALLBACK = "飞书项目"


@dataclass
class StoryMeta:
    """卡片需要的 Story 坐标。全部来自 Meegle（或台账手填），**不猜**。

    单独一个 dataclass 而不是七个 kwargs：卡片与决策通知两处都要同一批字段，
    参数列表一长就会漏传（漏传只会让卡片少一行，不会报错，属于最难发现的那类 bug）。
    """

    story_id: str = ""
    story_title: str = ""
    story_url: str = ""
    space_name: str = ""
    story_type: str = "Story"
    status: str = ""
    # PRD 文档标题（卡片 PRD 列的可点文本）。Meegle 不返回它，只有取正文时才知道，
    # 所以允许为空 → 渲染层退回 Story 标题，不编造。
    prd_title: str = ""

    @property
    def source_line(self) -> str:
        """`星路产研需求管理（产研专用） · Story · #7130628617`（缺项自动跳过）。"""
        parts = [self.space_name or _SPACE_FALLBACK, self.story_type]
        if self.story_id:
            parts.append(f"#{self.story_id}")
        return " · ".join(p for p in parts if p)

    @property
    def title_link(self) -> str:
        """蓝色可点标题；没 URL 就退回纯文本（不编 URL）。"""
        title = self.story_title or self.story_id or "（无标题）"
        return f"[{title}]({self.story_url})" if self.story_url else title


def _card_builder() -> Any:
    """延迟 import：渲染层要能在没装 lark_channel 的环境（纯单测）里跑。"""
    from lark_channel import CardBuilder

    return CardBuilder()


def card_text(card: dict[str, Any]) -> str:
    """卡片里给人看的文本（含表格单元格），单测断言与日志用。

    为什么不直接对 dict 断言：卡片结构是 SDK 的事，测试要钉的是**人眼会看到的那几行字**；
    结构一变（比如 note 从 plain_text 塌成 markdown）测试会假失败，掩盖真实回归。
    表格单元格的文本也要进来，否则「卡片有没有列出 PRD 链接」这类断言无处可查。
    """
    parts: list[str] = []

    def _visit(node: Any) -> None:
        """递归收文本：网格/表格/列里的 markdown 与按钮文字都要算进来。

        递归而不是按固定层级取：卡片现在有 column_set 套 column_set（行网格里再套
        按钮组），写死层级会在下一层改版时静默漏掉整块文本。
        """
        if isinstance(node, list):
            for n in node:
                _visit(n)
            return
        if not isinstance(node, dict):
            return
        tag = node.get("tag")
        if tag == "button":
            parts.append(str((node.get("text") or {}).get("content", "")))
            return
        if tag == "table":
            for r in node.get("rows") or []:
                parts.extend(str(v) for v in r.values())
            return
        if "content" in node:
            parts.append(str(node.get("content") or ""))
        for key in ("columns", "elements"):
            if node.get(key):
                _visit(node[key])

    _visit((card.get("body") or {}).get("elements") or [])
    head = ((card.get("header") or {}).get("title") or {}).get("content", "")
    return "\n".join([head] + [p for p in parts if p])


def _at_line(at_open_ids: list[str] | None, at_names: list[str] | None) -> str:
    """@ 分两类：有 open_id 的用 `<at id=…>`（飞书渲染成蓝色可点并真推送），
    没对上的只写名字。

    绝不把 open_id 猜成人名；也不因为 @ 不上就不发——通知的唯一出口是这张卡片。
    去重是必须的：Meegle 里一个人常同时挂「产品/需求提出人/项目」多个角色，
    按角色出名单会把他 @ 三遍（实测同一群里出现过）。
    """
    seen: set[str] = set()
    parts: list[str] = []
    for value in [f"<at id={oid}></at>" for oid in (at_open_ids or [])] + \
                 [f"@{name}" for name in (at_names or [])]:
        if value in seen:
            continue
        seen.add(value)
        parts.append(value)
    return " ".join(parts[:6])


@dataclass
class CardItem:
    """卡片表格里的一行 = 一条变更（含它归属的 Story 与那篇 PRD）。

    为什么把 Story 塞进每条而不是卡片级：一场会牵涉多个 Story / 多篇 PRD，
    卡片级归属只能发 N 张卡、或把变更错归到某一张卡的 Story 上——两个都不对。
    """

    story: StoryMeta = field(default_factory=StoryMeta)
    prd_url: str = ""
    cr_id: str = ""
    point: ChangePoint = field(default_factory=ChangePoint)
    # 原文所在章节（如「二、产品规则 > 2.3 可见性」）与块直达链接
    # （`{prd_url}#{block_id}`，规范见 lark-cli `lark-doc` skill："block 直达链接"）。
    # 两者都由 watcher 在锚定后用 BlockAnchor 回填；取不到就留空，渲染层不写。
    section: str = ""
    jump_url: str = ""


def build_notice_card(
    items: list[CardItem],
    *,
    source: str,
    source_url: str = "",
    at_open_ids: list[str] | None = None,
    at_names: list[str] | None = None,
    actions: bool = True,
) -> dict[str, Any]:
    """一次讨论/一场会的全部变更合成**一张**卡片（逐条发会刷屏）。

    版式三段式：
    ① 变更依据（来源 + 链接）——先说"凭什么提这些变更"，可点回原讨论；
    ② 表格：Story / PRD / 变更项（含原文所在章节）/ 变更前 / 变更后；表后逐条给
       采纳/修正/忽略按钮（`actions=False` 关掉，单测与预览用）；
    ③ 待确认人。

    调用前提：评论**已经真写进 PRD**。dry_run 阶段不会走到这里
    （watcher._run 在 dry_run 分支直接落观测返回），所以文案可以写实。
    """
    stories = _distinct_stories(items)
    b = _card_builder()
    # width_mode=fill：让卡片撑满会话可用宽度。三列表格里每列只分到 ~1/5 宽，
    # 默认宽度下「变更前/变更后」只剩省略号（用户截图反馈）。配置项实测被飞书接受。
    b.config(width_mode="fill")
    # template="" → SDK 的 `if template:` 判空后不写 header.template，飞书渲染为白底
    # 加粗标题（与飞书项目机器人的提醒卡片一致）。默认 blue 是一条彩色标题栏，
    # 在群里比正文还抢眼，像告警不像提醒。
    b.header(f"📋 PRD 变更建议提醒（{len(items)} 条 · {len(stories)} 个 Story）"
             if items else "📋 PRD 变更建议提醒", template="")

    # ① 变更依据：来源必须带链接——产品要能一眼回到"这是哪次讨论说的"
    basis = f"**变更依据：** {source}"
    if source_url:
        basis += f"（[查看原始讨论]({source_url})）"
    b.markdown(basis)
    b.markdown(f"<font color='grey'>共 {len(items)} 条建议，涉及 "
               f"{len(stories)} 篇 PRD，已写入各文档评论待确认。</font>")

    # ② 变更表：**column_set 行网格**而不是原生 table。
    # 原因（实测）：飞书 table 单元格放不了按钮（data_type=button 与单元格塞数组都 HTTP 400），
    # 而按钮要跟随每一行（放同一列的单元格里）——只有网格能做到。
    if items:
        shown = items[:_TABLE_MAX_ROWS]
        b.raw(_grid_header())
        for it in shown:
            for element in _grid_row(it, actions):
                b.raw(element)
            b.raw({"tag": "hr"})
        if len(items) > _TABLE_MAX_ROWS:
            b.markdown(f"<font color='grey'>另有 {len(items) - _TABLE_MAX_ROWS} 条"
                       "见 PRD 评论。</font>")

    b.markdown("<font color='grey'>注：点右侧按钮即落状态；修正请按提示回复新表述。"
               "也可在 PRD 评论里回复「采纳 / 忽略 / 调整：…」确认。</font>")

    # ③ 待确认人
    at = _at_line(at_open_ids, at_names)
    if at:
        b.markdown(f"**待确认人：** {at}")
    b.markdown("<font color='grey'>点上方按钮即落状态；修正请按提示回复新表述。</font>")
    return dict(b.to_dict())


# 不分页：飞书 table 的 page_size 上限就是 10，给 20 会被
# clamp 回 10 —— 行数超过 10 就必然翻页。所以行数上限直接按 10 设，多出的写
# 「另有 N 条见 PRD 评论」，而不是做一张注定分页的表。
_TABLE_MAX_ROWS = 10


def _table_element(headers: list[str], rows: list[list[str]],
                   *, widths: list[str] | None = None,
                   page_size: int = 10) -> dict[str, Any]:
    """原生 table 元素（保留给**不需要按钮**的场合，如决策回执）。

    列宽只接受百分比：实测像素值（`width: 120`）飞书返回 HTTP 400，"20%" 通过。
    变更表已改用 column_set 网格（单元格放不了按钮），这里不再承担主表渲染。
    """
    cols: list[dict[str, Any]] = []
    for i, h in enumerate(headers):
        col: dict[str, Any] = {"name": f"col_{i}", "display_name": h,
                               "data_type": "lark_md"}
        if widths and i < len(widths):
            col["width"] = widths[i]
        cols.append(col)
    return {"tag": "table", "page_size": max(1, min(10, page_size)), "columns": cols,
            "rows": [{f"col_{i}": c for i, c in enumerate(r)} for r in rows]}


# 网格列与权重：变更前/后是决策依据，权重最大；Story 最窄。
# 操作列**不在这里**：6 列在中文会话宽度里会挤到每列只剩省略号（见 _grid_row
# 「行太长了」），改成每条变更两行卡片（见 _grid_row）。
_GRID_COLS: tuple[tuple[str, int], ...] = (
    ("Story", 2), ("PRD", 3), ("变更项", 5),
)
_DIFF_COLS: tuple[tuple[str, int], ...] = (
    ("变更前", 1), ("变更后", 1),
)


def _grid_header() -> dict[str, Any]:
    """表头只覆盖第一行（Story/PRD/变更项），diff 行自带标签。"""
    return {"tag": "column_set", "flex_mode": "none", "background_style": "grey",
            "columns": [{"tag": "column", "width": "weighted", "weight": w,
                         "elements": [{"tag": "markdown", "content": f"**{h}**"}]}
                        for h, w in _GRID_COLS]}


def _grid_row(it: CardItem, actions: bool) -> list[dict[str, Any]]:
    """一条变更 = 两行网格 + 一行按钮。

    版式：
      行1  [Story #ID] [PRD 标题] [变更项 + 类型 + 章节]
      行2  [变更前 …]              [变更后 …]
      行3  [✅ 采纳][✏️ 修正][🚫 忽略]      ← 单独一行，靠左，与上面两行左缘对齐

    为什么不继续用「6 列一行」：中文字符宽，6 列时「变更前/变更后」只剩 6~8 个字，
    被截成省略号，diff 反而看不出改了哪几个字——分行后每列能放下 2~3 倍文字。
    """
    out: list[dict[str, Any]] = []
    cells = [_story_cell(it.story), _prd_cell(it.prd_url, it.story), _change_cell(it)]
    out.append({"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": w,
         "elements": [{"tag": "markdown", "content": c}]}
        for c, (_, w) in zip(cells, _GRID_COLS, strict=False)]})
    diff = [_before_cell(it), _after_cell(it)]
    out.append({"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": w,
         "elements": [{"tag": "markdown", "content": f"<font color='grey'>{h}</font>\n{c}"}]}
        for c, (h, w) in zip(diff, _DIFF_COLS, strict=False)]})
    if actions and it.cr_id:
        out.append(build_review_buttons(it.cr_id))
    return out

def _distinct_stories(items: list[CardItem]) -> list[str]:
    seen: list[str] = []
    for it in items:
        if it.story.story_id and it.story.story_id not in seen:
            seen.append(it.story.story_id)
    return seen


def _story_cell(meta: StoryMeta) -> str:
    """Story 列：可点的 **Story 名称** + 一行灰色 `#ID · 状态`。

    第一行放**人认得的名称**，第二行灰字放 `#ID · 状态`（ID 只在精确对齐台账时才看）。
    名称缺失时退回 #ID（不编名字）。
    """
    name = meta.story_title or ""
    head = (f"[{name}]({meta.story_url})" if meta.story_url and name
            else (name or f"#{meta.story_id}"))
    if meta.story_url and not name:
        head = f"[#{meta.story_id}]({meta.story_url})"
    meta_bits = [f"#{meta.story_id}" if meta.story_id else "", meta.status]
    tail = " · ".join(b for b in meta_bits if b)
    return f"**{head}**" + (f"\n<font color='grey'>{tail}</font>" if tail else "")


def _prd_cell(prd_url: str, meta: StoryMeta) -> str:
    """PRD 列：**文档标题**（可点）。

    标题取自文档首块（`BlockAnchor.title`）。取不到时退回 Story 名称——两者常同名，
    但 Story 名可能带「【有变更】」这类前缀，所以**优先文档标题**、再退回。
    没 URL 就明说没有，绝不拿 prd_token 拼 URL（token 不是 URL 的一部分，拼出来打不开
    而卡片上看不出来）。
    """
    if not prd_url:
        return "<font color='grey'>（无 PRD 链接）</font>"
    label = meta.prd_title or meta.story_title or "打开 PRD"
    return f"[{label}]({prd_url})"


def _change_cell(it: CardItem) -> str:
    """变更项：标题 + 类型标签 + 原文所在章节（可点跳转到 PRD 对应段落）+ CR 编号。

    只有原文看不出这条规则在文档哪一节，
    而章节名 + 直达链接同时解决了"这是哪块"和"点进去看全文"。
    章节/链接都来自锚点结果（`card.py` 侧回填），取不到就省，不编。
    """
    cp = it.point
    tag = cp.change_type + (" · 合规" if "合规" in cp.compliance
                            and cp.compliance != "无" else "")
    if cp.band == BAND_FLAGGED:
        tag += " · ⚠️低置信"
    title = cp.title or _one_line(cp.suggested_text)[:30] or "（未命名）"
    where = ""
    if it.section:
        where = (f"\n<font color='grey'>章节：{it.section}</font>"
                 + (f"（[定位原文]({it.jump_url})）" if it.jump_url else ""))
    cr = f"\n<font color='grey'>{it.cr_id}</font>" if it.cr_id else ""
    return f"**{title}**\n`{tag}`{where}{cr}"


def _before_cell(it: CardItem) -> str:
    """变更前（左栏）：PRD 原文；定位到了就把这列变成指向该段落的链接。

    点原文可跳转到 PRD 位置，落在这一列最自然：点原文
    就是"去文档里看这句话在哪"。没定位到时是纯文本，不伪造链接。
    """
    cp = it.point
    if cp.is_new_clause or not cp.current_text.strip():
        return "<font color='grey'>（新增条款，PRD 无对应原文）</font>"
    text = _one_line(cp.current_text)[:120]
    return f"[{text}]({it.jump_url})" if it.jump_url else text


def _after_cell(it: CardItem) -> str:
    """变更后（右栏）：建议表述；删除条款固定写「【建议删除本段】」。"""
    cp = it.point
    if cp.change_type == "删除条款":
        return "**【建议删除本段】**"
    return _one_line(cp.suggested_text)[:120] or "<font color='grey'>（未给出表述）</font>"


def _change_line(cp: ChangePoint, cr_id: str) -> str:
    """一条变更一行：`CR-…  标题  [参数调整 · 需合规确认]`。"""
    tag = cp.change_type + (" · 需合规确认" if "合规" in cp.compliance
                            and cp.compliance != "无" else "")
    if cp.band == BAND_FLAGGED:
        tag += " · ⚠️AI 低置信"
    title = cp.title or _one_line(cp.suggested_text)[:24] or "（未命名）"
    head = f"{cr_id}  " if cr_id else ""
    return f"- {head}**{title}**  `{tag}`"


def build_decision_card(
    *, decision: str, cr_id: str, title: str, meta: StoryMeta,
    confirmer: str, story_url: str = "", extra: str = "",
) -> dict[str, Any]:
    """决策回执卡片（采纳/忽略/调整/超时）。采纳必须写明「正文还没改」。"""
    emoji, state = _DECISION_MARKS.get(decision, ("ℹ️", decision or "已处理"))
    b = _card_builder()
    b.header(f"{emoji} PRD 变更{state}")
    b.markdown(f"<font color='grey'>{meta.source_line}</font>")
    lines = [f"**{meta.title_link}**",
             f"**变更项：** {title or '（未命名）'}（{cr_id}）",
             f"**决策：** {state}"]
    if confirmer:
        lines.append(f"**确认人：** {confirmer}（PRD 评论回复）")
    if extra:
        lines.append(f"<font color='grey'>注：{extra}</font>")
    if decision == "adopt":
        lines.append("<font color='grey'>注：建议已确认，**PRD 正文需人工按评论表述更新**"
                     "（本机器人不代改正文）。</font>")
    b.markdown("\n".join(lines))
    url = story_url or meta.story_url
    if url:
        b.button("查看详情", url=url)
    return dict(b.to_dict())


_DECISION_MARKS = {
    # 用词必须与 decisions._MARKS 的台账状态一致（已采纳/已忽略/已调整）：
    # 卡片说「需调整」而台账写「已调整」，产品对着两处会以为是两件事。
    "adopt": ("✅", "已采纳"),
    "ignore": ("🚫", "已忽略"),
    "adjust": ("🔄", "已调整"),
    "timeout": ("⏰", "超时未决"),
}
