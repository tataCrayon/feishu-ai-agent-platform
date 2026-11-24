"""飞书项目（Meego）story —— 需求事实的唯一读取口。

职责边界：
    只回答三件事：某个迭代下有哪些需求、各自什么状态、现在卡在谁手上。
    不写工作簿（那是 pm_weekly_write）、不判业务归属（那是 pm_scope）、
    不起草话术（那是 pm_nudge）。

为什么要这一层：
    工作簿「产品线版本计划」里的需求行靠人工抄，批次和目标上线日已经互相打架
    （全表 18 条有名称记录里 5 处冲突）。改成以 story 为源、工作簿为视图，
    人工只需要维护 story，工作簿不再二次录入。

对外入口（都是纯函数，不缓存、不落盘）：
    resolve("11.26")            → 命中的迭代（名字 + id），用于「11.26 是哪个迭代」
    stories_of(["20261126"])    → 迭代下的需求行
    stories_named("分红")       → 按需求名反查（含状态、卡在谁手上）

MQL 四个坑（实测踩出来的，改代码前先读）：
    1. 单次最多回 50 条，`LIMIT n` 只截断不翻页 —— 必须走 session_id + group_pagination_list
       （`pm_project_mcp.search_mql` 已经封好，别自己拼 LIMIT 期望能拿全量）
    2. 表名带空间名：FROM examplecorp.story
    3. **story 上没有可用的「所属迭代」字段**：`field_edd7c0` 这个名字存在（不报错）
       但对全量 1389 条都是 null。迭代归属只在 sprint 侧反向挂着
       （`迭代下需求` = field_ae754c），所以「某迭代下有哪些需求」必须
       sprint → story id → 批量回读三步走。
    4. 按状态筛要用**中文 label**（`work_item_status = '6-实现中'`）；
       传 key（如 'started'）静默回 0 行，看起来像「没有这类需求」。
    5. `schedule`（排期）字段 SELECT 出来不返回，节点级时间只能走
       `pm_schedule_risk.plan_of` 的 `list_schedule`。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from app.domains.pm import pm_project_mcp as mcp  # 同目录裸 import，与 pm_daemon 的既有约定一致

logger = logging.getLogger("pm_hub.story_source")

STORY_TYPE = "story"
SPRINT_TYPE = "sprint"

# 单次 MQL 硬上限（服务端限制，`LIMIT n` 只截断不翻页）。
# 翻页由 `pm_project_mcp.search_mql` 负责，这里只用于表达「要全量」的意图。
LIMIT_SCAN = 3000

# 一次 `IN (...)` 里塞多少个 id。实测 100 个也回得出来，但不无限加长语句——
# SQL 长度与「某个 id 写错整条查询失败」的爆炸半径都随它线性增长。
ID_BATCH = 50

# 飞书项目状态标签 → 工作簿「状态」列口径。
# 键是标签的数字前缀（"6-实现中" → "6"）。
# 线上实际枚举：1-排期中 / 2-需求中 / 4-待评审 / 5-待开发 / 6-实现中 /
# 7-SIT测试 / 8-UAT验收 / 9-待上线 / 9-生产验证 / 10-已上线。
# **9 出现两次**（不同流程模板），所以关键档位按 label 精确覆盖、不能只按前缀归类——
# 「待上线」和「生产验证」的人不一样，前者催 PM 提工单，后者催 PM 验收。
_STAGE_BY_INDEX = {
    "1": "需求中",      # 排期中
    "2": "需求中",
    "3": "需求中",
    "4": "需求中",      # 待评审
    "5": "待开发",
    "6": "实现中",
    "7": "sit测试",
    "8": "uat测试",
    "9": "uat测试",
    "10": "已上线",
}

# 数字前缀分不开的档位按 label 覆盖（「9-」有两义）。
_STAGE_BY_LABEL = {
    "9-待上线": "生产验证",
    "9-生产验证": "生产验证",
    "8-UAT验收": "uat测试",
    "7-SIT测试": "sit测试",
}

# 到了这一档，说明开发已经交了，不必再问「提测了吗」。
HANDED_OVER_STAGES = frozenset({"sit测试", "uat测试", "生产验证", "已上线"})


@dataclass(frozen=True)
class Story:
    """一条需求在飞书项目里的当前快照。字段名对齐工作簿的语义，不抄字段 key。"""

    item_id: int
    name: str
    status: str          # 原始标签，如 "5-待开发"
    iteration: str       # 所属迭代名，如 "20261126"
    business: str        # 业务线全路径，如 "公募基金&投顾 / 公募基金"
    operator: str        # 当前负责人 —— 现在卡在谁手上，就是该被问的人
    priority: str
    schedule: str        # 排期原文，可能为空
    target: str          # 排期结束日（能解析出来才有）

    @property
    def stage(self) -> str:
        """归一化到工作簿口径。未识别的前缀原样返回，不猜。"""
        exact = _STAGE_BY_LABEL.get(self.status.strip())
        if exact:
            return exact
        idx = self.status.split("-", 1)[0].strip()
        return _STAGE_BY_INDEX.get(idx, self.status)

    @property
    def handed_over(self) -> bool:
        """开发已经交出去了。False 才需要去问「提测了吗」。"""
        return self.stage in HANDED_OVER_STAGES

    @property
    def line(self) -> str:
        """业务线的最细一级，用于对齐工作簿「业务线」列（公募基金 / 私募基金 / ESOP / 票据）。"""
        return self.business.split(" / ")[-1] if self.business else ""

    def row(self) -> str:
        """一行给人看。"""
        parts = [self.name, self.stage]
        if self.line:
            parts.append(self.line)
        if self.operator:
            parts.append(f"@{self.operator}")
        return " · ".join(parts)


# ══════════════════════════════════════════════════════════════════════
# MQL 值解码
# ══════════════════════════════════════════════════════════════════════

def _decode(value_type: str, value: Any) -> List[str]:
    """把一个字段的值统一解成字符串列表。未知类型不抛错，退化成 JSON 文本。"""
    if not value:
        return []

    if value_type == "string_value":
        return [value["string_value"]] if value.get("string_value") else []
    if value_type in ("long_value", "number_value"):
        raw = value.get("long_value", value.get("number_value"))
        return [str(raw)] if raw is not None else []
    if value_type == "key_label_value":
        label = (value.get("key_label_value") or {}).get("label")
        return [label] if label else []
    if value_type == "key_label_value_list":
        return [x["label"] for x in value.get("key_label_value_list") or [] if x.get("label")]
    if value_type == "user_value_list":
        out = []
        for u in value.get("user_value_list") or []:
            name = u.get("name_cn") or u.get("name_en") or u.get("email")
            if name:
                out.append(name)
        return out
    if value_type == "cascade_key_label_value":
        node = value.get("cascade_key_label_value") or {}
        path, cur = [], node
        while cur and cur.get("label"):
            path.append(cur["label"])
            children = cur.get("children") or []
            cur = children[0] if children else None
        return path
    if value_type == "schedule":
        sched = value.get("schedule") or {}
        spans = [sched.get("start_time"), sched.get("end_time")]
        return [str(x) for x in spans if x]
    if value_type == "date":
        raw = value.get("date") or value.get("long_value")
        return [str(raw)] if raw else []
    return [json.dumps(value, ensure_ascii=False)]


def _field_map(field_list: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """moql_field_list → {字段名: [值...]}。同时用 name 和 key 建索引，方便两种写法查。"""
    out: Dict[str, List[str]] = {}
    for f in field_list or []:
        values = _decode(f.get("value_type", ""), f.get("value") or {})
        for alias in (f.get("name"), f.get("key")):
            if alias:
                out.setdefault(alias, values)
    return out


def _first(fields: Dict[str, List[str]], *names: str) -> str:
    for n in names:
        got = fields.get(n)
        if got:
            return got[0]
    return ""


def _mql(statement: str) -> List[Dict[str, List[str]]]:
    return [_field_map(r.get("moql_field_list") or []) for r in mcp.search_mql(statement)]


# ══════════════════════════════════════════════════════════════════════
# 迭代
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Iteration:
    item_id: str
    name: str


def _normalize(text: str) -> str:
    """迭代名比对口径：去掉点、横杠、空格，让「11.26」「11-26」「1126」都能命中
    「财富票据v3.4.1（11.26）」和「20261126」。"""
    return "".join(ch for ch in (text or "").lower() if ch not in ". -_/（）()")


def resolve(keyword: str) -> List[Iteration]:
    """按片段找迭代。他说「11.26」「票据」时，用它定位到真实迭代名。

    不用 LIKE —— 直接拉全量在本地匹配，迭代总量百来个，一次往返就够。
    一次指令常常命中多个迭代（20261126 + 财富票据v3.4.1（11.26）），
    所以返回列表，调用方决定全拉还是让他挑。
    """
    rows = _mql(f"SELECT name, work_item_id FROM {mcp.PROJECT_KEY}.{SPRINT_TYPE} LIMIT {LIMIT_SCAN}")
    needle = _normalize(keyword)
    out = []
    for r in rows:
        name = _first(r, "名称", "name")
        if not name:
            continue
        if needle and needle not in _normalize(name):
            continue
        out.append(Iteration(item_id=_first(r, "工作项id", "work_item_id"), name=name))
    return out


def _iter_story_ids() -> Dict[str, List[str]]:
    """迭代名 → 该迭代下的需求 id 列表（sprint 侧 `迭代下需求` 反向挂载）。

    单独一个函数是为了让 `stories_of` 和 `stories_named` 都不必重复读一遍 sprint 表。
    值取 `key`（工作项 id）不是 `label`（需求名）：id 唯一，名字会重。
    """
    body = mcp.call_tool(
        "search_by_mql",
        {"project_key": mcp.PROJECT_KEY,
         "mql": f"SELECT name, work_item_id, `迭代下需求` FROM {mcp.PROJECT_KEY}.{SPRINT_TYPE} "
                f"LIMIT {LIMIT_SCAN}"},
    )
    out: Dict[str, List[str]] = {}
    for record in _raw_records(body):
        name, ids = "", []
        for f in record.get("moql_field_list") or []:
            key = f.get("key") or ""
            if key == "name":
                name = (f.get("value") or {}).get("string_value") or ""
            elif key == "field_ae754c":
                ids = [str(x.get("key") or "") for x in
                       ((f.get("value") or {}).get("key_label_value_list") or []) if x.get("key")]
        if name and ids:
            out.setdefault(name, []).extend(ids)
    return out


def _raw_records(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = body.get("data") or {}
    return [r for group in data.values() if isinstance(group, list) for r in group]


# 迭代名里的日期：8 位紧凑串（20261126），或任意位置的 M.D（11.26）。
_ITER_YMD = re.compile(r"(20\d{2})(\d{2})(\d{2})")
_ITER_MD = re.compile(r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})")


def _iter_sort_key(name: str) -> tuple:
    """按迭代名里的日期排序，无日期的排最后。用于多归属时挑「最早发版」那个。"""
    m = _ITER_YMD.search(name or "")
    if m:
        return (0, f"{m.group(1)}{m.group(2)}{m.group(3)}")
    hits = _ITER_MD.findall(name or "")
    if hits:
        month, day = hits[-1]
        return (0, f"0000{int(month):02d}{int(day):02d}")
    return (1, name or "")


def _owner_of_iteration() -> Dict[str, str]:
    """story id → 迭代名。一条需求可能挂在多个迭代下（实测 9/1034 条如此，
    多为「测试迭代 + 正式迭代」同时挂），取**名字日期最早**的那个：
    发版日取早的那次，催办只会更早不会漏掉。"""
    owners: Dict[str, str] = {}
    names_by_id: Dict[str, List[str]] = {}
    for name, ids in _iter_story_ids().items():
        for sid in ids:
            names_by_id.setdefault(sid, []).append(name)
    for sid, names in names_by_id.items():
        owners[sid] = sorted(names, key=_iter_sort_key)[0]
    return owners


# ══════════════════════════════════════════════════════════════════════
# 需求
# ══════════════════════════════════════════════════════════════════════

_STORY_SELECT = (
    "SELECT name, work_item_id, work_item_status, business, "
    "current_status_operator, priority "
)


def _to_story(r: Dict[str, List[str]], iteration: str = "") -> Story:
    item_id = _first(r, "工作项id", "work_item_id")
    return Story(
        item_id=int(item_id) if item_id.isdigit() else 0,
        name=_first(r, "名称", "name"),
        status=_first(r, "状态", "work_item_status"),
        # 迭代名不是 story 自己的字段（坑 3），由调用方从 sprint 侧带进来
        iteration=iteration or _first(r, "所属迭代", "迭代", "field_edd7c0"),
        business=" / ".join(r.get("业务线") or []),
        operator="、".join(r.get("当前负责人") or []),
        priority=_first(r, "优先级", "priority"),
        # 排期在 MQL 里读不出（`schedule` 字段不随 SELECT 返回，见模块顶部坑 3 相邻说明），
        # 真要排期走 `pm_schedule_risk.plan_of` 的 list_schedule，那里有节点级明细。
        schedule="",
        target="",
    )


def _stories_by_ids(ids: List[str], iteration_by_id: Optional[Dict[str, str]] = None
                    ) -> List[Story]:
    """按 id 批量取 story，自动分批。空 id 列表返回空——不发无谓请求。"""
    wanted = [str(i) for i in dict.fromkeys(ids) if str(i).strip()]
    out: List[Story] = []
    for i in range(0, len(wanted), ID_BATCH):
        chunk = wanted[i:i + ID_BATCH]
        statement = (
            f"{_STORY_SELECT}FROM {mcp.PROJECT_KEY}.{STORY_TYPE} "
            f"WHERE work_item_id IN ({', '.join(chunk)})"
        )
        for r in _mql(statement):
            sid = _first(r, "工作项id", "work_item_id")
            out.append(_to_story(r, (iteration_by_id or {}).get(sid, "")))
    return out


def stories_of(iteration_names: List[str]) -> List[Story]:
    """拉若干迭代下的全部需求。

    三步走（坑 3）：sprint 侧读 `迭代下需求` → 拿 id → 批量回读 story 字段。
    story 上的「所属迭代」全为 null，直接按它 WHERE 永远 0 行。
    """
    names = [n.strip() for n in iteration_names if n and n.strip()]
    if not names:
        return []
    mapping = _iter_story_ids()
    all_ids: List[str] = []
    for name in names:
        ids = mapping.get(name) or []
        if not ids:
            logger.warning("迭代「%s」在飞书项目里没有挂任何需求（或名字不精确）", name)
            continue
        all_ids.extend(ids)

    owner = _owner_of_iteration()
    by_id = {sid: name for name, ids in mapping.items() for sid in ids}
    return _stories_by_ids(all_ids, {sid: owner.get(sid, by_id.get(sid, "")) for sid in all_ids})


def stories_named(keywords: List[str]) -> List[Story]:
    """按需求名反查，含状态与卡在谁手上。

    用 MQL 的 `like` 在服务端筛（实测 `name like '%分红%'` 有效），不再全表拉
    1389 行本地过滤——那样每调一次就是 28 次往返，而且会随 story 增长线性变慢。
    """
    wanted = [k.strip() for k in keywords if k and k.strip()]
    if not wanted:
        return []
    conds = " OR ".join(f"name like '%{w.replace(chr(39), '')}%'" for w in wanted)
    rows = _mql(f"{_STORY_SELECT}FROM {mcp.PROJECT_KEY}.{STORY_TYPE} WHERE {conds} "
                f"LIMIT {LIMIT_SCAN}")
    if not rows:
        return []
    # 迭代名从 sprint 侧补齐（story 自己不带）
    iter_of = _owner_of_iteration()
    return [_to_story(r, iter_of.get(_first(r, "工作项id", "work_item_id"), "")) for r in rows]


def pending_handover(stories: List[Story]) -> Dict[str, List[Story]]:
    """按「当前负责人」分组，只留开发还没交付的。

    这是「去找对应开发问提测状态」的输入：谁手上压着，就问谁。
    """
    buckets: Dict[str, List[Story]] = {}
    for s in stories:
        if s.handed_over:
            continue
        buckets.setdefault(s.operator or "（无负责人）", []).append(s)
    return buckets


def stories_by_ids(ids: List[int]) -> Dict[int, Story]:
    """按工作项 id 批量回读当前状态。

    问询闭环用：问出去之后过几天再读一次，看状态有没有往前走。
    按 id 查而不是按迭代查 —— 中间迭代可能被调整，id 不会。
    """
    wanted = [str(i) for i in dict.fromkeys(ids) if i]
    if not wanted:
        return {}
    # 迭代名从 sprint 侧补（story 自己不带，坑 3）
    iter_of = _owner_of_iteration()
    out: Dict[int, Story] = {}
    for s in _stories_by_ids(wanted, iter_of):
        if s.item_id:
            out[s.item_id] = s
    return out