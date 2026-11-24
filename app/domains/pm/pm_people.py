"""open_id → 人名。

为什么需要这一层
--------------
飞书消息体里的 sender.name 基本是空串，只有 open_id，所以清单里一直是
「ou_817c1a989」这种。要变成「卢燕」得另查通讯录。

为什么必须批量
--------------
lark-cli 每次调用要 1-2 秒（node 启动），逐条查等于给每条消息加 2 秒。
所以只在「一批消息处理完」时集中查一次，并且缓存 7 天。

权限
----
走 user 身份，实际需要的是 contact:user.base:readonly（查用户信息）+
contact:user:search（搜索接口）。实测 contact:contact.base:readonly 不是必需的，
申请不到也不影响 —— 拿它只是为了「通讯录授权范围」，与查人无关。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.domains.pm.feishu_pm_sync import _run_lark_cli

logger = logging.getLogger("pm_hub.people")

_CACHE_PATH = Path(__file__).resolve().parents[3] / ".people_cache.json"

# 缓存 7 天。人员不常变，但改名 / 转岗要能跟上。
CACHE_TTL_SEC = 7 * 24 * 3600

# 单次 --user-ids 的条数。飞书没写明确上限，取保守值，多切几次比超限报错强。
BATCH = 20


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cache: Dict[str, Dict[str, Any]]) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("写人名缓存失败: %s", e)


def _query_batch(ids: List[str]) -> Dict[str, Dict[str, str]]:
    """一次 lark-cli 调用换一批名字。失败直接抛，由 _query 决定怎么降级。"""
    res = _run_lark_cli(
        [
            "contact", "+search-user",
            "--user-ids", ",".join(ids),
            "--as", "user",
            "--format", "json",
        ]
    )
    out: Dict[str, Dict[str, str]] = {}
    for u in (res.get("data") or {}).get("users") or []:
        oid = u.get("open_id")
        if not oid:
            continue
        out[oid] = {
            "name": u.get("localized_name") or u.get("name") or "",
            "dept": u.get("department") or "",
        }
    return out


def _query(ids: List[str]) -> Dict[str, Dict[str, str]]:
    """换名字。整批失败就逐个重试，**一个坏 id 不能拖垮整批**。

    飞书这个接口是全有或全无：传 20 个 id，只要有 1 个不存在（离职、外部租户、
    手误），整批返回 99992351，20 个人名全拿不到。实测过，所以必须逐个兜底。
    查不到的写进缓存标 invalid，7 天内不再重试，避免每次轮询都白撞一遍。
    """
    try:
        return _query_batch(ids)
    except Exception as e:
        if len(ids) == 1:
            logger.warning("通讯录查不到 %s（已标记，7 天内不再试）: %s", ids[0], str(e)[:120])
            return {ids[0]: {"name": "", "dept": "", "invalid": True}}

        logger.warning("批量查 %d 个失败，逐个兜底: %s", len(ids), str(e)[:120])
        out: Dict[str, Dict[str, str]] = {}
        for one in ids:
            out.update(_query([one]))
        return out


def resolve_many(ids: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """把一批 open_id 换成 {name, dept}。命中缓存的直接返回，未知的批量查。

    返回只包含查到的 id —— 调用方用 `names.get(id)` 判断，查不到就保留原样。
    """
    wanted = [i for i in dict.fromkeys(ids) if i and i.startswith("ou_")]
    if not wanted:
        return {}

    cache = _load()
    now = time.time()

    known: Dict[str, Dict[str, Any]] = {}
    for i in wanted:
        entry = cache.get(i)
        if entry and now - float(entry.get("at") or 0) < CACHE_TTL_SEC:
            known[i] = entry

    missing = [i for i in wanted if i not in known]
    if missing:
        for start in range(0, len(missing), BATCH):
            fetched = _query(missing[start:start + BATCH])
            for oid, info in fetched.items():
                cache[oid] = {**info, "at": now}
                known[oid] = cache[oid]
        _save(cache)
        logger.info("通讯录解析 %d 个，新查 %d 个", len(wanted), len(missing))

    return known


def name_of(open_id: str, fallback: str = "") -> str:
    """单个查询。只建议在低频路径上用（高频路径请用 resolve_many 批量）。"""
    info = resolve_many([open_id]).get(open_id) or {}
    return info.get("name") or fallback


def dept_of(open_id: str) -> str:
    return (resolve_many([open_id]).get(open_id) or {}).get("dept") or ""


def apply_to(items: Iterable[Any], id_attr: str = "sender_id", name_attr: str = "sender_name") -> None:
    """就地给一组对象回填人名。

    感知事件和过审条目字段名不同，但形状一样，所以用属性名参数化，
    免得两头各写一遍几乎相同的循环。
    """
    items = list(items)
    ids = [getattr(it, id_attr, "") for it in items]
    names = resolve_many(ids)
    for it in items:
        info = names.get(getattr(it, id_attr, "")) or {}
        if info.get("name"):
            setattr(it, name_attr, info["name"])


# ══════════════════════════════════════════════════════════════════════
# 反向：人名 → open_id
#
# 只在「真要发消息」时用得上。story 里只有中文名（当前负责人），
# 发私聊要 open_id，中间这一跳必须有，否则催办发不出去。
# ══════════════════════════════════════════════════════════════════════

def _search_by_name(name: str) -> List[Dict[str, Any]]:
    """走 contact:user:search。返回原始用户列表，调用方自己判重名。"""
    res = _run_lark_cli(
        [
            "contact", "+search-user",
            "--query", name,
            "--as", "user",
            "--format", "json",
        ]
    )
    return (res.get("data") or {}).get("users") or []


def open_id_of(name: str) -> str:
    """人名 → open_id。查不到返回空串，绝不猜。

    顺序：先翻缓存（open_id → name 的反正向），再搜通讯录。
    重名时只认 localized_name 完全相等的第一个 —— 搜「李」会返回一堆人，
    取第一个等于把消息发给不相干的人。
    """
    name = (name or "").strip()
    if not name:
        return ""

    # 先翻缓存，命中就不走网络
    for oid, info in _load().items():
        if (info.get("name") or "").strip() == name and not info.get("invalid"):
            return oid

    try:
        users = _search_by_name(name)
    except Exception as e:
        logger.warning("按名字搜不到 %s: %s", name, str(e)[:120])
        return ""

    exact = [u for u in users if (u.get("localized_name") or "").strip() == name]
    if not exact:
        logger.warning("「%s」没有精确匹配的账号（候选 %d 个），不发", name, len(users))
        return ""

    if len(exact) > 1:
        logger.warning("「%s」有 %d 个同名账号，取第一个", name, len(exact))

    u = exact[0]
    oid = u.get("open_id") or ""
    if not oid:
        return ""

    # 回写缓存，下一次不用再搜
    cache = _load()
    cache[oid] = {
        "name": u.get("localized_name") or name,
        "dept": u.get("department") or "",
        "at": time.time(),
    }
    _save(cache)
    return oid