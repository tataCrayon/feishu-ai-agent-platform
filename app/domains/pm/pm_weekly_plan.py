"""PM Weekly Plan - 运营轨解析（工作簿 sht001 每周计划跟踪）

为什么单独一个模块
------------------
`pm_hub_engine` 只读 sht000（研发轨：需求、责任人、闸门）。工作簿另一张表
sht001 是 PMO 自己的运营轨（合同台账、报价模型、客户对接…），此前
`SHEET_ID_WEEKLY` 常量定义了但全代码库零引用，等于对机器透明。

两张表的形态完全不同：
    sht000   规范二维表，A 列合并单元格表示版本归属
    sht001   分块表，每周一个「周期日历」标题行 + 一个表头行 + N 条数据行

所以不能用 parse_stories 那套行读取，必须按块切分。

真实结构（读自 2026-10-28）
--------------------------
    A        B            C        D              E          F          G        H
    周期日历  2026年7月第3周 2026-7-20 至 2026-7-24
    编号     工作事项      优先级别  负责人/对接人    开始日期    完成日期    跟踪状态  备注说明
    1       公、私募排期事项  高级     陈泽鑫          2026.7.20            进行中

回写约定
--------
`WeeklyItem.row` 是 1-based 真实行号，`STATUS_COL_LETTER` 是状态列。
状态回写 = 写 `{SHEET}!G{row}` 单个单元格，不碰其它列。
这个行号在解析时确定，不在回写时重算——重算会因表变动写错行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, List, Optional, Sequence

from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_WEEKLY,
    fetch_sheet_values,
)

logger = logging.getLogger("pm_hub.weekly_plan")

# ── 表结构 ────────────────────────────────────────────────────────────
SHEET = SHEET_ID_WEEKLY
READ_RANGE = f"{SHEET}!A1:H400"

COL_NO, COL_TASK, COL_PRIORITY, COL_OWNER = 0, 1, 2, 3
COL_START, COL_DUE, COL_STATUS, COL_REMARK = 4, 5, 6, 7
COL_LETTER = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G", 7: "H"}
STATUS_COL_LETTER = COL_LETTER[COL_STATUS]

# 表里出现的状态原话，归一到这四个
STATUS_TODO = "未开始"
STATUS_DOING = "进行中"
STATUS_DONE = "已完成"
STATUS_BLOCKED = "阻塞"
VALID_STATUS: tuple[str, ...] = (STATUS_TODO, STATUS_DOING, STATUS_DONE, STATUS_BLOCKED)

# 「周期日历」「编号」是块分隔标记，不是业务数据
_MARK_BLOCK = "周期日历"
_MARK_HEADER = "编号"

# 「未开始事项」是同一张表里的第二种块，结构和周块一样、语义完全不同：
# 没有周期日历、没有排期，只是「想起来但还没开始」的事项池。
# 它的表头 B 列写的是「未开始工作事项」，用它跟周块的表头区分。
_MARK_BACKLOG = "未开始事项"
_COL_TASK_BACKLOG = "未开始工作事项"

# 形如 7-6 / 7.6 / 7月6日 / 7/6 的写法都要认（格式示例，不是注释日期）
_DATE_RE = re.compile(r"(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})")


# ══════════════════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════════════════

@dataclass
class WeeklyItem:
    """一条运营事项。row 是回写锚点，不要在别处重算。"""
    no: Any
    task: str
    priority: str
    owner: str
    start: str
    due: str
    status: str          # 已归一化到 VALID_STATUS
    remark: str
    row: int             # 1-based 行号

    @property
    def done(self) -> bool:
        return self.status == STATUS_DONE


@dataclass
class WeekBlock:
    """一周的完整数据块。"""
    label: str                      # "2026年7月第 3 周"
    span: str                       # "2026-7-20 至 2026-7-24"
    start: Optional[date]
    end: Optional[date]
    header_row: int = 0
    items: List[WeeklyItem] = field(default_factory=list)

    def open_items(self) -> List[WeeklyItem]:
        return [i for i in self.items if not i.done]


@dataclass
class ActivePlan:
    """当前该看哪一周。before/after 都可能是 None。

    stale_days > 0 表示表已经停更了这么多天，推送时必须告诉用户，
    否则每天早上看到的是两个月前的旧事项还以为系统坏了。
    """
    block: Optional[WeekBlock]
    stale_days: int
    is_stale: bool


@dataclass
class BacklogRow:
    """「未开始事项」里的一行。row 是回写锚点，跟 WeeklyItem 一个道理。"""
    task: str
    priority: str
    owner: str
    start: str
    due: str
    status: str
    remark: str
    row: int


@dataclass
class Backlog:
    """「未开始事项」整块。

    跟周块的区别不只是形状：这类事没有日期，不该按「今天该做」来管。
    它的用途是「记着有这么回事，时机到了再捡起来」——
    所以写进来之后就不该每天催，只在他看表的时候出现。
    """
    header_row: int = 0
    rows: List[BacklogRow] = field(default_factory=list)

    @property
    def next_row(self) -> int:
        """下一个能写的行。

        块里已有数据就接在最后一条后面；空块就是表头下一行。
        不用「扫到第一个空行」——中间被人插了空行的话，那样会覆盖下面一条。
        """
        return (self.rows[-1].row + 1) if self.rows else self.header_row + 1

    def has(self, task: str) -> bool:
        """按事项名判重。

        这条路径每天都会走一遍，没有判重的话同一件事会一天多一行，
        一周之后这张表就没法看了。
        """
        key = _norm_task_key(task)
        return bool(key) and any(_norm_task_key(r.task) == key for r in self.rows)


# ══════════════════════════════════════════════════════════════════════
# 解析（纯函数，无网络）
# ══════════════════════════════════════════════════════════════════════

def _cell(row: Sequence[Any], idx: int) -> str:
    if idx >= len(row):
        return ""
    v = row[idx]
    return "" if v is None else str(v).strip()


def _norm_task_key(text: str) -> str:
    """事项名归一，只用于判重。

    同一件事在表里可以有无数种写法（多空格、多标点）。收敛到「只剩实词」，
    是为了让「跟进陈雨帆：反馈给燕姐」和「跟进陈雨帆 反馈给燕姐」不被当成两条。
    """
    t = str(text or "").strip().lower()
    for ch in " \t　·、，,。.：:；;（）()【】[]「」\"'“”":
        t = t.replace(ch, "")
    return t


def _parse_date(text: str) -> Optional[date]:
    m = _DATE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_span(span: str) -> tuple[Optional[date], Optional[date]]:
    """'2026-7-20 至 2026-7-24' -> (date, date)

    只按「至/到/~」切分。不能按 '-' 切，否则 2026-7-20 会被切成三段。
    """
    chunks = re.split(r"至|到|～|~", str(span or ""))
    dates = [d for d in (_parse_date(c) for c in chunks) if d]
    if len(dates) >= 2:
        return dates[0], dates[1]
    if dates:
        return dates[0], dates[0]
    return None, None


def _norm_status(raw: str) -> str:
    """把表里各种写法收敛到四个标准状态。

    真实表里出现过"已完成"/"进行中"，也允许用户手打"完成""卡住了"。
    收敛失败的保留原文，不硬塞默认值——错误数据应该看得见。
    """
    t = str(raw or "").strip()
    if not t:
        return STATUS_TODO
    if "完成" in t:
        return STATUS_DONE
    if "阻塞" in t or "卡" in t:
        return STATUS_BLOCKED
    if "进行" in t or t == "中":
        return STATUS_DOING
    return t


def parse_weekly_blocks(values: List[List[Any]]) -> List[WeekBlock]:
    """把 sht001 的原始二维数组解析成周块列表。

    状态机：
        空行        → 结束当前块
        「周期日历」 → 开新块
        「编号」     → 标记表头，进入数据区
        其它非空行   → 数据行（仅当已在数据区且 B 列有内容）
    """
    blocks: List[WeekBlock] = []
    cur: Optional[WeekBlock] = None
    in_data = False

    for idx, row in enumerate(values or []):
        row_no = idx + 1
        cells = list(row or [])

        if not any(c not in (None, "") for c in cells):
            cur, in_data = None, False
            continue

        c0 = _cell(cells, COL_NO)

        if c0 == _MARK_BLOCK:
            cur = WeekBlock(
                label=_cell(cells, 1),
                span=_cell(cells, 2),
                start=_parse_span(_cell(cells, 2))[0],
                end=_parse_span(_cell(cells, 2))[1],
            )
            blocks.append(cur)
            in_data = False
            continue

        if c0 == _MARK_HEADER:
            if cur:
                cur.header_row = row_no
            in_data = True
            continue

        if not (cur and in_data):
            continue

        task = _cell(cells, COL_TASK)
        if not task:
            continue

        cur.items.append(WeeklyItem(
            no=cells[COL_NO] if COL_NO < len(cells) else None,
            task=task,
            priority=_cell(cells, COL_PRIORITY),
            owner=_cell(cells, COL_OWNER),
            start=_cell(cells, COL_START),
            due=_cell(cells, COL_DUE),
            status=_norm_status(_cell(cells, COL_STATUS)),
            remark=_cell(cells, COL_REMARK),
            row=row_no,
        ))

    return blocks


def parse_backlog(values: List[List[Any]]) -> Backlog:
    """从同一张表里切出「未开始事项」块。

    为什么不给 parse_weekly_blocks 加分支：两个块的边界规则不一样。
    周块遇空行就结束；未开始事项块在表尾，中间的空行只是排版，
    当成结束的话，后面补的行就再也读不到了。

    状态机：
        「未开始事项」          → 进入块
        「编号」/「未开始工作事项」 → 记表头行
        「周期日历」            → 说明已经出了这个块，退出
        B 列非空的其它行        → 一条事项
    """
    bl = Backlog()
    inside = False

    for idx, row in enumerate(values or []):
        row_no = idx + 1
        cells = list(row or [])
        c0 = _cell(cells, COL_NO)

        if c0 == _MARK_BACKLOG:
            inside = True
            continue
        if not inside:
            continue
        if c0 == _MARK_BLOCK:
            inside = False
            continue

        if c0 == _MARK_HEADER or _cell(cells, COL_TASK) == _COL_TASK_BACKLOG:
            bl.header_row = row_no
            continue

        task = _cell(cells, COL_TASK)
        if not task:
            continue

        bl.rows.append(BacklogRow(
            task=task,
            priority=_cell(cells, COL_PRIORITY),
            owner=_cell(cells, COL_OWNER),
            start=_cell(cells, COL_START),
            due=_cell(cells, COL_DUE),
            status=_norm_status(_cell(cells, COL_STATUS)),
            remark=_cell(cells, COL_REMARK),
            row=row_no,
        ))

    return bl


# ══════════════════════════════════════════════════════════════════════
# 查询（纯函数）
# ══════════════════════════════════════════════════════════════════════

def latest_block(blocks: List[WeekBlock]) -> Optional[WeekBlock]:
    """表里最后一周。表停更时靠它兜底。"""
    return blocks[-1] if blocks else None


def find_block_for(blocks: List[WeekBlock], today: date) -> Optional[WeekBlock]:
    """找出 today 落在哪一周的区间内。"""
    for b in blocks:
        if b.start and b.end and b.start <= today <= b.end:
            return b
    return None


def resolve_active_plan(blocks: List[WeekBlock], today: date) -> ActivePlan:
    """决定「今天该看哪一周」。

    表在跟新 → 返回当周，is_stale=False
    表已停更 → 返回最后一周，并算出停更多少天
    """
    hit = find_block_for(blocks, today)
    if hit:
        return ActivePlan(block=hit, stale_days=0, is_stale=False)

    last = latest_block(blocks)
    if not last or not last.end:
        return ActivePlan(block=last, stale_days=0, is_stale=True)

    return ActivePlan(
        block=last,
        stale_days=(today - last.end).days,
        is_stale=True,
    )


def match_item(block: WeekBlock, key: str) -> List[WeeklyItem]:
    """按序号或标题定位事项。

    支持用户说「1 完成」「第 3 条」「私募周报 完成」三种表达。
    返回列表而不是单条——模糊匹配可能命中多条，由调用方决定是否要求澄清。
    """
    k = str(key or "").strip()
    if not k or not block:
        return []

    # 纯数字 → 优先按编号精确匹配
    if k.isdigit():
        n = int(k)
        exact = [i for i in block.items if str(i.no).strip().isdigit() and int(i.no) == n]
        if exact:
            return exact

    # 「第3条」「第 3 项」
    m = re.fullmatch(r"第\s*(\d+)\s*(?:条|项|个)?", k)
    if m:
        n = int(m.group(1))
        return [i for i in block.items if str(i.no).strip().isdigit() and int(i.no) == n]

    # 标题子串匹配
    return [i for i in block.items if k in i.task]


_TARGET_SEP = re.compile(r"[\s,，、/]+")


def split_targets(raw: str) -> List[str]:
    """把「5 7」「5、7」「5,7」拆成 ['5','7']。

    只在每个片段都是纯数字时才拆 —— 「私募 周报」这种带空格的事项名拆碎更糟。
    """
    tokens = [t.strip() for t in _TARGET_SEP.split(str(raw or "")) if t.strip()]
    if len(tokens) > 1 and all(t.isdigit() for t in tokens):
        return tokens
    return [str(raw or "").strip()]


def resolve_targets(block: WeekBlock, raw: str) -> tuple:
    """把用户给的一串指代解析成事项。返回 (items, problems)。

    problems 非空时调用方应整批不动 —— 半截执行比不执行更难查。

    为什么要支持一次多个：09:00 日计划推的就是编号清单，他顺口就是「5 7 已完成」。
    只认一个的话，第二条会被静默丢掉。
    """
    items: List[WeeklyItem] = []
    problems: List[str] = []
    for tok in split_targets(raw):
        hits = match_item(block, tok)
        if not hits:
            problems.append(f"没找到「{tok}」")
        elif len(hits) > 1:
            names = "、".join(f"{h.no}.{h.task}" for h in hits)
            problems.append(f"「{tok}」匹配到多条（{names}），是哪一条？")
        elif all(h.row != hits[0].row for h in items):
            items.append(hits[0])
    return items, problems


def open_items_of(block: Optional[WeekBlock]) -> List[WeeklyItem]:
    return block.open_items() if block else []
def stale_age(item: WeeklyItem, today: date) -> Optional[int]:
    """事项从开始日期挂到今天多少天。用于「这条拖了 106 天」这类提示。"""
    d = _parse_date(item.start)
    if not d or item.done:
        return None
    return (today - d).days


# ══════════════════════════════════════════════════════════════════════
# 取数
# ══════════════════════════════════════════════════════════════════════

def load_weekly_blocks() -> List[WeekBlock]:
    """读工作表并解析。失败抛异常，由调用方决定降级方式。"""
    values = fetch_sheet_values(SPREADSHEET_TOKEN, READ_RANGE)
    blocks = parse_weekly_blocks(values)
    logger.info("sht001 解析到 %d 个周块，最后一块: %s",
                len(blocks), blocks[-1].label if blocks else "无")
    return blocks


def load_active_plan(today: Optional[date] = None) -> ActivePlan:
    today = today or date.today()
    return resolve_active_plan(load_weekly_blocks(), today)


def load_backlog() -> Backlog:
    """读「未开始事项」块。

    跟 load_weekly_blocks 共用一次 READ_RANGE —— 同一张表，多读一次是浪费，
    但两个解析器读的区间一样、语义不同，合并反而更容易出错。
    """
    values = fetch_sheet_values(SPREADSHEET_TOKEN, READ_RANGE)
    bl = parse_backlog(values)
    logger.info("sht001 未开始事项 %d 条（表头行 %s）", len(bl.rows), bl.header_row)
    return bl