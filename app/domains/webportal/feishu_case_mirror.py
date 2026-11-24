"""工单案例库飞书镜像同步 — 案例落盘后建子文档 + 更新目录索引 + 回读校验。

写目标严格限定 `[projects.case_mirror]` 配置的 parent_node_token / index_doc_token
两个 token（lark-cli user 身份写权限的收口点，调用方不得传任意 token）。
子文档内容由上游引擎渲染（`order_agent/cases.py::case_to_mirror_markdown`，
随 A2A 产物 `mirror_markdown` 传过来），本模块只负责飞书写入与索引维护。

幂等口径：按案例标题匹配目录索引已有行——命中则覆盖该子文档并更新该行，
未命中才新建。标题变了会另起一篇，需人工合并。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import CaseMirrorConfig
from app.protocols.lark_cli import LarkCli, LarkCliError

logger = logging.getLogger("services.feishu_case_mirror")

# 目录索引表行：| [<短标题>](<wiki>/<node_token>) | <status> | <来源类型> |
_INDEX_ROW_RE = re.compile(
    r"^\|\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\s*\|"
    r"\s*(?P<status>[^|]+?)\s*\|\s*(?P<source>[^|]+?)\s*\|\s*$")
_STATS_RE = re.compile(r"共\s*(\d+)\s*个案例（verified\s*(\d+)\s*/\s*draft\s*(\d+)\s*）")

_SOURCE_LABELS = {"cmdb": "CMDB 工单", "feishu": "飞书操作手册"}


@dataclass
class MirrorResult:
    """一次镜像同步的结果（调用方拼进回复，不做二次解释）。"""

    ok: bool
    node_url: str = ""
    created: bool = False
    index_updated: bool = False
    verified: bool = False
    error: str = ""


def source_label(source_type: str) -> str:
    """来源类型 → 目录索引「来源」列文案（与既有 26 行同口径）。"""
    return _SOURCE_LABELS.get(source_type, source_type or "其他")


def apply_index_update(index_md: str, *, title: str, node_url: str,
                       status: str, source_label: str) -> str:
    """目录索引 markdown → 更新后的 markdown（插入/更新行 + 重算统计行）。"""
    lines = index_md.splitlines()
    row_idx = [i for i, ln in enumerate(lines) if _INDEX_ROW_RE.match(ln)]

    # 同链接已有行 → 就地更新（案例重同步时改状态，不新增重复行）
    for i in row_idx:
        m = _INDEX_ROW_RE.match(lines[i])
        if m and node_url in m.group("url"):
            lines[i] = f"| [{title}]({node_url}) | {status} | {source_label} |"
            return _refresh_stats(lines)

    new_row = f"| [{title}]({node_url}) | {status} | {source_label} |"
    insert_at = None
    for i in row_idx:  # 同来源分组末尾（既有行按 CMDB 工单组 / 飞书操作手册组分段）
        m = _INDEX_ROW_RE.match(lines[i])
        if m and m.group("source").strip() == source_label:
            insert_at = i + 1
    if insert_at is None:
        insert_at = (row_idx[-1] + 1) if row_idx else len(lines)
    lines.insert(insert_at, new_row)
    return _refresh_stats(lines)


def _refresh_stats(lines: list[str]) -> str:
    """重算结尾统计行：共 N 个案例（verified A / draft B）。"""
    rows = [m for m in (_INDEX_ROW_RE.match(ln) for ln in lines) if m]
    total = len(rows)
    verified = sum(1 for m in rows if m.group("status").strip() == "verified")
    stats = f"共 {total} 个案例（verified {verified} / draft {total - verified}）。"
    for i, ln in enumerate(lines):
        if _STATS_RE.search(ln):
            lines[i] = stats
            break
    else:
        lines += ["", stats]
    return "\n".join(lines)


class CaseMirrorSync:
    """案例 → 飞书案例库镜像：建/更子文档 + 目录索引 + 回读校验。"""

    def __init__(self, lark: LarkCli | None = None,
                 cfg: CaseMirrorConfig | None = None) -> None:
        self._lark = lark or LarkCli()
        self._cfg = cfg or CaseMirrorConfig()

    def config_error(self) -> str:
        """配置缺口（空串 = 就绪）；未就绪时调用方只提示不同步，不阻断本地落盘。"""
        if not self._cfg.enabled:
            return "案例镜像同步未启用（[projects.case_mirror].enabled=false）"
        missing = [name for name in ("space_id", "parent_node_token", "index_doc_token")
                   if not getattr(self._cfg, name)]
        return f"案例镜像同步未配置完整（缺 {', '.join(missing)}）" if missing else ""

    def node_url(self, node_token: str) -> str:
        return f"{self._cfg.wiki_base_url.rstrip('/')}/{node_token}"

    async def sync(self, *, title: str, status: str, source_type: str,
                   mirror_markdown: str) -> MirrorResult:
        """同步一个案例到飞书案例库。

        三步缺一不可（SKILL 步骤 8）：建/更子文档 → 更新目录索引 → 回读校验。
        任一步失败都返回 ok=False 并带上原因，由调用方如实转述给用户。
        """
        error = self.config_error()
        if error:
            return MirrorResult(ok=False, error=error)
        if not title or not mirror_markdown:
            return MirrorResult(ok=False, error="案例标题或镜像内容为空，跳过同步")
        try:
            return await self._sync(title=title, status=status,
                                    source_type=source_type,
                                    mirror_markdown=mirror_markdown)
        except LarkCliError as exc:
            logger.warning("case mirror sync failed: %s", exc)
            return MirrorResult(ok=False, error=f"飞书写入失败：{exc}")

    async def _sync(self, *, title: str, status: str, source_type: str,
                    mirror_markdown: str) -> MirrorResult:
        cfg = self._cfg
        label = source_label(source_type)

        index_md = (await self._lark.get_doc_content(
            cfg.index_doc_token, max_chars=200_000))["content"]

        node_token = _find_node_token(index_md, title)
        created = False
        if node_token:
            await self._lark.update_doc_markdown(self.node_url(node_token),
                                                mirror_markdown)
        else:
            node = await self._lark.create_wiki_node(
                cfg.space_id, cfg.parent_node_token, title)
            node_token = node["node_token"] or node["obj_token"]
            if not node_token:
                raise LarkCliError("wiki +node-create 未返回 node_token")
            await self._lark.update_doc_markdown(
                node["obj_token"] or self.node_url(node_token), mirror_markdown)
            created = True

        node_url = self.node_url(node_token)
        new_index = apply_index_update(index_md, title=title, node_url=node_url,
                                       status=status, source_label=label)
        await self._lark.update_doc_markdown(cfg.index_doc_token, new_index)

        verified = await self._verify(title=title, node_token=node_token,
                                      node_url=node_url, status=status, label=label)
        return MirrorResult(ok=True, node_url=node_url, created=created,
                            index_updated=True, verified=verified)

    async def _verify(self, *, title: str, node_token: str, node_url: str,
                      status: str, label: str) -> bool:
        """回读校验：目录索引含该行且统计行与表格行数一致（SKILL 停止条件之一）。"""
        index_md = (await self._lark.get_doc_content(
            self._cfg.index_doc_token, max_chars=200_000))["content"]
        if node_token not in index_md:
            return False
        rows = [m for m in (_INDEX_ROW_RE.match(ln)
                            for ln in index_md.splitlines()) if m]
        m = _STATS_RE.search(index_md)
        if not m or not rows:
            return False
        total = len(rows)
        verified = sum(1 for r in rows if r.group("status").strip() == "verified")
        return (int(m.group(1)) == total
                and int(m.group(2)) == verified
                and int(m.group(3)) == total - verified)


def _find_node_token(index_md: str, title: str) -> str:
    """按标题在目录索引里找已有 node_token（幂等重同步用）。"""
    for ln in index_md.splitlines():
        m = _INDEX_ROW_RE.match(ln)
        if m and m.group("title").strip() == title:
            token = m.group("url").rstrip("/").rsplit("/", 1)[-1]
            return token
    return ""
