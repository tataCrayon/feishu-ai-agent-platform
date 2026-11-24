"""绑定解析 — Story↔PRD↔参与人 从飞书项目（Meegle）自动取，群 ↔ Story 仍由人给。

分工的理由（实测，见 .agent/context/…/CONTEXT.md §E）：
- Meegle 上**已经有**的东西不该再让人抄一遍进多维表格：Story 标题/状态、PRD 链接
  （字段 `field_c56ee5`「需求文档」）、参与人（`role_members` 的 PM/测试/需求提出人）。
- Meegle 上**没有**的东西不能猜：需求讨论在哪个飞书群。Story 没有 chat_id 字段
  （只有 `group_type` 拉群方式配置），靠群名去 im +chat-search 模糊匹配群 = 设计稿
  §6 明令禁止的猜测行为（绑错就把变更建议写到别人的 PRD 上了）。

所以：`story_chat_map`（StoryID → chat_id）是管理员唯一要维护的一项，其余全自动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.domains.prd_watch.ledger import StoryBinding
from app.domains.prd_watch.meegle import MeegleClient, MeegleError, Story
from app.protocols.lark_cli import LarkCli

logger = logging.getLogger("prd_watch.binding")

# 参与人角色 → 通知里的位置。Meegle 角色名是空间自定义的，命不上就不 @（不瞎猜人）
_NOTIFY_ROLES = ("产品", "PM", "研发", "测试", "QA", "需求提出人")

# 「需求文档」字段里能当 PRD 的链接形态。实测 examplecorp 最近 30 条 Story 里，该字段
# 有 2 条填的是内部 git 仓库 tree 地址（git 仓库 tree 地址…）：那不是可评论的文档，
# 打给 lark-cli 只会回一串 JSON 残片错误。先把形态判掉，既不浪费一次子进程调用，
# 也能在 snapshot 里给出「这条 Story 的 PRD 不在飞书上」这种能听懂的话。
_DOC_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_DOC_PATH_KINDS = ("/wiki/", "/docx/", "/docs/", "/drive/", "/file/")


def doc_url_kind(url: str) -> str:
    """飞书文档链接 → 类型（wiki/docx/…）；不是飞书文档链接 → 空串。"""
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not _host_ok(host):
        return ""
    path = u.path or ""
    for kind in _DOC_PATH_KINDS:
        if kind in path:
            return kind.strip("/")
    return ""


def _host_ok(host: str) -> bool:
    """只认 `*.feishu.cn` / 裸域本身 —— 带前导点是为了挡住 evilfeishu.cn 这种蹭名域。"""
    return any(host == s.strip(".") or host.endswith(s) for s in _DOC_HOST_SUFFIXES)


def _clean_url(raw: str) -> str:
    """取字段里 URL 的首段——产品常在同一字段写「链接 + 空格 + 备注」。

    实测样本：`https://…/wiki/CxNkwgOyRig2RvkXE1KcEAscnCe ###第三节！`。整串交给
    lark-cli 时尾注会混进请求里，报错还是 `lacks permission`，就会被误判成授权问题
    （假阳性）。先切首段，再判形态。
    """
    text = (raw or "").strip()
    return text.split()[0] if text else ""


def _brief_err(exc: Exception) -> str:
    """三方错误 → 一句话。lark-cli 的原始输出带 JSON 残片，直接进台账没法看。"""
    text = str(exc).replace("\n", " ").strip()
    low = text.lower()
    if "lacks permission" in low or "permission_denied" in low or "131006" in low:
        return "PRD 文档未对机器人身份开放（需 wiki 空间/文档授权，重试无用）"
    return text[:120]


@dataclass
class BindingSource:
    """Meegle + 配置的群映射 → StoryBinding（带解析缓存，一天内不重复打三方）。"""

    client: MeegleClient | None = None
    lark: LarkCli = field(default_factory=LarkCli)
    story_chat_map: dict[str, str] = field(default_factory=dict)   # story_id → chat_id
    prd_field: str = "field_c56ee5"
    task_field: str = "wiki"
    include_status: tuple[str, ...] = ()                            # 空=不按状态筛
    limit: int = 30

    def __post_init__(self) -> None:
        # chat_id → 该群全部绑定。story_chat_map 允许多个 Story 指同一个群，
        # 旧实现用 dict[chat]=binding 直接覆盖 → 第二个 Story 的 PRD 永远不会被评论
        # （一场会牵涉多个 Story / 多篇 PRD）。
        self._by_chat: dict[str, list[StoryBinding]] = {}
        self._by_story: dict[str, StoryBinding] = {}
        self._errors: list[str] = []

    # ── 对外 ─────────────────────────────────────────────────────
    async def bindings_for_chat(self, chat_id: str) -> list[StoryBinding]:
        """一个群的全部 Story 绑定（一群多 Story 是常态，不是一份）。"""
        await self._refresh_once()
        return list(self._by_chat.get(chat_id) or [])

    async def binding_for_chat(self, chat_id: str) -> StoryBinding | None:
        rows = await self.bindings_for_chat(chat_id)
        return rows[0] if rows else None

    async def binding_for_story(self, story_id: str) -> StoryBinding | None:
        await self._refresh_once()
        return self._by_story.get(story_id)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def snapshot(self) -> dict[str, Any]:
        return {"bound_chats": len(self._by_chat), "stories": len(self._by_story),
                "mapped": len(self.story_chat_map), "errors": self._errors[:3],
                "source": "meegle"}

    # ── 解析 ─────────────────────────────────────────────────────
    async def _refresh_once(self) -> None:
        if self._by_chat or not self._should_scan():
            return
        await self.scan()

    def _should_scan(self) -> bool:
        return bool(self.story_chat_map and self.client is not None)

    async def scan(self) -> list[StoryBinding]:
        """拉 Story 列表 → 解析 PRD 链接 → 出绑定。失败逐条记 error，不整体抛。"""
        if self.client is None:
            self._errors.append("Meegle 客户端未装配（凭证或空间名未配）")
            return []
        try:
            stories = self.client.list_stories(limit=self.limit,
                                               status=self.include_status[0] if self.include_status else "",
                                               prd_field=self.prd_field,
                                               task_field=self.task_field)
        except MeegleError as e:
            self._errors.append(f"Story 列表取失败: {str(e)[:150]}")
            return []

        wanted = {str(k): str(v) for k, v in self.story_chat_map.items()}
        by_id = {s.story_id: s for s in stories}
        # 配了映射却没进「最近更新 limit 条」列表的 Story（一场会牵涉的多个 Story 里
        # 常有排期更早的那几个）→ 按 ID 直取补齐，而不是报"没找到"就放弃。
        # 只对**配了映射**的补拉（个位数），不给全空间 1300 条都拉。
        missing = [sid for sid in wanted if sid not in by_id]
        for story_id in missing:
            try:
                by_id[story_id] = self.client.get_story(
                    story_id, prd_field=self.prd_field, task_field=self.task_field)
            except MeegleError as e:
                self._errors.append(f"Story {story_id} 直取失败: {str(e)[:100]}")

        found: set[str] = set()
        for story_id, chat_id in wanted.items():
            story = by_id.get(story_id)
            if story is None or not chat_id:
                continue          # 没配群映射的 Story 不监听（不是遗漏，是授权边界）
            found.add(story_id)
            # 列表路径只回字段值（无参与人/空间名/链接）→ 走详情补齐；
            # 直取路径本来就是详情，_with_people 内部会跳过不重复拉
            story = await self._with_people(story)
            binding = await self._to_binding(story, chat_id)
            if binding is None:
                continue
            bucket = self._by_chat.setdefault(binding.chat_id, [])
            if all(binding.story_id != b.story_id for b in bucket):
                bucket.append(binding)     # 一群多 Story：全部留，不覆盖
            self._by_story[binding.story_id] = binding

        for story_id in set(wanted) - found:
            # 配了群但列表与直取都拿不到：可能已归档或 ID 写错，必须说出来
            self._errors.append(f"Story {story_id} 取不到（列表 {self.limit} 条内无、直取失败）")
        return [b for bucket in self._by_chat.values() for b in bucket]

    async def _with_people(self, story: Story) -> Story:
        """补齐参与人。

        MQL 列表查询只回字段值，`role_members`（产品/测试/需求提出人）与当前节点
        只有 get_workitem_brief 给——而卡片要 @ 的正是这些人。只对**已配群映射**的
        Story 逐条补拉（映射表由管理员维护，量是个位数），不给全空间 1300 条都拉。
        """
        if self.client is None or story.owners:
            return story
        try:
            full = self.client.get_story(story.story_id, prd_field=self.prd_field,
                                         task_field=self.task_field)
        except MeegleError as e:
            self._errors.append(f"Story {story.story_id} 参与人取失败: {str(e)[:100]}")
            return story
        story.owners = full.owners
        story.owner_emails = full.owner_emails
        story.node = full.node
        story.prd_url = story.prd_url or full.prd_url
        # 卡片第一行「空间 · Story · #ID」与可点标题靠这三项，而 MQL 列表不返回它们
        # （实测列表行只有字段值）→ 必须一并从详情补，否则卡片永远退化成只有 #ID。
        story.space_name = story.space_name or full.space_name
        story.story_type = story.story_type or full.story_type
        story.url = story.url or full.url
        return story

    async def _to_binding(self, story: Story, chat_id: str) -> StoryBinding | None:
        prd_token = await self._resolve_prd(story)
        if not prd_token:
            return None
        return StoryBinding(
            story_id=story.story_id, story_title=story.title,
            story_url=story.url, prd_token=prd_token, chat_id=chat_id,
            owners=_owners(story), owner_emails=_emails(story),
            status=story.status, source="meegle",
            space_name=story.space_name, story_type=story.story_type,
            prd_url=_clean_url(story.prd_url or ""))

    async def resolve_prd(self, story: Story) -> str:
        """公开入口：Story 的 PRD 字段 → obj_token（`bind_story` 手工登记要用）。"""
        return await self._resolve_prd(story)

    async def _resolve_prd(self, story: Story) -> str:
        """PRD 链接 → docx obj_token。

        Story 上挂的是 wiki 链接，而读正文/写评论要 obj_token（node_token 直接去
        fetch 会报 invalid token length，实测）。链接字段有时被产品填成正文摘要或
        内部 git 地址（实测 30 条里 13 条非链接、2 条 git tree），那就不是一篇可评论
        的文档 → 跳过并记因由。

        身份：**读必须用 user**（bot 身份实测对同一 wiki 节点同样 `lacks permission`，
        而 user 的 scope 里已有 `wiki:node:read` + `docx:document:readonly`；5/5 已授权
        文档可读到块）。写评论默认 bot（user 缺 comment scope）——读写身份不同是有意
        的，别"顺手统一"成同一个，那样要么读不到正文要么写不进评论。
        """
        raw = (story.prd_url or "").strip()
        url = _clean_url(raw)
        if url != raw:
            logger.info("easyprd Story %s PRD 字段带尾注，已取首段: %s",
                        story.story_id, raw[len(url):][:40])
        kind = doc_url_kind(url)
        if not kind:
            self._errors.append(
                f"Story {story.story_id} 的「{self.prd_field}」不是飞书文档链接"
                f"（值形如 {raw[:40] or '空'}）")
            return ""
        try:
            info = await self.lark.inspect_url(url)
        except Exception as e:                      # 三方错误一律降级，不让一条链接打断整批
            self._errors.append(f"Story {story.story_id} PRD 解析失败: {_brief_err(e)}")
            logger.info("easyprd PRD 解析失败 story=%s url=%s: %s",
                        story.story_id, url[:60], _brief_err(e))
            return ""
        token = info.get("obj_token") or info.get("token") or ""
        if not token:
            self._errors.append(f"Story {story.story_id} PRD 链接解析不出 token")
            return ""
        return token


def _pick(story: Story, source: dict[str, str]) -> dict[str, str]:
    """只留需要被通知的角色，其余（渠道/前端/核心交易等）不进 @ 名单。"""
    return {role: value for role, value in source.items()
            if value and any(k in role for k in _NOTIFY_ROLES)}


def _owners(story: Story) -> dict[str, str]:
    """角色 → 姓名。@ 用的 open_id 由 watcher 在发卡片时按群成员名单现算——
    Meegle 只给 email 域账号，而它能不能换成 open_id 取决于应用通讯录权限，
    拿不到时退回「@姓名纯文本」，不能因为 @ 不上就整条建议不发。"""
    return _pick(story, story.owners)


def _emails(story: Story) -> dict[str, str]:
    return _pick(story, story.owner_emails)
