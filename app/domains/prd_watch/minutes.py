"""easyprd 妙记通道 — 显式映射（minutes_story_map）里的会议 → 同一条识别管道。

本模块只是**调度器**：素材读取与链接形态判定在 `meeting.py`（单源），这里负责
「配置里映射了哪些会、哪些还没处理过、喂给哪个 Story」。两处各写一份读取逻辑
必然漂移（纪要通道先读到、妙记通道还在用旧字段名）。

来源限定：只处理 `[projects.prd_watch].minutes_story_map` 里**显式配过**的
minute_token。**不做会议自动发现**——「哪场会对应哪个需求」属于人工台账，
让 bot 靠会议标题猜需求是设计稿 §6 明令禁止的猜测行为。
（按需求名搜会议是另一条**人工触发**的路径，见 `ingest` / `meeting_auto`。）

素材是「妙记摘要 + 待办」而不是逐字稿：逐字稿动辄上万字且口语噪声高，
摘要里留下的才是会上确认的结论。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.ledger import Ledger
from app.domains.prd_watch.meeting import MeetingMaterial, from_minute_token
from app.domains.prd_watch.watcher import PrdWatcher

logger = logging.getLogger("prd_watch.minutes")

# 已处理过的 minute_token（进程内去重；跨重启会重来一次，代价是重复识别，
# 而识别本身对同一素材是幂等的：同一批变更会被同一 CR 序号规则挡掉重复出评论）
_seen: set[str] = set()


async def fetch_minute_text(minute_token: str,
                            identity: str = "user",
                            base_url: str = "") -> tuple[str, list[str], str]:
    """读一场会的摘要与待办 → (送识别的文本, 错误列表, 可点链接)。

    保留这个签名是因为 `test_minutes.py` 与历史调用方按它断言；实现已收敛到
    `meeting.from_minute_token`，此处不再重复解析返回结构。
    """
    # identity 参数保留兼容：meeting 层固定走 user（bot 读不到他人妙记）
    del identity
    mat = await from_minute_token(minute_token, base_url=base_url)
    return mat.text, mat.errors, mat.minutes_url


async def poll_minutes(
    config: PrdWatchConfig, ledger: Ledger, watcher: PrdWatcher,
    *, binding: Any = None,
) -> dict[str, Any]:
    """处理配置里映射过的每一场会（未处理过的）。返回统计供日志与单测断言。

    `binding`（Meegle 绑定源）也要问：Story↔PRD/参与人 的事实源已经迁到飞书项目，
    只查多维表格等于「接了 Meegle 反而漏掉会议素材」。
    """
    stats: dict[str, Any] = {"checked": 0, "new": 0, "changes": 0, "errors": []}
    if not config.minutes_story_map:
        stats["skipped_reason"] = "未配置 minutes_story_map（妙记↔Story 显式映射）"
        return stats

    for token, story_id in config.minutes_story_map.items():
        token = str(token).strip()
        if not token or token in _seen:
            continue
        stats["checked"] += 1
        story_id = str(story_id)
        b = await binding.binding_for_story(story_id) if binding is not None else None
        if b is None:
            b = await ledger.binding_for_story(story_id)
        if b is None:
            stats["errors"].append(f"{token}: 台账与 Meegle 都没有 Story {story_id} 的绑定")
            continue
        mat = await from_minute_token(token, base_url=config.minutes_base_url)
        if mat.errors and not mat.text:
            # 缺 scope / 无读权限之类不可恢复的错误：记完跳过本轮，不重复刷同一条
            stats["errors"].extend(mat.errors)
            continue
        if not mat.text:
            stats["errors"].append(f"{token}: 妙记没有摘要/待办内容")
            continue
        _seen.add(token)
        stats["new"] += 1
        result = await watcher.handle_material(
            chat_id=b.chat_id, story_id=b.story_id,
            text=mat.text, source_label=mat.source_label,
            source_url=mat.source_url,
        )
        stats["changes"] += len(result.changes)
        logger.info("[prd_watch] 妙记 %s → story %s：%s",
                    token, story_id, result.summary())
    return stats


async def ingest(
    material: MeetingMaterial, chat_id: str, story_id: str, watcher: PrdWatcher,
) -> Any:
    """把任意来源的会议素材喂进管道（手动贴链接 / 自动扫描 / 妙记轮询共用）。

    `story_id` 为空时 `watcher.handle_material` 会退回「该群全部绑定」，
    由识别器在候选集里判归属；群也没绑定时它会如实报「无绑定」，不猜。
    """
    if not material.ok:
        return None
    return await watcher.handle_material(
        chat_id=chat_id, story_id=story_id, text=material.text,
        source_label=material.source_label, source_url=material.source_url)


def mark_seen(token: str) -> bool:
    """登记已处理；返回 True = 本次新登记（False = 之前处理过）。"""
    token = (token or "").strip()
    if not token or token in _seen:
        return False
    _seen.add(token)
    return True


def seen_tokens() -> set[str]:
    """已处理过的会议 token 只读视图（自动扫描按它跳过，不重复登记）。"""
    return set(_seen)


def reset_seen() -> None:
    """供测试与手动重跑（「重新识别那场会」）。"""
    _seen.clear()
