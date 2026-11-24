"""业务域能力 MCP server（stdio）— 薄封装，不加业务逻辑。

负责：把 `app/domains/` 与 `app/ops/scheduler/` 的现有入口包成 MCP 工具，
并统一 JSON 回执。
不负责：日报生成、合并判定、目录扫描、任务状态机——全部调用下层实现
（conventions L3：工具层不得与业务逻辑双写）。
调用方：dsh 按 `[projects.mcp.domains]` 以子进程挂载。
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from app.config import AppConfig, load_config
from app.domains.daily.aihot import (
    AihotError,
    get_daily_by_date,
    get_daily_pair,
    get_daily_report,
    render_push_text,
)
from app.domains.daily.card import build_daily_card, collect_links
from app.domains.skill_catalog.query import (
    find_similar,
    find_status_entries,
    format_catalog,
    format_inventory_overview,
    format_review,
    format_similar,
    format_status_board,
    format_status_card,
    review_entry,
)
from app.domains.skill_catalog.scan import CatalogScanner
from app.domains.skill_catalog.store import CatalogStore
from app.mcp._common import current_chat_id, fail, ok
from app.protocols.feishu_send import FeishuSendError, send_card

logger = logging.getLogger("mcp.domains")

mcp = FastMCP("domains")

PROJECT_DIR = Path(__file__).resolve().parents[2]

_cfg: AppConfig | None = None
_catalog: CatalogStore | None = None


def _config() -> AppConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def reset() -> None:
    """清进程内缓存（配置 + 目录 store）——单测替换配置用。"""
    global _cfg, _catalog
    _cfg = None
    _catalog = None


def _catalog_store() -> CatalogStore:
    """按 [projects.skill_catalog] 建 store（与 main.py 同一路径约定）。"""
    global _catalog
    if _catalog is None:
        sc = _config().skill_catalog
        bare = sc.bare_dir if Path(sc.bare_dir).is_absolute() else str(PROJECT_DIR / sc.bare_dir)
        snap = (sc.snapshot_path if Path(sc.snapshot_path).is_absolute()
                else str(PROJECT_DIR / sc.snapshot_path))
        _catalog = CatalogStore(
            CatalogScanner(
                repo_url=sc.repo_url,
                bare_dir=bare,
                skip_branches=sc.skip_branches,
                fetch_timeout_sec=sc.fetch_timeout_sec,
            ),
            snapshot_path=snap,
            refresh_interval_hours=sc.refresh_interval_hours,
        )
    return _catalog


@mcp.tool()
async def daily_report(date_iso: str = "") -> str:
    """取技术日报（AI HOT）。date_iso 留空=今天，否则 YYYY-MM-DD 取历史日报。

    返回 markdown 全文与推送文本；对端暂无日报时 available=false。
    """
    try:
        pair = await get_daily_by_date(date_iso) if date_iso else await get_daily_pair()
    except AihotError as e:
        logger.warning("[mcp.domains] daily_report failed: %s", e)
        return fail(f"日报获取失败：{e}（AI HOT 对端网络/协议异常，可稍后重试）")
    if not pair:
        return ok(available=False, note="AI HOT 暂无该日日报（成品尚未生成）")
    date_out, markdown, push_text = pair
    return ok(available=True, date=date_out, markdown=markdown, push_text=push_text)


@mcp.tool()
async def daily_report_card(date_iso: str = "") -> str:
    """把技术日报渲染成**卡片**发到当前会话（结构由代码钉死，你不拼卡片 JSON）。

    「发我日报 / 每日日报要点 / 总结一下今天的技术日报」这类请求优先用本工具：
    卡片已按要点头（日期）+ 板块分区 + 每条可点标题（aihot 站内页）排好，
    比你转述文本稳定得多。date_iso 留空=今天，否则 YYYY-MM-DD。

    回报两类事实，照实转述、不要补全：
    - available=false：对端还没有该日成品，如实说，别用别的日期顶替；
    - sent=false：卡片没发出去（原因在 error），把 result 里的 push_text
      原文作为纯文本发给用户，不要让用户空手而归。
    """
    try:
        report = await get_daily_report(date_iso)
    except AihotError as e:
        logger.warning("[mcp.domains] daily_report_card failed: %s", e)
        return fail(f"日报获取失败：{e}（AI HOT 对端网络/协议异常，可稍后重试）")
    if not report:
        return ok(available=False, note="AI HOT 暂无该日日报（成品尚未生成）")

    date_out = str(report.get("date") or "")[:10]
    card = build_daily_card(report)
    chat_id = current_chat_id()
    if not chat_id:
        return ok(available=True, sent=False, date=date_out,
                  error="没有会话上下文（PROMATE_CALLER_CHAT_ID 未注入），卡片无法投递",
                  card_links=collect_links(report)[:40])
    try:
        message_id = send_card(chat_id, card)
    except FeishuSendError as e:
        logger.warning("[mcp.domains] daily_report_card 发送失败: %s", e)
        push_text = render_push_text(report)
        return ok(available=True, sent=False, date=date_out, error=str(e)[:200],
                  push_text=push_text)
    return ok(available=True, sent=True, date=date_out, message_id=message_id,
              entries=len(collect_links(report)))


@mcp.tool()
async def skill_catalog_query(
    action: str = "overview",
    branch: str = "",
    name: str = "",
    query: str = "",
    refresh: bool = False,
) -> str:
    """查团队 Skill 目录（team-skills/skills 各分支的快照）。

    action: overview=资产盘点 / full=全量目录 / similar=立项查重（配合 query）/
    review=单条审查（配合 name）/ status=单条规范状态（配合 name）/ board=建设现状总表。
    refresh=true 才访问 git 仓库；普通查询只读本地快照。
    """
    store = _catalog_store()
    snap = store.load()
    if snap is None or refresh or store.is_stale(snap):
        snap, note = await store.refresh(force=refresh or snap is None)
        if snap is None:
            return fail(f"Skill 目录不可用：{note}")
    else:
        note = ""

    if action == "overview":
        text = format_inventory_overview(snap)
    elif action == "full":
        text = format_catalog(snap, branch=branch)
    elif action == "similar":
        if not query.strip():
            return fail("action=similar 需要 query（要查重的能力描述）")
        text = format_similar(snap, query, find_similar(snap, query))
    elif action == "review":
        if not name.strip():
            return fail("action=review 需要 name（Skill 名字）")
        text = format_review(review_entry(snap, name), snap)
    elif action == "status":
        if not name.strip():
            return fail("action=status 需要 name（Skill 名字）")
        text = format_status_card(find_status_entries(snap, name), snap)
    elif action == "board":
        text = format_status_board(snap)
    else:
        return fail(f"未知 action：{action!r}（overview/full/similar/review/status/board）")
    if note:
        text = f"{note}\n\n{text}"
    return ok(text=text, fetched_at=snap.fetched_at)


@mcp.tool()
def list_schedules(status: str = "") -> str:
    """列出定时任务档案（只读）。status 留空=全部，否则 pending/approved/paused/
    rejected/done/cancelled。

    这里是**纯只读快照**：创建与生命周期操作在 `capabilities` server 里
    （走审批卡 + 身份鉴权），不要在这边做写操作——重复实现会出现两套权限判定。
    """
    store = _schedule_store()
    from app.ops.scheduler.store import (
        STATUS_APPROVED,
        STATUS_CANCELLED,
        STATUS_DONE,
        STATUS_PAUSED,
        STATUS_PENDING,
        STATUS_REJECTED,
    )

    labels = {
        STATUS_PENDING: "待审批",
        STATUS_APPROVED: "调度中",
        STATUS_PAUSED: "已暂停",
        STATUS_REJECTED: "已驳回",
        STATUS_DONE: "已过期归档",
        STATUS_CANCELLED: "已取消",
    }
    wanted = [status] if status else list(labels)
    unknown = [s for s in wanted if s not in labels]
    if unknown:
        return fail(f"未知 status：{'/'.join(unknown)}（可选 {'/'.join(labels)}）")
    tasks = store.list_by_status(*wanted)
    return ok(total=len(tasks), tasks=[
        {
            "task_id": t.task_id,
            "status": t.status,
            "status_label": labels.get(t.status, t.status),
            "instruction": t.instruction,
            "agent": t.agent,
            "schedule": t.describe(),
            "expire_date": t.expire_date,
            "run_count": t.run_count,
            "last_run_ok": t.last_run_ok,
        }
        for t in tasks
    ])


def _schedule_store():
    from app.ops.scheduler.store import ScheduledTaskStore

    return ScheduledTaskStore()


if __name__ == "__main__":
    mcp.run()
