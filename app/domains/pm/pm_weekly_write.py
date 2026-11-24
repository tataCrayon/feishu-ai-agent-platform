"""PM Weekly Write - sht001 写操作的唯一出口

为什么要单独一个模块
--------------------
写用户主表是这套系统里唯一的破坏性动作。如果 agent 或任何 handler
都能随手写，出问题无法定位、无法回滚、无法审计。

所有写操作收敛到这里，好处：
  1. 只有一个地方拼 range 字符串，写错列的事故面收窄
  2. 强制「暂存 → 确认 → 落笔」两步走，不可能一条消息直接改表
  3. 写后回读校验，写错了立刻能发现

只写一列
--------
状态列（G）。不动 A-F、不动 H。改任务内容、改负责人这些都不在本模块能力内，
需要的话另开函数并单独评估——不要为了"方便"把写权限扩大。

追加行
------
`append` 那一组函数是唯一的例外：往「未开始事项」块尾部追加新行。
它不是「改已有数据」，是「把一件还没开始的事记下来」，两者风险面完全不同：
前者可能改错别人的内容，后者最坏情况是多出一行，删掉即可。
即便如此也走同样的两步确认，不提供一句话直接落笔的入口。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.domains.pm.pm_hub_engine import SPREADSHEET_TOKEN, fetch_sheet_values
from app.domains.pm.pm_weekly_plan import (
    SHEET,
    STATUS_COL_LETTER,
    VALID_STATUS,
    WeeklyItem,
    load_backlog,
)
from app.domains.pm.feishu_pm_sync import _run_lark_cli

logger = logging.getLogger("pm_hub.weekly_write")

# 暂存文件落在这里（feishu-agent-platform 根目录），跟 .threads.jsonl 这些放一起。
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent

# 待确认队列。key = sender_id，value = 一批改动。
#
# 为什么是「一批」而不是「一条」：他在 09:00 的日计划里看到的是编号 5/6/7，
# 顺口就说「5 7 已完成」。一次动作改多条是常态，单条槽位会被后一条覆盖，
# 表现为「只改了一条，另一条静默丢失」。
#
# 进程内内存存储：daemon 重启就丢，这是有意的——未确认的写入不应该跨重启存活。
_PENDING: Dict[str, "PendingBatch"] = {}

# 确认/取消的口令。放在这里是为了 daemon 命令层能统一识别，
# 不用把判断散落在各处。
CONFIRM_WORDS = frozenset({
    "确认", "确定", "是", "好", "好的", "嗯", "对", "ok", "okay", "yes", "改吧", "改",
    "可以", "行", "要", "要的",
})
CANCEL_WORDS = frozenset({
    "取消", "算了", "不用", "不改", "no", "cancel", "不用了", "先不用",
})

# 备忘池单独一套词。
#
# 为什么不能共用：「改表状态」问的是「这么改行不行」，答「好」很自然；
# 但备忘池问的是「这几条要不要记进工作簿」，人真正会说出口的是「记」，
# 不是「确认」。实测：「记」「记上」「记一下」「不记」「先不记」
# 全部接不住，落到意图路由后被判成闲聊 —— 他以为记上了，实际没记住。
# 这类「我说了它没反应」比不做按钮更伤信任，所以词表按槽位分开。
BACKLOG_CONFIRM_WORDS = CONFIRM_WORDS | frozenset({
    "记", "记吧", "记上", "记一下", "记了吧", "记下来", "记录下来", "存", "存一下", "存吧",
})
BACKLOG_CANCEL_WORDS = CANCEL_WORDS | frozenset({
    "不记", "不记了", "先不记", "别记", "不用记", "不存", "先放着", "先放放",
})


@dataclass
class PendingChange:
    sender: str
    item: WeeklyItem
    new_status: str
    week_label: str
    created_at: datetime


@dataclass
class PendingBatch:
    """一次待确认的写入，可能是 1 条也可能是 N 条。"""

    sender: str
    changes: List[PendingChange]
    week_label: str
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════
# 暂存与确认
# ══════════════════════════════════════════════════════════════════════

def _cell_range(item: WeeklyItem) -> str:
    col = STATUS_COL_LETTER
    return f"{SHEET}!{col}{item.row}:{col}{item.row}"


def stage_status_change(
    sender: str,
    item: WeeklyItem,
    new_status: str,
    week_label: str = "",
) -> str:
    """单条暂存。保留这个签名，是为了不让已有的调用方跟着改。"""
    return stage_batch(sender, [(item, new_status)], week_label)


def stage_batch(
    sender: str,
    pairs: List[tuple],
    week_label: str = "",
) -> str:
    """把一批改动放进待确认队列，返回给人看的预览文案。

    不写表。用户回「确认」才落笔。

    逐条校验，坏的那条单独挡掉并说明原因，好的照常进队列 ——
    因为「5 7 已完成」里如果 5 已经完成，没理由连 7 也一起不做。
    """
    created = datetime.now()
    accepted: List[PendingChange] = []
    rejected: List[str] = []

    for item, new_status in pairs:
        if new_status not in VALID_STATUS:
            rejected.append(
                f"第 {item.no} 条「{item.task}」：「{new_status}」不是合法状态，"
                f"只能选 {' / '.join(VALID_STATUS)}"
            )
            continue
        if item.status == new_status:
            rejected.append(f"第 {item.no} 条「{item.task}」已经是「{new_status}」，跳过。")
            continue
        accepted.append(
            PendingChange(
                sender=sender,
                item=item,
                new_status=new_status,
                week_label=week_label,
                created_at=created,
            )
        )

    if not accepted:
        return "\n".join(rejected) if rejected else "没有可改的条目。"

    _PENDING[sender] = PendingBatch(
        sender=sender,
        changes=accepted,
        week_label=week_label,
        created_at=created,
    )

    where = f"（{week_label}）" if week_label else ""
    body = [
        f"· 第 {c.item.no} 条 {c.item.task}：{c.item.status} → {c.new_status}"
        for c in accepted
    ]
    lines = [f"准备改 {len(accepted)} 条{where}", "", *body]
    if rejected:
        lines.extend(["", "没动：", *[f"· {r}" for r in rejected]])
    lines.extend(["", "回「确认」落笔，回「取消」放弃。"])
    return "\n".join(lines)


def has_pending(sender: str) -> bool:
    return sender in _PENDING


def pending_summary(sender: str) -> Optional[str]:
    batch = _PENDING.get(sender)
    if not batch:
        return None
    rows = [
        f"第 {c.item.no} 条「{c.item.task}」{c.item.status} → {c.new_status}"
        for c in batch.changes
    ]
    if len(rows) == 1:
        return f"{rows[0]}（待确认）"
    return f"{len(rows)} 条待确认：" + "；".join(rows)


def cancel(sender: str) -> Optional[str]:
    batch = _PENDING.pop(sender, None)
    if not batch:
        return None
    if len(batch.changes) == 1:
        c = batch.changes[0]
        return f"已取消：第 {c.item.no} 条「{c.item.task}」保持「{c.item.status}」。"
    kept = "、".join(f"第 {c.item.no} 条" for c in batch.changes)
    return f"已取消，{kept}都没动。"


# ══════════════════════════════════════════════════════════════════════
# 落笔
# ══════════════════════════════════════════════════════════════════════

def _write_cell(item: WeeklyItem, value: str) -> Dict:
    cmd = [
        "api", "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values",
        "--data", "-",
        "--as", "user",
    ]
    body = {"valueRange": {"range": _cell_range(item), "values": [[value]]}}
    return _run_lark_cli(cmd, input_data=json.dumps(body, ensure_ascii=False))


def _read_cell(item: WeeklyItem) -> Optional[str]:
    """写后回读。读不到不等于写失败，所以返回 None 时调用方应降级处理。

    复用 pm_hub_engine.fetch_sheet_values 而不是自己拼 GET 请求：
    那条路径已经在生产里跑通了，range 里的 ! 和 : 怎么转义它已经趟过坑。
    """
    try:
        values = fetch_sheet_values(SPREADSHEET_TOKEN, _cell_range(item))
        if values and values[0]:
            return str(values[0][0]).strip()
    except Exception as e:
        logger.warning("回读校验失败（不影响写入结果）: %s", e)
    return None


def _apply_one(c: PendingChange) -> str:
    """落一条并回读校验。返回这一条的结果行，不抛异常。"""
    item, new_status = c.item, c.new_status
    try:
        _write_cell(item, new_status)
    except Exception as e:
        logger.exception("写入状态失败 row=%s", item.row)
        return f"· 第 {item.no} 条「{item.task}」写入失败：{e}（工作簿没动，可以重试）"

    actual = _read_cell(item)
    if actual is None:
        return f"· 第 {item.no} 条「{item.task}」→ {new_status}（回读没取到值，建议扫一眼表）"

    if actual == new_status:
        logger.info("状态更新成功 row=%s %s -> %s", item.row, item.status, new_status)
        return f"· 第 {item.no} 条「{item.task}」→ {new_status}"

    logger.error("回读不一致 row=%s 期望=%s 实际=%s", item.row, new_status, actual)
    return (
        f"· 第 {item.no} 条「{item.task}」期望「{new_status}」，回读是「{actual}」，"
        f"位置 {_cell_range(item)}，请人工核对"
    )


def commit(sender: str) -> Optional[str]:
    """执行待确认的改动并回读校验。返回给用户的结果文案。"""
    batch = _PENDING.pop(sender, None)
    if not batch:
        return None

    results = [_apply_one(c) for c in batch.changes]

    if len(results) == 1:
        return results[0].lstrip("· ")
    return f"已处理 {len(results)} 条：\n" + "\n".join(results)


# ══════════════════════════════════════════════════════════════════════
# 备忘池：往「未开始事项」追加
# ══════════════════════════════════════════════════════════════════════

# 独立的暂存槽位。跟状态回写分开存 —— 两类动作的落点和确认文案完全不同，
# 共用一个槽位会在「先说要改状态、又发来一条备忘」时互相覆盖。
#
# 与 _PENDING 不同，这个槽位**落盘**。原因：状态回写是他当场说「5 已完成」，
# 上下文全在当下；而备忘池是机器人自己攒出来的、9 点问一句「这几条要不要记」，
# 他可能下午才回。daemon 重启就丢的话，这条事就静默消失了 —— 漏记比多问严重。
_PENDING_BACKLOG_PATH = PROJECT_DIR / ".backlog_pending.json"

# 暂存过期天数。这个槽位落盘后会一直存在，如果他不回「确认」也不回「取消」，
# 那条暂存会一直在，之后哪天他随口一句「确认」（其实想确认别的动作）就被劫走。
# 3 天没人理就当放弃 —— 账本 7 天内也不会再问同一件事，不会反复打扰。
_PENDING_BACKLOG_TTL_DAYS = 3


def _pending_path_key(sender: str) -> str:
    return str(sender)


def _load_pending_backlog() -> Dict[str, List["BacklogDraft"]]:
    try:
        mtime = _PENDING_BACKLOG_PATH.stat().st_mtime
    except Exception:
        return {}

    if (time.time() - mtime) / 86400 > _PENDING_BACKLOG_TTL_DAYS:
        logger.info("备忘池暂存已过期，丢弃：%s", _PENDING_BACKLOG_PATH)
        _PENDING_BACKLOG_PATH.unlink(missing_ok=True)
        return {}

    try:
        raw = json.loads(_PENDING_BACKLOG_PATH.read_text(encoding="utf-8"))
        return {
            k: [BacklogDraft(**d) for d in v]
            for k, v in raw.items() if isinstance(v, list)
        }
    except Exception:
        return {}


def _save_pending_backlog(d: Dict[str, List["BacklogDraft"]]) -> None:
    try:
        payload = {
            k: [asdict(x) for x in v] for k, v in d.items() if v
        }
        if not payload:
            _PENDING_BACKLOG_PATH.unlink(missing_ok=True)
            return
        _PENDING_BACKLOG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写备忘池暂存失败（内存里仍有）: %s", e)


@dataclass
class BacklogDraft:
    """一条准备写进「未开始事项」的事。

    只填四个字段：事项、优先级、负责人、备注。
    计划开始/完成日期留空 —— 这类事本来就没排期，填个假日期只会误导自己。
    """
    task: str
    priority: str = "中级"
    owner: str = "李四"
    remark: str = ""


def stage_backlog(sender: str, drafts: List[BacklogDraft]) -> str:
    """把事项放进待确认队列，返回预览文案。不写表。

    为什么不直接写：这类事写进表就沉底了，写错一条等于给他加了个假待办，
    比漏记更难受。多问一句「确认」的成本，远低于事后回去清理。
    """
    if not drafts:
        return "没有要记的事。"

    # 合并而不是覆盖：昨天问过但没回「确认」的那条还在队列里，
    # 不能被今天这批顶掉 —— 那等于静默丢了他本该看到的提醒。
    store = _load_pending_backlog()
    merged = store.get(sender, []) + list(drafts)

    seen = set()
    queue: List[BacklogDraft] = []
    for d in merged:
        task = (d.task or "").strip()
        if not task or task in seen:
            continue
        seen.add(task)
        queue.append(d)

    store[sender] = queue
    _save_pending_backlog(store)

    body = [
        f"{i}. {d.task}" + (f"（{d.remark}）" if d.remark else "")
        for i, d in enumerate(queue, 1)
    ]
    return "\n".join([
        f"准备记到「未开始事项」{len(queue)} 条",
        "",
        *body,
        "",
        "这些不急的事不进今天的待办，只记进工作簿。",
        "回「记」我就写进去，回「不记」就算了。",
    ])


def has_pending_backlog(sender: str) -> bool:
    return bool(_load_pending_backlog().get(sender))


def cancel_backlog(sender: str) -> Optional[str]:
    store = _load_pending_backlog()
    drafts = store.pop(sender, None)
    if not drafts:
        return None
    _save_pending_backlog(store)
    return f"已取消，{len(drafts)} 条都没记。"


def _write_backlog_row(row: int, d: BacklogDraft) -> Dict:
    """写一行：B 事项、C 优先级、D 负责人、H 备注。

    E/F/G（计划开始、计划完成、跟踪状态）留空 —— 没排期就写没排期，
    不拿假数据占位。只碰这一行的 B-H，不越界。
    """
    rng = f"{SHEET}!B{row}:H{row}"
    body = {"valueRange": {"range": rng, "values": [[
        d.task, d.priority, d.owner, "", "", "", d.remark,
    ]]}}
    cmd = [
        "api", "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values",
        "--data", "-",
        "--as", "user",
    ]
    return _run_lark_cli(cmd, input_data=json.dumps(body, ensure_ascii=False))


def _read_backlog_task(row: int) -> Optional[str]:
    try:
        values = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET}!B{row}:B{row}")
        if values and values[0]:
            return str(values[0][0]).strip()
    except Exception as e:
        logger.warning("回读未开始事项失败: %s", e)
    return None


def commit_backlog(sender: str) -> Optional[str]:
    """落笔：往「未开始事项」尾部逐行追加，写一行回读一行。

    中途失败就停，后面的不再写 —— 半截写入比整批放弃更难查。
    失败时**不清空队列**：没写进去的那几条还留着，他再说一次「确认」就能重试。
    """
    store = _load_pending_backlog()
    drafts = store.get(sender)
    if not drafts:
        return None

    try:
        bl = load_backlog()
    except Exception as e:
        logger.exception("读未开始事项失败")
        return f"读工作簿失败，这次没写：{e}"

    if not bl.header_row:
        return "没找到「未开始事项」表头，这次没写，先看一眼表。"

    results: List[str] = []
    done: List[BacklogDraft] = []
    seen = set()
    row = bl.next_row

    for d in drafts:
        key = d.task.strip()
        if bl.has(d.task) or key in seen:
            results.append(f"· 已在表里，跳过：{d.task}")
            done.append(d)
            continue
        seen.add(key)

        try:
            _write_backlog_row(row, d)
        except Exception as e:
            logger.exception("追加未开始事项失败 row=%s", row)
            results.append(f"· {d.task} 写入失败：{e}（再说一次「确认」可重试）")
            break

        actual = _read_backlog_task(row)
        if actual is None:
            results.append(f"· {d.task} → 已写（回读没取到值，建议扫一眼 B{row}）")
            done.append(d)
        elif actual == d.task:
            logger.info("未开始事项追加成功 row=%s task=%s", row, d.task)
            results.append(f"· {d.task} → 已记到 B{row}")
            done.append(d)
        else:
            logger.error("回读不一致 row=%s 期望=%s 实际=%s", row, d.task, actual)
            results.append(f"· {d.task} 回读是「{actual}」，位置 B{row}，请人工核对")
            break
        row += 1

    # 只移除真正处理过的，剩下没写的留在队列里等重试。
    written = {id(x) for x in done}
    rest = [x for x in drafts if id(x) not in written]
    if rest:
        store[sender] = rest
    else:
        store.pop(sender, None)
    _save_pending_backlog(store)

    return "\n".join(results) if results else "没有可写的内容。"