"""lark-cli 封装 — 用 subprocess 调用全局 lark-cli，--as user 读写飞书文档。

为什么不用 bot(tenant_access_token) 直调飞书 API：
- bot 应用身份只有 im 权限，wiki/docx/drive 全部 403（见 memory/feishu-lark-cli-access.md）。
- 分享链接授权的是【人】不是【应用】，所以分享链接对 bot 身份无效。
- lark-cli --as user 走用户自己的 user_access_token，能读写用户有权限的文档，
  绕开了 bot 应用身份的权限限制。

依赖：机器上已安装 lark-cli 并 auth login 过（详见 lark-shared skill）。本模块不负责授权，
只在 lark-cli 缺 scope / 未登录时给出可读错误信息。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any

logger = logging.getLogger("agents.handlers.lark_cli")

# lark-cli 可执行文件。npm 全局包在 Windows 下是可执行文件是 .cmd 包装器，
# Python subprocess 无法直接执行无扩展名的 shell 脚本，必须显式用 .cmd。
# 注意：传给子进程的 env 必须是【完整继承】os.environ 再覆盖，否则会丢失 HOME/USERPROFILE
# 等配置定位变量，导致 lark-cli 报 "not configured"（找不到 ~/.lark-cli/config.json）。
def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    # Windows 下 .cmd 包装器依赖 node；确保 node 所在目录在 Path 中
    node_bin = _resolve_node_bin()
    if node_bin not in env.get("Path", "").split(os.pathsep):
        env["Path"] = node_bin + os.pathsep + env.get("Path", "")
    return env

# 单次 lark-cli 调用超时（读长文档可能较慢）
_DEFAULT_TIMEOUT_SEC = 60


def _resolve_lark_cli() -> str:
    """定位 lark-cli 入口：**优先 node + run.js 直调，回退 .cmd**。

    为什么不用 `.cmd`：`create_subprocess_exec` 起 `.cmd` 会经 `cmd.exe`，
    而 cmd 把 `&` `|` `<` `>` 当命令分隔符——`minutes +search --query "钱包管理&钱包绑定"`
    会被切成两条命令，报「'钱包管理' 不是内部或外部命令」，看起来像搜索无结果。
    直调 `node run.js` 绕开 shell，参数原样传递。
    """
    node = shutil.which("node") or shutil.which("node.exe")
    if node:
        for cli in ("lark-cli.cmd", "lark-cli"):
            found = shutil.which(cli)
            if not found:
                continue
            root = os.path.dirname(found)
            runjs = os.path.join(root, "node_modules", "@larksuite", "cli",
                                 "scripts", "run.js")
            if os.path.isfile(runjs):
                _NODE_RUNJS[0] = runjs
                return node
    # 回退：老路径（参数里有 shell 元字符时会被 cmd 拆开，见上）
    for candidate in ("lark-cli.cmd", "lark-cli"):
        found = shutil.which(candidate)
        if found:
            _NODE_RUNJS[0] = ""
            return found
    raise FileNotFoundError("未找到 lark-cli，请确认已全局安装（见 lark-shared skill）")


# 直调模式下的 run.js 路径（空 = 走 .cmd 回退）
_NODE_RUNJS: list[str] = [""]


def _resolve_node_bin() -> str:
    """定位 node 可执行文件所在目录，供 env 注入。"""
    node = shutil.which("node")
    if not node:
        raise LarkCliError("未找到 node，请确认 node 已在 PATH")
    return os.path.dirname(node)


class LarkCliError(Exception):
    """lark-cli 调用失败（未登录 / 缺 scope / 命令失败等）。"""

    def __init__(self, message: str, *, missing_scopes: list[str] | None = None):
        super().__init__(message)
        self.missing_scopes = missing_scopes or []


async def _run(
    args: list[str],
    *,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    cwd: str | None = None,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    """执行 lark-cli 命令，返回解析后的 JSON dict。

    Args:
        stdin_text: 写入子进程 stdin 的文本（配合 `--content -` 传长 Markdown，
            绕开 `@file` 只认相对路径的限制）。

    Raises:
        LarkCliError: 命令不存在 / 非零退出 / 返回 ok=false（未登录、缺 scope、资源无权限）。
    """
    if shutil.which("lark-cli") is None:
        raise LarkCliError("未找到 lark-cli，请确认已全局安装（见 lark-shared skill）")

    env = _build_env()
    entry = _resolve_lark_cli()
    cmd = [entry, _NODE_RUNJS[0], *args] if _NODE_RUNJS[0] else [entry, *args]
    logger.info("lark-cli: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes),
                                                timeout=timeout_sec)
    except TimeoutError:
        raise LarkCliError(f"lark-cli 调用超时(>{timeout_sec}s): {' '.join(cmd)}") from None
    except OSError as e:
        raise LarkCliError(f"lark-cli 启动失败: {e}") from e

    if proc.returncode != 0:
        tail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        # lark-cli 的业务错误（缺 scope / 无权限 / 未登录）也走非零退出码，
        # 且 payload 里带 missing_scopes —— 不解析就会把「缺哪个 scope」这种
        # 可行动信息埋在长 JSON 里，用户/上层拿不到（easyprd 写评论实测踩过）。
        parsed = _error_from_output(tail)
        if parsed is not None:
            message, missing = parsed
            raise LarkCliError(f"lark-cli 退出码 {proc.returncode}: {message}",
                               missing_scopes=missing)
        raise LarkCliError(f"lark-cli 退出码 {proc.returncode}: {tail[-500:]}")


    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        raise LarkCliError(f"lark-cli 无输出: {' '.join(cmd)}")

    # 输出可能带日志行（见 case-ingest SKILL 工具边界）：截到第一个 '{' 再解析
    start = raw.find("{")
    if start < 0:
        raise LarkCliError(f"lark-cli 输出非 JSON: {raw[:500]}")
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError:
        raise LarkCliError(f"lark-cli 输出非 JSON: {raw[:500]}") from None

    # ok=false → 未登录 / 缺 scope / 资源无权限
    if payload.get("ok") is False or payload.get("code", 0) != 0:
        err = payload.get("error") or {}
        message = err.get("message", raw[:200])
        missing = err.get("missing_scopes") or []
        raise LarkCliError(message, missing_scopes=missing)

    return payload



def _error_from_output(tail: str) -> tuple[str, list[str]] | None:
    """从非零退出的输出里扒结构化错误（拿不到就返回 None，交回原文兜底）。"""
    start = tail.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(tail[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    message = str(err.get("message") or "")[:300]
    if not message:
        return None
    missing = [str(s) for s in (err.get("missing_scopes") or [])]
    return message, missing


class LarkCli:
    """飞书文档访问封装（--as user）：正文读取 + 案例库镜像写入。"""

    def __init__(self):
        pass

    async def get_doc_content(
        self,
        token: str,
        *,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        """读取文档正文（markdown）。

        Args:
            token: 文档 token（wiki 的 obj_token 或 docx token）。
            max_chars: 正文截取长度上限。

        Returns:
            {"token", "revision_id", "content"}；content 已按 max_chars 截断。
        """
        args = [
            "docs", "+fetch",
            "--doc", token,
            "--as", "user",
            "--doc-format", "markdown",
            "--format", "json",
        ]
        payload = await _run(args)
        data = payload.get("data") or {}
        doc = data.get("document") or {}
        content = doc.get("content", "")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n...(内容较长，已截取前{max_chars}字)"
        return {
            "token": doc.get("document_id", token),
            "revision_id": doc.get("revision_id", ""),
            "content": content,
        }

    # ── 写操作（案例库镜像同步专用）──────────────────────────────────
    # 写目标必须由调用方从配置取（案例库目录节点 + 索引文档），不得接受用户
    # 任意指定的 token——这是 lark-cli user 身份写权限的收口点。

    async def create_wiki_node(self, space_id: str, parent_node_token: str,
                               title: str) -> dict[str, str]:
        """在指定 wiki 父节点下建子文档（docx）。

        Returns:
            {"node_token", "obj_token", "title"}；obj_token 用于 docs +update 写正文。
        """
        payload = await _run([
            "wiki", "+node-create",
            "--space-id", space_id,
            "--parent-node-token", parent_node_token,
            "--obj-type", "docx",
            "--title", title,
            "--as", "user",
            "--format", "json",
        ])
        data = payload.get("data") or {}
        node = data.get("node") or {}
        return {
            "node_token": str(node.get("node_token") or data.get("node_token") or ""),
            "obj_token": str(node.get("obj_token") or data.get("obj_token") or ""),
            "title": str(node.get("title") or data.get("title") or title),
        }

    async def update_doc_markdown(self, doc: str, markdown: str) -> dict[str, Any]:
        """整篇覆盖内容为 markdown（案例库子文档/索引文档写入）。

        正文走 stdin（`--content -`）：`@file` 只认相对路径，长 Markdown 用
        stdin 最稳。
        """
        return await _run([
            "docs", "+update",
            "--doc", doc,
            "--command", "overwrite",
            "--doc-format", "markdown",
            "--content", "-",
            "--as", "user",
            "--format", "json",
        ], stdin_text=markdown)

    # ── PRD 变更监听（easyprd）专用：块定位 + 评论读写 ─────────────
    # 与上面写操作同规矩：目标文档 token 由调用方从台账/配置取，不接受消息内容里
    # 的链接决定写到哪里。identity 可切（见方法注释），默认 user。

    async def inspect_url(self, url: str, *, identity: str = "user") -> dict[str, str]:
        """解析飞书文档链接 → 可操作坐标。

        Story 上挂的是 wiki 链接（`/wiki/{node_token}`），而读正文与写评论要的是
        **docx obj_token**——两者不是一个东西，直接拿 node_token 去 fetch 会报
        `invalid token length`（实测）。返回 {"token","type","title","obj_token"}。
        """
        payload = await _run(["drive", "+inspect", "--url", url,
                              "--as", identity if identity in ("user", "bot") else "user",
                              "--format", "json"])
        data = payload.get("data") or {}
        node = data.get("wiki_node") or {}
        return {
            "token": str(data.get("token") or ""),
            "type": str(data.get("type") or ""),
            "title": str(data.get("title") or ""),
            "obj_token": str(node.get("obj_token") or data.get("token") or ""),
            "node_token": str(node.get("node_token") or ""),
        }

    async def fetch_block_ids(self, doc: str, *, keyword: str = "") -> list[dict[str, str]]:
        """读文档，返回 [{block_id, text}]（带块引用，锚点定位用）。

        与 get_doc_content 的区别：那边是 markdown（给人读的正文），这边是
        DocxXML + with-ids（给代码找的块）。现有 markdown 路径拿不到块 ID，
        写评论锚不到段落，所以必须单独一条读取。

        Args:
            doc: 文档 token 或 URL。
            keyword: 传了就只取命中块的片段（服务端筛，省一次全文下载）。
        """
        blocks = await self.fetch_blocks(doc)
        if keyword:
            needle = "".join(keyword.split())
            hits = [b for b in blocks if needle in "".join(b.get("text", "").split())]
            if hits:                       # 命中为空时退回全文，交给本地模糊匹配
                return hits
        return blocks

    async def fetch_blocks(self, doc: str) -> list[dict[str, str]]:
        """全文块列表，每块带元素类型：`{block_id, tag, text}`。

        章节归属与「点原文跳转」都要块类型（h1/h2 vs p）与块 ID；单看
        `fetch_block_ids` 的扁平文本算不出这条原文属于哪一节。
        """
        payload = await _run(["docs", "+fetch", "--doc", doc, "--as", "user",
                              "--doc-format", "xml", "--detail", "with-ids",
                              "--format", "json"])
        content = str(((payload.get("data") or {}).get("document") or {}).get("content") or "")
        return _extract_tagged_blocks(content)

    async def add_comment(
        self, doc: str, text: str, *, block_id: str = "", identity: str = "user",
        doc_type: str = "docx", link_url: str = "",
    ) -> dict[str, Any]:
        """写文档评论。block_id 为空 → 整篇评论（降级形态）。

        `link_url` 给了就把链接作为独立的 `link` 元素接在正文后：正文是纯文本，
        裸 URL 在客户端不会自动变成可点链接。

        两种身份都能用，但失败点不同，处理方式也不同：
        - `user`：要 `docs:document.comment:create` + `:write_only` 两个 scope，
          缺一个就 missing_scopes；评论署名是人。
        - `bot`：scope 由应用统一开通，但应用必须已是该文档的协作者（文档 owner
          在「共享」里加应用），否则 `1069303 Access denied`。评论署名是应用。

        两者都不静默改身份：失败抛 LarkCliError 带 missing_scopes，由上层回执用户。
        """
        as_ = identity if identity in ("user", "bot") else "user"
        args = ["drive", "+add-comment", "--doc", doc, "--type", doc_type,
                "--as", as_, "--content", "-", "--format", "json"]
        if block_id:
            args += ["--block-id", block_id]
        else:
            args += ["--full-comment"]
        return await _run(args, stdin_text=_elements_payload(text, link_url))

    async def list_comment_replies(
        self, doc: str, comment_id: str, *, identity: str = "user",
        doc_type: str = "docx",
    ) -> list[dict[str, str]]:
        """列出某条评论的回复（决策闭环靠它轮询「采纳/忽略/调整」）。"""
        payload = await _run([
            "drive", "+list-replies", "--token", doc, "--type", doc_type,
            "--comment-id", comment_id,
            "--as", identity if identity in ("user", "bot") else "user",
            "--format", "json",
        ])
        data = payload.get("data") or {}
        items = data.get("items") or data.get("replies") or []
        out: list[dict[str, Any]] = []
        for i, it in enumerate(items if isinstance(items, list) else []):
            out.append({
                "reply_id": str(it.get("reply_id") or it.get("id") or ""),
                "text": _reply_text(it.get("content")),
                "user_id": str(it.get("user_id")
                               or _as_dict(it.get("receiver")).get("open_id") or ""),
                "created_at": str(it.get("create_time") or it.get("created_time") or ""),
                # items[0] 是评论本体（发起的那条），后面才是回复。决策闭环必须跳过
                # 本体：它由我们自己写，正文里带着「采纳 → 回复『采纳』」的说明，
                # 当成回复读就会在首轮把所有 pending 全部判成"已采纳"。
                "is_body": i == 0,
                # 评论本体的 id：`+resolve-comment` 吃的是它，不是回复的 reply_id
                # （实测拿 reply_id 去 resolve 报 "fail"）。
                "id": str(((payload.get("data") or {}).get("comment_id")) or ""),
            })
        return out

    async def list_comments(
        self, doc: str, *, identity: str = "user", doc_type: str = "docx",
        solved_status: str = "all", page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """列出文档上的**全部**评论（含已解决），用于"这处是否已经有评论了"。

        `solved_status` 默认 `all`：已解决的评论同样算"这处已经提过"——忽略过的
        变更不该在下一轮讨论里被重新提一遍，那对产品是重复噪音。

        返回每条评论的 `block_id`（`content_anchor_id`，整篇评论为空）、`quote`
        （所锚原文）、`body`（评论正文拼接文本，标题从里面解析）。
        """
        payload = await _run([
            "drive", "+list-comments", "--token", doc, "--type", doc_type,
            "--solved-status", solved_status, "--page-size", str(min(page_size, 100)),
            "--as", identity if identity in ("user", "bot") else "user",
            "--format", "json",
        ])
        data = payload.get("data") or {}
        items = data.get("items") or []
        out: list[dict[str, Any]] = []
        for it in items if isinstance(items, list) else []:
            replies = ((it.get("reply_list") or {}).get("replies") or [])
            body = _reply_text((replies[0] or {}).get("content")) if replies else ""
            out.append({
                "comment_id": str(it.get("comment_id") or ""),
                "block_id": str((it.get("extra") or {}).get("content_anchor_id") or ""),
                "quote": str(it.get("quote") or ""),
                "body": body,
                "is_solved": bool(it.get("is_solved")),
                "is_whole": bool(it.get("is_whole")),
                "reply_count": len(replies),
            })
        return out

    async def resolve_comment(
        self, doc: str, comment_id: str, *, identity: str = "user",
        doc_type: str = "docx",
    ) -> dict[str, Any]:
        """把评论标记为已解决（产品「忽略」后的收尾动作）。

        只吃**评论 id**（`+list-replies` 返回体里的 `comment_id`）；传某条回复的
        reply_id 会返回 `fail`（实测）。调用方拿不到评论 id 时就别调，
        个别标记失败不该把整条决策回执拖垮。
        """
        return await _run([
            "drive", "+resolve-comment", "--token", doc, "--type", doc_type,
            "--comment-id", comment_id,
            "--as", identity if identity in ("user", "bot") else "user",
            "--format", "json",
        ])

    async def reply_comment(
        self, doc: str, comment_id: str, text: str, *, identity: str = "user",
        doc_type: str = "docx", link_url: str = "",
    ) -> dict[str, Any]:
        """在既有评论下追加一条回复（决策回写状态用：✅已采纳 / ❌已忽略 / 🔄已调整）。

        用回复而不是改评论正文：飞书没有「编辑评论」的开放能力，回复是唯一可追溯
        且不会覆盖产品原话的写法。
        """
        return await _run([
            "drive", "+add-reply", "--token", doc, "--type", doc_type,
            "--comment-id", comment_id, "--content", "-",
            "--as", identity if identity in ("user", "bot") else "user",
            "--format", "json",
        ], stdin_text=_elements_payload(text, link_url))



# ── 模块内小工具（评论/块解析，纯函数便于单测）─────────────────────

_BLOCK_RE = re.compile(r"<(?P<tag>[a-zA-Z]+)\s+id=\"(?P<id>[^\"]+)\">"
                        r"(?P<text>.*?)</(?P=tag)>", re.S)
# 带 tag 的版本：允许 id 前面还有别的属性（飞书不同块类型的属性顺序不固定）
_TAGGED_BLOCK_RE = re.compile(r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*?\bid=\"(?P<id>[^\"]+)\""
                              r"[^>]*>(?P<text>.*?)</(?P=tag)>", re.S)
# 表格单元格等嵌套场景里同样的 id 形态，统一按「带 id 的元素」取


def _extract_blocks(xml: str) -> list[dict[str, str]]:
    """DocxXML → [{block_id, text}]，text 已剥掉内层标签。

    只取顶层带 id 的元素；同一 id 重复出现取首次（飞书块 id 唯一，重复说明
    嵌套命中，外层更有用）。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _BLOCK_RE.finditer(xml or ""):
        bid = m.group("id")
        if bid in seen:
            continue
        text = re.sub(r"<[^>]+>", "", m.group("text"))
        text = " ".join(text.split())
        if not text:
            continue
        seen.add(bid)
        out.append({"block_id": bid, "text": text})
    return out


def _extract_tagged_blocks(xml: str) -> list[dict[str, str]]:
    """DocxXML → [{block_id, tag, text}]，保留元素类型（h1/h2/p/li…）。

    与 `_extract_blocks` 的差别只有多一个 `tag`：章节归属要知道哪几个块是标题，
    否则算不出"这条原文属于哪一节"。正则同时捕获前后标签名并要求配对，
    避免把 `<p>…<b>…</b></p>` 的内层标签当成块。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _TAGGED_BLOCK_RE.finditer(xml or ""):
        bid = m.group("id")
        if bid in seen:
            continue
        text = " ".join(re.sub(r"<[^>]+>", "", m.group("text")).split())
        if not text:
            continue
        seen.add(bid)
        raw = m.group("text")
        out.append({
            "block_id": bid, "tag": m.group("tag").lower(), "text": text,
            # 保留原始 XML 片段：删除线是 `<del>` 标签，剥标签后就丢了，
            # 而「这段已经被划掉」正是分析层要判的东西。
            "raw": raw,
        })
    return out


def _comment_elements(text: str) -> str:
    """评论正文 → lark-cli 的 reply_elements JSON（多行合成一个 text 元素）。

    飞书评论 elements 支持 text/mention_user/link 三种类型。本能力的正文以
    Markdown 风格写在 text 里——**评论不渲染 markdown**，`**加粗**` 与 `[文字](url)`
    会原样显示成星号和括号（实测），所以正文里只留纯文本与「▎」分节符；
    需要可点的链接时另发一个 `link` 元素（见 `comment_link_element`），
    或者干脆把 URL 单独写一行。@人走消息卡片而不是评论。
    """
    return json.dumps([{"type": "text", "text": text or ""}], ensure_ascii=False)


def comment_link_element(url: str) -> dict[str, str] | None:
    """把链接包成可见的 `link` 元素；空或非 http(s) 返回 None。

    为什么要单独一个元素：评论正文是纯文本，写进去的 URL 在飞书客户端不会自动变成
    可点链接（`+add-comment` 不校验外部 URL，但也不会把裸 URL 渲染成链接）。
    """
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return None
    return {"type": "link", "link": u}


def _elements_payload(text: str, link_url: str = "") -> str:
    """正文 + 可选链接 → `--content` 的 JSON。

    换行要并进正文那一元素里：单独发一个纯 `\\n` 的 text 元素会被 CLI 拒
    （`element #N type=text requires non-empty text`，实测）。
    """
    body = text or ""
    link = comment_link_element(link_url)
    if link is None:
        return json.dumps([{"type": "text", "text": body}], ensure_ascii=False)
    return json.dumps([
        {"type": "text", "text": body.rstrip() + "\n"},
        link,
    ], ensure_ascii=False)


def _reply_text(content: Any) -> str:
    """评论/回复的 content → 纯文本。

    飞书的形态是 `{"elements": [{"type": "text_run", "text_run": {"text": "…"}}]}`，
    没有顶层 `text` 字段——只取 `content["text"]` 会让每条回复都成空串，
    决策闭环于是永远读不到「采纳」二字，且日志里看不出异常。
    """
    c = _as_dict(content)
    if isinstance(c.get("text"), str):
        return str(c["text"]).strip()
    parts: list[str] = []
    for el in c.get("elements") or []:
        e = _as_dict(el)
        run = _as_dict(e.get("text_run"))
        text = run.get("text") or (e.get("text") if isinstance(e.get("text"), str) else "")
        if text:
            parts.append(str(text))
        person = _as_dict(e.get("person"))
        if person.get("user_id"):
            parts.append(f"@{person['user_id']}")
    return "".join(parts).strip()


def _as_dict(value: Any) -> dict[str, Any]:
    """SDK/dict 混合返回统一转 dict（lark-cli 有时给 dict 有时给对象）。"""
    if isinstance(value, dict):
        return value
    return dict(getattr(value, "__dict__", {}) or {})
