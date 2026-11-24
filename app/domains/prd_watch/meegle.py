"""飞书项目（Meegle）工作项读取 — Story 与其 PRD 链接、参与人的取数层。

为什么不用 lark-cli：lark-cli 没有 project/meegle 域（实测命令清单里没有）。走 Meegle
官方 MCP 端点 `https://project.feishu.cn/mcp_server/v1`（JSON-RPC 2.0 tools/call），
纯标准库实现，零新增依赖。

为什么凭证走环境变量而不是 config.toml：这是**个人访问凭证**（`m-` 开头），等于把持有人
在飞书项目的读权限借给机器人。落 config.toml 会随仓库外泄，落日志会进任务档案。

实测事实（细节见 .agent/context/…/CONTEXT.md §E）：
- 空间 examplecorp（星路产研需求管理）project_key=0000000000000000000000aa，story 共 1323 条
- Story 上就有 PRD 链接：字段 `field_c56ee5`「需求文档」值是 wiki URL
- 参与人在 `role_members`（PM/测试/需求提出人…，含姓名与 email）与 `work_item_current_node`
- MQL 必须 ``FROM `examplecorp`.`story` `` 且仍要传 project_key；`!= ''` 过滤不生效
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("prd_watch.meegle")

MCP_URL = os.environ.get("FEISHU_MCP_URL", "https://project.feishu.cn/mcp_server/v1")
_TOKEN_ENV = ("MEEGLE_USER_TOKEN", "FEISHU_MCP_TOKEN")
_TOKEN_FILE = Path.home() / ".claude" / "feishu_token.txt"

# 不打印 token 值，只打印来源与掩码（与 feishu-issue-reader 同口径）
_WIKI_RE = re.compile(r"(?:feishu\.cn|larksuite\.com)/(?:wiki|docx)/([A-Za-z0-9]+)")

# 设计稿字段（按可信度排序：先设计稿本体，再 UE 稿链接，最后 UI&UX 文档）。
# 三个都是 link 类型；实测《钱包管理&钱包绑定》三个全空（设计稿在「设计工单」里），
# 《AI_星路通pro_多语言》填在 field_e822f6——所以必须三个都试，不能只认一个 key。
DESIGN_FIELDS: tuple[tuple[str, str], ...] = (
    ("field_c8e024", "UE 设计稿"),
    ("field_e440ec", "UE 稿链接"),
    ("field_e822f6", "UI&UX设计文档"),
)

# get_story 默认一起取的字段：PRD/任务文档 + 设计稿三件套 + 业务线/优先级
_EXTRA_FIELDS: tuple[str, ...] = tuple(k for k, _ in DESIGN_FIELDS) + ("business", "priority")

# 「设计工单」是**独立工作项类型**（不是 Story 的字段）。实测：钱包管理这篇 Story
# 三个设计字段全空，稿子挂在设计工单 7130054949 的 `field_e53947`「设计稿链接」上。
# 设计工单与 Story 的关联在同名「需求名称」+ 反向字段 `field_0dbd5e`（该字段不可用于
# MQL 过滤，所以按名称匹配；名称是设计工单创建时从 Story 带过来的，等价性够用）。
DESIGN_WORKITEM_TYPE = "68a809890b4df12e7ac99b2b"
DESIGN_LINK_FIELDS: tuple[tuple[str, str], ...] = (
    ("field_e53947", "设计稿链接"),
    ("field_db3c9b", "交互稿链接"),
)


class MeegleError(RuntimeError):
    """取数失败（凭证缺失/过期、参数错、空间不可见）。message 面向用户，可直接回执。"""


def resolve_token() -> tuple[str, str]:
    """返回 (token, 来源说明)。找不到就抛，不静默返回空串去发无鉴权请求。"""
    for name in _TOKEN_ENV:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"env:{name}"
    if _TOKEN_FILE.is_file():
        value = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value, str(_TOKEN_FILE)
    raise MeegleError(
        "未找到飞书项目访问凭证。请到 project.feishu.cn → 头像 → 开发者中心 → 用户访问凭证"
        f"新建（m- 开头），写入 .env 的 {_TOKEN_ENV[0]}，或放 {_TOKEN_FILE}。")


def mask(token: str) -> str:
    return f"{token[:5]}…{token[-4:]}" if len(token) > 12 else "短token(已隐藏)"


@dataclass
class Story:
    """一个需求 Story（easyprd 需要的最小集）。"""

    story_id: str = ""
    title: str = ""
    status: str = ""
    prd_url: str = ""
    task_doc_url: str = ""
    owners: dict[str, str] = field(default_factory=dict)   # 角色名 → 姓名
    owner_emails: dict[str, str] = field(default_factory=dict)  # 角色名 → email
    node: str = ""
    url: str = ""
    # 空间显示名（卡片第一行「星路产研需求管理（产研专用） · Story · #ID」用）。
    # Meegle 详情里 owned_project.name 给的就是它，不用另外查一次空间列表。
    space_name: str = ""
    story_type: str = "Story"
    # 设计稿链接：Story 上三个 link 字段（实测 key 与显示名）
    #   field_c8e024「UE 设计稿」/ field_e822f6「UI&UX设计文档」/ field_e440ec「UE 稿链接」
    # 多数 Story 只填其中之一，所以按优先级取第一个非空；全空 = 没有设计稿，不猜。
    design_url: str = ""
    design_field: str = ""
    priority: str = ""
    business: str = ""

    @property
    def prd_token(self) -> str:
        """PRD 文档 token（wiki URL 里的 node token，需再用 drive inspect 换 obj_token）。"""
        m = _WIKI_RE.search(self.prd_url or "")
        return m.group(1) if m else ""

    @property
    def usable(self) -> bool:
        return bool(self.story_id and self.prd_url)


class MeegleClient:
    """JSON-RPC 会话（initialize → notifications/initialized → tools/call）。"""

    def __init__(self, simple_name: str = "examplecorp", timeout_sec: float = 25.0):
        self.simple_name = simple_name
        self.timeout_sec = timeout_sec
        self.token, self.token_source = resolve_token()
        self._session_id = ""
        self._initialized = False

    # ── 传输 ─────────────────────────────────────────────────────
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            # 端点会按 Accept 在 SSE 与裸 JSON 间切换，两种都得能吃（实测）
            "Accept": "application/json, text/event-stream",
            "X-Mcp-Token": self.token,      # 与 feishu-issue-reader 同款头
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            MCP_URL, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                sid = resp.headers.get("Mcp-Session-Id") or ""
                if sid:
                    self._session_id = sid
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            if e.code in (401, 403) or "10022" in detail or "expire" in detail.lower():
                raise MeegleError(
                    f"飞书项目凭证被拒（HTTP {e.code}）：token 可能已过期，"
                    f"请到开发者中心换新凭证（{mask(self.token)}）") from e
            raise MeegleError(f"飞书项目请求失败 HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise MeegleError(f"飞书项目不可达：{e.reason}") from e

        body: dict[str, Any] = {}
        for line in raw.splitlines():          # SSE 帧
            if line.startswith("data:"):
                body = json.loads(line[5:].strip())
                break
        if not body and raw.strip():
            body = json.loads(raw)
        if body.get("error"):
            raise MeegleError(f"飞书项目 MCP 错误：{json.dumps(body['error'], ensure_ascii=False)[:200]}")
        return body

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "feishu-agent-platform-easyprd", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_init()
        body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        result = body.get("result") or {}
        if result.get("structuredContent"):
            return result["structuredContent"]
        for item in result.get("content", []):
            try:
                data = json.loads(item.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            # 工具不报错而是把 error 塞在 raw 里（实测 MQL 语法错就是这个形态）
            raw = data.get("raw") if isinstance(data, dict) else None
            if isinstance(raw, list) and raw and str(raw[0]).startswith("error="):
                raise MeegleError(f"飞书项目查询失败：{str(raw[0])[:200]}")
            return data
        return {}

    # ── 业务查询 ─────────────────────────────────────────────────
    def project_key(self) -> str:
        """simple_name → project_key（每次进程启动查一次就缓存）。"""
        if self._key:
            return self._key
        data = self.call("search_project_info", {})
        for p in data.get("projects") or []:
            if p.get("simple_name") == self.simple_name or p.get("name") == self.simple_name:
                self._key = str(p.get("project_key") or "")
                return self._key
        names = [p.get("simple_name") for p in (data.get("projects") or [])]
        raise MeegleError(f"凭证可见的空间里没有 {self.simple_name}（可见：{names}）")

    _key = ""

    def story_url(self, story_id: str) -> str:
        """Story 详情页 URL。

        结构来自官方 Web 约定（`https://project.feishu.cn/<project_key>/story/detail/<id>`，
        与 feishu-issue-reader 解析链接时用的同一条规则）。卡片要「点得进 Story」，
        而 Meegle 接口本身不返回 URL（实测 get_workitem_brief 无任何 url 字段）——
        这是**按 project_key + work_item_id 拼装**，两个值都来自接口，不是猜的。
        """
        if not story_id:
            return ""
        return f"https://project.feishu.cn/{self.project_key()}/story/detail/{story_id}"

    def list_stories(self, limit: int = 30, status: str = "",
                     prd_field: str = "field_c56ee5",
                     task_field: str = "wiki") -> list[Story]:
        """列最近更新的 Story（含 PRD 链接与参与人）。

        MQL 不支持按字段非空过滤（实测 `!= ''` 无效），所以取回来在本地筛：
        有 PRD 链接的才可能出变更建议，没有的照实标出来（否则「为什么没建议」
        会变成又一个查不出来的洞）。
        """
        where = f"WHERE `状态` = '{status}'" if status else ""
        mql = (f"SELECT `工作项id`, `名称`, `状态`, `{prd_field}`, `{task_field}` "
               f"FROM `{self.simple_name}`.`story` {where} LIMIT {min(limit, 50)}")
        data = self.call("search_by_mql", {"project_key": self.project_key(), "mql": mql})
        stories = [_row_to_story(g) for bucket in (data.get("data") or {}).values()
                   for g in (bucket if isinstance(bucket, list) else [])]
        return [s for s in stories if s.story_id]

    def get_story(self, story_id: str, prd_field: str = "field_c56ee5",
                  task_field: str = "wiki") -> Story:
        """单个 Story 的详情（含角色参与人、当前节点、设计稿链接）。

        设计稿字段一并取：给一个 Story 链接就要能拿到它的 Figma 稿，
        否则「PRD↔UI 对账」每次都要人再贴一次链接（还会贴错 Story）。
        """
        data = self.call("get_workitem_brief", {
            "project_key": self.project_key(), "work_item_type": "story",
            "work_item_id": str(story_id),
            "fields": [prd_field, task_field, *_EXTRA_FIELDS]})
        attr = data.get("work_item_attribute") or {}
        owners: dict[str, str] = {}
        emails: dict[str, str] = {}
        for role in attr.get("role_members") or []:
            for m in role.get("members") or []:
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                owners.setdefault(str(role.get("name") or ""), name)
                if m.get("email"):
                    emails.setdefault(str(role.get("name") or ""), str(m["email"]))
        nodes = data.get("work_item_current_node") or []
        project = attr.get("owned_project") or {}
        wtype = attr.get("work_item_type") or {}
        story = Story(
            story_id=str(attr.get("work_item_id") or story_id),
            title=str(attr.get("work_item_name") or ""),
            status=str(((attr.get("work_item_status") or {}).get("name"))
                       or (nodes[0].get("name") if nodes else "") or ""),
            owners=owners, owner_emails=emails,
            node=str(nodes[0].get("name") or "") if nodes else "",
            space_name=str(project.get("name") or ""),
            story_type=str(wtype.get("name") or "Story"))
        story.url = self.story_url(story.story_id)
        design: dict[str, str] = {}
        for f in data.get("work_item_fields") or []:
            key = str(f.get("key") or "")
            value = _field_value(f.get("value"))
            if key == prd_field:
                story.prd_url = value
            elif key == task_field:
                story.task_doc_url = value
            elif key in dict(DESIGN_FIELDS):
                design[key] = value
            elif key == "priority":
                story.priority = value
            elif key == "business":
                story.business = value
        # 设计稿：按优先级取第一个非空；一个都没有就留空（不猜、不回退到图片块）
        for key, label in DESIGN_FIELDS:
            if design.get(key):
                story.design_url = design[key]
                story.design_field = label
                break
        return story

    def list_by_name(self, keyword: str, *, limit: int = 30,
                     prd_field: str = "field_c56ee5",
                     task_field: str = "wiki") -> list[Story]:
        """按名称片段查 Story（MQL `名称 LIKE %word%`），带设计稿字段。

        「同族需求」的判据是**名称前缀**（如 `【webportal】VA`）——那是产品线的
        人工约定，比让模型读标题猜关系可靠。这里只负责把候选查出来，
        关联与否由人确认，不自动建关系。
        """
        word = (keyword or "").strip().replace("'", "").replace("`", "")
        if len(word) < 2:
            return []
        extra = ", ".join(f"`{f}`" for f in (prd_field, task_field, *_EXTRA_FIELDS))
        mql = (f"SELECT `工作项id`, `名称`, `状态`, {extra} "
               f"FROM `{self.simple_name}`.`story` WHERE `名称` LIKE '%{word}%' "
               f"LIMIT {max(1, min(50, limit))}")
        data = self.call("search_by_mql", {"project_key": self.project_key(), "mql": mql})
        rows = [_row_to_story(g, prd_field=prd_field, task_field=task_field)
                for bucket in (data.get("data") or {}).values()
                for g in (bucket if isinstance(bucket, list) else [])]
        for story in rows:
            # MQL 不返回 URL，而卡片上同族需求必须是**可点蓝链**（纯文本等于让人
            # 自己复制 #ID 去搜）。URL 由 project_key + id 拼，两个值都来自接口。
            if story.story_id and not story.url:
                story.url = self.story_url(story.story_id)
        return [s for s in rows if s.story_id]

    def sprint_of(self, story_id: str, *, limit: int = 50,
                  prd_field: str = "field_c56ee5",
                  task_field: str = "wiki") -> tuple[str, str, list[Story]]:
        """Story 所属迭代 → (迭代名, 迭代ID, 同迭代的其他 Story)。

        为什么是「同迭代」而不是「同版本」：Story 上没有可读的迭代/版本字段
        （`actual_online_version` 实测为空），而 Sprint 侧有「迭代下需求」
        （`field_ae754c`）。一次 MQL 就能把最近 N 个迭代的成员一起取回，
        本地找命中——比逐个迭代 get_workitem_brief 快一个数量级（实测 50 个迭代
        逐个拉要 50 次往返）。

        「同版本」的另一种可靠来源是**名称里的版本号**（`V1.6.0`），由调用方
        按名称前缀+版本标记补充，本方法不猜。
        """
        mql = (f"SELECT `工作项id`, `名称`, `迭代下需求` "
               f"FROM `{self.simple_name}`.`sprint` LIMIT {max(1, min(100, limit))}")
        try:
            data = self.call("search_by_mql",
                             {"project_key": self.project_key(), "mql": mql})
        except MeegleError as e:
            logger.info("迭代查询失败: %s", str(e)[:120])
            return "", "", []
        target = str(story_id)
        for bucket in (data.get("data") or {}).values():
            for group in bucket if isinstance(bucket, list) else []:
                sprint_id = ""
                sprint_name = ""
                members: list[dict[str, str]] = []
                for item in group.get("moql_field_list") or []:
                    key = str(item.get("key") or "")
                    value = item.get("value") or {}
                    if key == "work_item_id":
                        sprint_id = str(value.get("long_value") or "")
                    elif key == "name":
                        sprint_name = str(value.get("string_value") or "")
                    elif key == "field_ae754c":
                        for entry in value.get("key_label_value_list") or []:
                            sid = str(entry.get("key") or "")
                            if sid:
                                members.append({"story_id": sid,
                                                "title": str(entry.get("label") or "")})
                if not any(m["story_id"] == target for m in members):
                    continue
                others = [m for m in members if m["story_id"] != target]
                stories = [Story(story_id=m["story_id"], title=m["title"],
                                 url=self.story_url(m["story_id"]))
                           for m in others]
                return sprint_name, sprint_id, stories
        return "", "", []

    def design_link_from_workorder(self, story_title: str, *,
                                   limit: int = 5) -> tuple[str, str, str]:
        """按 Story 名称找「设计工单」，返回 (设计稿链接, 字段名, 工单ID)。

        为什么靠名称匹配：设计工单与 Story 的关联字段 `field_0dbd5e` 是
        `workitem_related_select`，实测在 MQL 里过滤不了（返回空），而设计工单的
        「需求名称」是创建时从 Story 原样带过来的——用它做等价键最稳。
        名称太短（<4 字）不查：无关键词的 LIKE 会把整表捞回来。
        """
        name = (story_title or "").strip()
        if len(name) < 4:
            return "", "", ""
        safe = name.replace("'", "").replace("`", "")
        fields = ", ".join(f"`{k}`" for k, _ in DESIGN_LINK_FIELDS)
        mql = (f"SELECT `工作项id`, `需求名称`, {fields} "
               f"FROM `{self.simple_name}`.`{DESIGN_WORKITEM_TYPE}` "
               f"WHERE `需求名称` LIKE '%{safe}%' LIMIT {max(1, min(10, limit))}")
        try:
            data = self.call("search_by_mql",
                             {"project_key": self.project_key(), "mql": mql})
        except MeegleError as e:
            logger.info("设计工单查询失败(%s): %s", name[:20], str(e)[:120])
            return "", "", ""
        for bucket in (data.get("data") or {}).values():
            for group in bucket if isinstance(bucket, list) else []:
                found: dict[str, str] = {}
                work_id = ""
                for item in group.get("moql_field_list") or []:
                    key = str(item.get("key") or "")
                    value = item.get("value") or {}
                    text = str(value.get("string_value") or "").strip()
                    if key == "work_item_id":
                        work_id = str(value.get("long_value") or "")
                    elif text:
                        found[key] = text
                # 名称必须与 Story 名一致（LIKE 是包含匹配，会带出「钱包管理」别的工单）
                if found.get("name", "").strip() != name:
                    continue
                for key, label in DESIGN_LINK_FIELDS:
                    if found.get(key):
                        return found[key], label, work_id
        return "", "", ""

    def design_link_for(self, story_id: str) -> tuple[str, str]:
        """Story → (设计稿链接, 字段名)。三个设计字段全空时返回空串。

        注意：部分需求的稿子挂在「设计工单」工作项上，而不是 Story 的 link 字段
        （实测《钱包管理&钱包绑定》三个字段全空，但 UI 上「设计工单」有关联项）。
        Meegle MCP 不支持查询设计工单类型（`search_by_mql` 对该 type 返回空），
        所以这里只给 Story 字段能拿到的那部分；拿不到就如实说没有，不去猜。
        """
        story = self.get_story(story_id)
        return story.design_url, story.design_field


def _field_value(raw: Any) -> str:
    """Meegle 字段值 → 字符串（str / {"value":…} / {"label":…} / None 四种形态）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("value", "label", "name", "text", "link"):
            if raw.get(key):
                return str(raw[key]).strip()
        return ""
    if isinstance(raw, list):
        for item in raw:
            text = _field_value(item)
            if text:
                return text
        return ""
    return str(raw).strip()


def _row_to_story(group: dict[str, Any], *, prd_field: str = "field_c56ee5",
                  task_field: str = "wiki") -> Story:
    fields: dict[str, Any] = {}
    for item in group.get("moql_field_list") or []:
        value = item.get("value") or {}
        for key in ("string_value", "long_value"):
            if key in value:
                fields[item.get("key", "")] = value[key]
                break
        if item.get("key") == "work_item_status":
            lst = (value.get("key_label_value_list") or [{}])
            fields["status_label"] = str(lst[0].get("label") or "")
    design = ""
    design_field = ""
    for key, label in DESIGN_FIELDS:
        if str(fields.get(key) or "").strip():
            design = str(fields[key]).strip()
            design_field = label
            break
    return Story(
        story_id=str(fields.get("work_item_id") or ""),
        title=str(fields.get("name") or ""),
        status=str(fields.get("status_label") or ""),
        prd_url=str(fields.get(prd_field) or ""),
        task_doc_url=str(fields.get(task_field) or ""),
        design_url=design, design_field=design_field,
        priority=str(fields.get("priority") or ""),
        business=str(fields.get("business") or ""))
