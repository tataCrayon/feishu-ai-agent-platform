"""审批短链解析 — go.feishu.cn/approval/s/{code} → 审批单内容 + 附件。

实证结论（决定本模块形态）：
- 短链未登录 302 可拿到数字 InstanceID/UserID（go.feishu.cn → oa.feishu.cn
  ShortToLong → 小程序 applink）；数字 ID **不是** instance_code——直查
  GET /approval/v4/instances/{id} 报 1390003（user/bot 身份实测均如此）。
- instance_code 的合法来源是审批列表：lark-cli `approval tasks query`
  （topic 1待办/2已办/17未读知会/18已读知会，user 身份；「已发起」3 不在
  API 枚举内传了必报错，故不拉）返回
  UUID + summaries；`GET /approval/v4/instances/detail`（user 身份）返回
  完整 form JSON。
- form 的 attachmentV2/imageV2 widget：value 即 12 小时有效的附件地址（可多，
  逗号分隔），ext 是附件名——直接下载，无需 drive 接口。
- 关联（locate_instance 梯子）：L0 消息里流程编号 DATA\\d+ ↔ summaries/form
  「流程编号」精确匹配（工单场景最强键）；L1 短链数字 ID == 候选 task_id
  （理论精确键，但实测 InstanceID 与 task_id 不同雪花实例，生产≈不命中）；
  L2 待办+知会唯一候选自动选定；L3 歧义 → top-5 编号候选回序号。
  LocateResult.level 标注命中层级：L0/L1 精确可信，L2/L3 是启发式。

安全红线继承引擎侧：只读（GET/query），不点审批（不调 approve/reject/rollback）；
附件只下载到 logs/approvals/ 供工单管线消费。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("webportal.approval_link")

PROJECT_DIR = Path(__file__).resolve().parents[3]
APPROVALS_DIR = PROJECT_DIR / "logs" / "approvals"

APPROVAL_SHORT_LINK_RE = re.compile(
    r"https?://go\.feishu\.cn/approval/s/([A-Za-z0-9_-]+)")
# applink 小程序深链（审批卡片「可查看」跳转形态）：URL 里的 path 参数编码了
# instanceId。它和短链探测到的 InstanceID 是同一个数字 ID。
APPLINK_RE = re.compile(
    r"https?://applink\.feishu\.cn/client/mini_program/open\?[^\s)\]>\"']+")
_APPLINK_INSTANCE_RE = re.compile(r"instanceId(?:%3D|=)(\d{6,})", re.IGNORECASE)
_FLOW_NO_RE = re.compile(r"\b(DATA\d{6,})\b", re.IGNORECASE)

# 短链跟链探测上限（go → ShortToLong → applink 一般 2-3 跳）
_PROBE_MAX_HOPS = 3
_PROBE_TIMEOUT = 8.0
_LARK_TIMEOUT = 25.0
# 候选池主题。只列 API 真正支持的枚举（tasks query 帮助：1待办/2已办/
# 17未读知会/18已读知会）——「已发起」(3) 不在枚举内，传了必报 99992402
# field validation（实测），故不拉（发起人自己的单不在候选池，定位走
# L0 流程编号兜底链路）。
_TOPICS = ("1", "2", "17", "18")

GUIDANCE_REPLY = (
    "⚠️ 这个审批短链没能在你的审批列表里定位到对应单（内嵌/三方审批或你不是参与人）。\n"
    "请补充任一线索后我再试：\n"
    "• 流程编号（如 DATA2026100401）——最强线索\n"
    "• 审批单里的变更内容/主题关键词\n"
    "• 或直接把审批附件发给我（我照样解析）"
)


def build_guidance(link_kind: str = "", instance_id: str = "",
                   candidates: int = 0) -> str:
    """定位失败时的具体澄清引导——说清链接解析到了什么、比对了什么、缺什么。

    只说「换个说法再试试」等于把问题丢回用户；这里逐项摊开线索，用户照单
    补一条就能续跑。
    """
    lines = ["⚠️ 审批链接已解析，但没能定位到对应审批单。"]
    bits = []
    if link_kind:
        bits.append(f"链接类型：{link_kind}")
    if instance_id:
        bits.append(f"实例 ID `{instance_id}`")
    if bits:
        lines.append("（" + "，".join(bits) + "）")
    if candidates:
        lines.append(f"已比对你的审批列表 {candidates} 条（待办/已办/知会）仍未命中。")
    lines.append("常见原因：内嵌/三方审批不在列表接口覆盖内，或你不是该单参与人。")
    lines.append("请补充任一线索后我立即重试：")
    lines.append("• 流程编号（如 DATA2026100401）——最强线索")
    lines.append("• 审批单主题/变更内容关键词（如「删除 Web Portal 超级管理员」）")
    lines.append("• 或直接把审批附件发给我（我照样解析）")
    return "\n".join(lines)


# ── lark-cli 子进程封装（只读命令）────────────────────────────────

def _lark_cmd() -> str:
    return shutil.which("lark-cli") or "lark-cli"


async def _lark(*args: str,
                env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    """执行 lark-cli 子进程，返回解析后的 JSON（失败返回带 error 的 dict）。

    env_extra：按次注入环境变量（如 LARKSUITE_CLI_USER_ACCESS_TOKEN）覆盖
    keychain 登录身份——per-sender 授权态读审批的唯一身份选择通道。
    """
    cmd = [_lark_cmd(), *args]
    env = {**os.environ, **env_extra} if env_extra else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=_LARK_TIMEOUT)
    except Exception as e:
        logger.warning("lark-cli %s failed: %s", args[:3], e)
        return {"ok": False, "error": {"message": str(e)}}
    if proc.returncode != 0:
        msg = stderr.decode(errors="ignore")[:200]
        logger.warning("lark-cli %s rc=%s: %s", args[:3], proc.returncode, msg)
        return {"ok": False, "error": {"message": msg or f"rc={proc.returncode}"}}
    try:
        return json.loads(stdout.decode(errors="ignore"))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": {"message": f"json parse: {e}"}}


# ── 短链提取与探测 ───────────────────────────────────────────────

def extract_approval_codes(text: str) -> list[str]:
    """从用户文本里提取所有 go.feishu.cn 审批短链 code（去重保序，最多 3 个）。"""
    seen: set[str] = set()
    codes: list[str] = []
    for m in APPROVAL_SHORT_LINK_RE.finditer(text or ""):
        code = m.group(1)
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes[:3]


def extract_applink_instance_ids(text: str) -> list[str]:
    """从 applink 小程序深链里提取 instanceId（去重保序，最多 3 个）。

    深链的 path 参数是 URL 编码过的查询串，instanceId 藏在编码后的
    `instanceId%3D…` 里——正则同时认 `=` 和 `%3D`，不整段 decode
    （decode 整个 URL 会把参数分隔符也解开，反而切不出参数边界）。
    """
    seen: set[str] = set()
    ids: list[str] = []
    for m in APPLINK_RE.finditer(text or ""):
        im = _APPLINK_INSTANCE_RE.search(m.group(0))
        if not im:
            continue
        iid = im.group(1)
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    return ids[:3]


def extract_flow_numbers(text: str) -> list[str]:
    """提取流程编号（DATA\\d+，工单审批的 serialNumber——最强关联键）。"""
    return list(dict.fromkeys(m.upper() for m in _FLOW_NO_RE.findall(text or "")))


async def probe_short_link(code: str) -> dict[str, str]:
    """跟链探测短链（不携带登录态，≤3 跳），收集每跳 Location 参数。

    Returns: {"code", "instance_id", "user_id", "final_url"} —— 任一缺失即空串。
    尽力而为：探测失败/无 302 返回 {"code": code, ...空串}，不抛异常。
    """
    out = {"code": code, "instance_id": "", "user_id": "", "final_url": ""}
    url = f"https://go.feishu.cn/approval/s/{code}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT,
                                     follow_redirects=False) as client:
            for _ in range(_PROBE_MAX_HOPS):
                resp = await client.get(url)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get("location", "")
                out["final_url"] = loc or out["final_url"]
                for key, field_name in (("InstanceID", "instance_id"),
                                        ("UserID", "user_id"),
                                        ("instanceId", "instance_id")):
                    m = re.search(rf"(?:^|[?&]){key}=([^&#]+)", loc)
                    if m and not out[field_name]:
                        out[field_name] = m.group(1)
                if not loc:
                    break
                url = loc
    except Exception as e:
        logger.info("probe_short_link %s: %s", code, e)
    if out["instance_id"]:
        logger.info("approval link %s -> InstanceID=%s UserID=%s",
                    code, out["instance_id"], out["user_id"])
    return out


# ── 我的审批列表（关联候选池）────────────────────────────────────

def _task_to_candidate(task: dict) -> dict[str, Any]:
    """tasks query 条目 → 统一候选形状。"""
    return {
        "instance_code": task.get("instance_code") or "",
        "task_id": str(task.get("task_id") or ""),
        "definition_name": task.get("definition_name") or task.get("title") or "审批单",
        "title": task.get("title") or "",
        "topic": str(task.get("topic") or ""),
        "initiator_name": task.get("initiator_name") or "",
        "status": task.get("instance_status", task.get("status")),
        "summaries": task.get("summaries") or [],
    }


async def list_my_approvals(
        env_extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """拉取审批任务列表（5 个主题）→ 候选池；env_extra 有值则以该身份读。

    只读；单主题失败跳过不阻断。返回按 topic 权重 + task_id 倒序排列。
    """
    merged: dict[str, dict[str, Any]] = {}
    for topic in _TOPICS:
        # --page-all：默认 page-size=50，历史单多的账号单主题会超过一页；
        # 只取第一页会让更早的审批单从候选池里「消失」，表现为链接明明在
        # 我的已办里却定位不到。
        data = await _lark("approval", "tasks", "query", "--topic", topic,
                           "--page-all", "--page-size", "100", "--as", "user",
                           env_extra=env_extra)
        if not data.get("ok"):
            continue
        for task in ((data.get("data") or {}).get("tasks") or []):
            cand = _task_to_candidate(task)
            if not cand["instance_code"]:
                continue
            key = cand["instance_code"]
            prev = merged.get(key)
            # 同一实例多任务：保留 task_id 最新（雪花序大）那条
            if prev is None or cand["task_id"] > prev["task_id"]:
                merged[key] = cand
    return list(merged.values())


def _sort_candidates(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """topic 权重（待办>知会>已办）优先，同权重 task_id 新在前。"""
    order = {"1": 0, "17": 1, "18": 2, "2": 3}
    by_new = sorted(cands, key=lambda c: c["task_id"], reverse=True)
    return sorted(by_new, key=lambda c: order.get(c["topic"], 9))


# ── 关联梯子 ─────────────────────────────────────────────────────

@dataclass
class LocateResult:
    status: str = "none"              # matched | ambiguous | none
    candidate: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    level: str = ""                   # L0 流程编号 | L1 短链 ID | L2 唯一 | L3 列选 | ""


def _summary_text(cand: dict[str, Any]) -> str:
    return " ".join(str(s.get("key", "")) + " " + str(s.get("value", ""))
                    for s in cand.get("summaries") or [])


def locate_instance(probe: dict[str, str], query_text: str,
                    candidates: list[dict[str, Any]]) -> LocateResult:
    """三级关联：L0 流程编号精确 → L1 短链数字 ID==task_id → L2 待办/知会唯一。

    都不命中 → ambiguous（top-5 编号候选）或 none（池空）。
    level 字段标注命中层级——L0/L1 是精确键；L2 是启发式（调用方对
    「非本人池」的 L2 命中可降级为不信任，改走 per-sender 授权）。
    注：实测短链 InstanceID 与 task_id 不同雪花实例（L1 生产命中率≈0），
    流程编号 L0 才是主力精确键。
    """
    cands = _sort_candidates(candidates)

    # L0：消息里的流程编号（DATA\\d+）↔ summaries 文本
    flows = extract_flow_numbers(query_text)
    if flows:
        hits = [c for c in cands
                if any(f in _summary_text(c).upper() for f in flows)]
        if len(hits) == 1:
            return LocateResult("matched", hits[0], hits,
                                "流程编号精确匹配", "L0")
        if len(hits) > 1:
            return LocateResult("ambiguous", None, hits[:5],
                                f"流程编号命中 {len(hits)} 条", "L0")

    # L1：短链数字 ID（与任务 ID 同雪花号段）精确等值
    pid = (probe or {}).get("instance_id", "")
    if pid:
        hits = [c for c in cands if c.get("task_id") == pid]
        if hits:
            return LocateResult("matched", hits[0], hits,
                                "短链 ID 精确匹配", "L1")

    # L2：待办 + 知会唯一候选（无时间字段，按主题收敛）
    live = [c for c in cands if c.get("topic") in ("1", "17")]
    if len(live) == 1:
        return LocateResult("matched", live[0], live,
                            "待办/知会唯一命中", "L2")
    if len(live) > 1:
        return LocateResult("ambiguous", None, live[:5],
                            f"待办/知会命中 {len(live)} 条", "L2")

    # L3：全池歧义列选 / 空池
    if cands:
        return LocateResult("ambiguous", None, cands[:5],
                            "未精确命中，列候选", "L3")
    return LocateResult("none", None, [], "列表为空", "L3")


# ── 详情与附件 ───────────────────────────────────────────────────

_STATUS_ZH = {"PENDING": "审批中", "APPROVED": "已通过",
              "REJECTED": "已拒绝", "CANCELED": "已撤销", "DELETED": "已删除"}


def _widget_value_text(widget: dict) -> str:
    """form widget 的 value → 可读文本（radioV2 取 option.text 等）。"""
    val = widget.get("value")
    if val is None:
        return ""
    opt = widget.get("option")
    if isinstance(opt, dict) and opt.get("text"):
        return str(opt["text"])
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or val.get("value") or "")
    if isinstance(val, list):
        return "、".join(_widget_value_text({**widget, "value": v}) if isinstance(v, dict)
                        else str(v) for v in val)[:300]
    return str(val)[:300]


_ATTACHMENT_TYPES = {"attachment", "attachmentV2", "image", "imageV2"}


def _widget_attachments(widget: dict) -> list[dict[str, str]]:
    """附件/图片 widget → [{"name", "url"}]（value 为地址，ext 为名称串）。"""
    if widget.get("type") not in _ATTACHMENT_TYPES:
        return []
    val = widget.get("value") or ""
    urls = [u.strip() for u in (val.split(",") if isinstance(val, str)
                               else [str(v) for v in val]) if u.strip()]
    names_raw = str(widget.get("ext") or "").replace("'", "")
    names = [n.strip() for n in names_raw.split(",") if n.strip()]
    out = []
    for i, url in enumerate(urls):
        name = names[i] if i < len(names) else Path(url.split("?")[0]).name or f"附件{i + 1}"
        out.append({"name": name, "url": url})
    return out


def _snowflake_ms(value: Any) -> int | None:
    """雪花 ID → 毫秒时间戳（>>22）；非法值返回 None。"""
    try:
        return int(value) >> 22
    except (TypeError, ValueError):
        return None


async def resolve_by_instance_ids(
        instance_ids: list[str],
        candidates: list[dict[str, Any]],
        env_extra: dict[str, str] | None = None,
        limit: int = 0) -> LocateResult | None:
    """数字 instanceId 兜底定位（applink 深链 / 短链探测产物）。

    instanceId 与任务 task_id 是不同雪花对象（等值恒不中，L1 生产命中率≈0），
    开放平台也没有 instanceId→instance_code 的反查接口（拿它当 instance_code
    查详情报 1390003 实测）。可用的桥：实例的内部 ID 与其**首个节点任务**
    同毫秒生成——两者雪花毫秒（>>22）相等。故在候选池里
    逐候选拉详情，按「任务 ID 毫秒 ≈ instanceId 毫秒（±1）」找归属。

    精确键（task_id 等值）优先；毫秒对齐要求全池**唯一**命中才采纳，多命中
    报 ambiguous 交列选——宁可让用户点选，不猜错单。候选按梯子顺序取前
    limit 条（0 = 全池），并发拉详情（信号量 5）。limit 默认全池而不是截断：
    实测截到前 15 条会让真实目标落在截断线外（iXHU4TyL / iXb8g4HB 两条实测
    短链在 limit=15 时返回「未命中」、全池才命中），代价只是多几次只读 GET。
    """
    ids = [str(i) for i in dict.fromkeys(instance_ids or []) if str(i).isdigit()]
    if not ids or not candidates:
        return None
    id_set = set(ids)
    buckets = {b for b in (_snowflake_ms(i) for i in ids) if b is not None}
    if not buckets:
        return None

    cands = _sort_candidates(candidates)
    if limit > 0:
        cands = cands[:limit]

    # 精确键：task_id 等值（罕见但零成本）
    hits = [c for c in cands if str(c.get("task_id") or "") in id_set]
    if len(hits) == 1:
        return LocateResult("matched", hits[0], hits, "短链 ID 精确匹配", "L1")
    if len(hits) > 1:
        return LocateResult("ambiguous", None, hits[:5],
                            f"短链 ID 命中 {len(hits)} 条", "L1")

    # 兜底：实例 ID 创建毫秒 ↔ 候选任务 ID 毫秒对齐（±1 容忍时钟跨界）
    sem = asyncio.Semaphore(5)

    async def _scan(cand: dict[str, Any]) -> dict[str, Any] | None:
        async with sem:
            raw = await fetch_instance_raw(
                str(cand.get("instance_code") or ""), env_extra=env_extra)
        if not raw:
            return None
        for task in raw.get("tasks") or []:
            tid = task.get("id") if isinstance(task, dict) else None
            ms = _snowflake_ms(tid)
            if ms is not None and any(abs(ms - b) <= 1 for b in buckets):
                return cand
        return None

    matched = [c for c in await asyncio.gather(
        *(_scan(c) for c in cands)) if c is not None]
    if len(matched) == 1:
        return LocateResult("matched", matched[0], matched,
                            "短链实例 ID 创建毫秒对齐", "L1")
    if len(matched) > 1:
        return LocateResult("ambiguous", None, matched[:5],
                            f"实例 ID 毫秒对齐命中 {len(matched)} 条", "L1")
    return None


async def fetch_instance_raw(
        instance_code: str,
        env_extra: dict[str, str] | None = None) -> dict[str, Any] | None:
    """拉审批实例原始详情节点（含 tasks/comments），失败返回 None。"""
    data = await _lark("api", "GET", "/open-apis/approval/v4/instances/detail",
                       "--params", json.dumps({"instance_code": instance_code}),
                       "--as", "user", env_extra=env_extra)
    if not data.get("ok"):
        logger.warning("fetch_instance_raw %s failed: %s",
                       instance_code, (data.get("error") or {}).get("message", ""))
        return None
    node = data.get("data") or {}
    detail = node.get("data") or node  # 兼容 {data:{data:...}} / {data:...}
    return detail if isinstance(detail, dict) else None


async def fetch_instance_form(
        instance_code: str,
        env_extra: dict[str, str] | None = None) -> dict[str, Any] | None:
    """拉审批单详情（user 身份；env_extra 有值则以该身份读），解析 form。"""
    detail = await fetch_instance_raw(instance_code, env_extra=env_extra)
    if detail is None:
        return None
    try:
        widgets = json.loads(detail.get("form") or "[]")
    except json.JSONDecodeError:
        widgets = []
    lines: list[str] = []
    attachments: list[dict[str, str]] = []
    for w in widgets if isinstance(widgets, list) else []:
        if not isinstance(w, dict):
            continue
        name = str(w.get("name") or w.get("id") or "?")
        if w.get("type") in _ATTACHMENT_TYPES:
            atts = _widget_attachments(w)
            attachments.extend(atts)
            lines.append(f"{name}: 附件 {len(atts)} 个（{('、'.join(a['name'] for a in atts))[:80]}）")
        else:
            text = _widget_value_text(w)
            if text:
                lines.append(f"{name}: {text}")
    status = _STATUS_ZH.get(str(detail.get("status", "")), str(detail.get("status", "")))
    meta = {
        "definition_name": detail.get("definition_name") or "审批单",
        "status": status,
        "serial_number": str(detail.get("serial_number") or ""),
        "start_time": str(detail.get("start_time") or ""),
        "instance_code": instance_code,
    }
    return {"meta": meta, "text": "\n".join(lines), "attachments": attachments}


async def download_form_attachments(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    """下载附件到 logs/approvals/，返回 [{filename, file_path}]（handler 附件形状）。"""
    out: list[dict[str, Any]] = []
    if not items:
        return out
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for item in items:
            name = item.get("name") or "attachment"
            url = item.get("url") or ""
            if not url:
                continue
            dest = APPROVALS_DIR / f"{int(time.time())}_{Path(name).name}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                dest.write_bytes(resp.content)
            except Exception as e:
                logger.warning("attachment %s download failed: %s", name, e)
                continue
            out.append({"filename": name, "file_path": str(dest)})
    return out


def format_approval_content(form_text: str, meta: dict[str, Any],
                            attachments: list[dict[str, Any]] | None = None,
                            located_by: str = "") -> str:
    """【审批单】用户可读块（定位回执 + 表单 + 附件清单）。"""
    lines = ["📄 **审批单已读取**" + (f"（{located_by}）" if located_by else "")]
    lines.append(f"**{meta.get('definition_name', '审批单')}**"
                 + (f" · 流程编号 `{meta.get('serial_number')}`" if meta.get("serial_number") else "")
                 + f" · {meta.get('status', '')}")
    lines.append("")
    lines.append(form_text or "（表单为空）")
    if attachments:
        names = "、".join(a.get("filename", "?") for a in attachments)
        lines.append(f"\n📎 **附件已随工单携带**：{names}")
    return "\n".join(lines)
