"""webportal_handler：案例沉淀入参门禁 + 飞书素材取正文 + 镜像同步回报（全部打桩）。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.handlers.webportal_handler import WebportalHandler
from app.config import CaseMirrorConfig, load_config


class FakeLark:
    """只读桩：get_doc_content 返回固定正文。"""

    def __init__(self, content="# 操作手册标题\n\n正文内容"):
        self.content = content
        self.written: list[tuple[str, str]] = []

    async def get_doc_content(self, token, *, max_chars=4000):
        return {"token": token, "revision_id": "1", "content": self.content}

    async def create_wiki_node(self, space_id, parent_node_token, title):
        return {"node_token": "node1", "obj_token": "obj1", "title": title}

    async def update_doc_markdown(self, doc, markdown):
        self.written.append((doc, markdown))
        return {"ok": True}


@pytest.fixture()
def handler():
    h = WebportalHandler(load_config().router, case_mirror=CaseMirrorConfig(
        enabled=True, space_id="s", parent_node_token="p", index_doc_token="i"))
    return h


def test_ingest_requires_exactly_one_source(handler):
    r = asyncio.run(handler._run_ingest({}))
    assert "二选一" in r.error
    r = asyncio.run(handler._run_ingest({"order_id": "5107", "feishu_url": "https://x"}))
    assert "二选一" in r.error


def test_ingest_order_id_must_be_digits(handler):
    r = asyncio.run(handler._run_ingest({"order_id": "DATA2026"}))
    assert "纯数字" in r.error


def test_ingest_feishu_fetches_body_and_sends_prefixed_message(handler, monkeypatch):
    """飞书素材：本侧取正文 → 「沉淀案例 飞书 <链接>\\n<正文>」发给引擎。"""
    from app.protocols import lark_cli

    sent: list[list] = []

    async def fake_a2a_send(parts):
        sent.append(parts)
        return {"ingested": True, "case_id": "demo", "draft_path": "cases/demo.md",
                "status": "draft", "title": "演示", "source_type": "feishu",
                "mirror_markdown": "# 演示"}

    monkeypatch.setattr(lark_cli, "LarkCli", FakeLark)
    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    monkeypatch.setattr(handler, "_sync_case_mirror", _fake_mirror_line)

    r = asyncio.run(handler._run_ingest({"feishu_url": "https://x.feishu.cn/wiki/abc"}))
    assert r.success, r.error
    text = sent[0][0]["text"]
    assert text.startswith("沉淀案例 飞书 https://x.feishu.cn/wiki/abc\n")
    assert "正文内容" in text
    assert "案例草稿已沉淀" in r.data["reply"]


def test_ingest_cmdb_sends_plain_prefix(handler, monkeypatch):
    sent: list[list] = []

    async def fake_a2a_send(parts):
        sent.append(parts)
        return {"ingested": True, "case_id": "demo", "draft_path": "cases/demo.md",
                "status": "draft", "title": "演示", "source_type": "cmdb",
                "mirror_markdown": "# 演示"}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    monkeypatch.setattr(handler, "_sync_case_mirror", _fake_mirror_line)

    r = asyncio.run(handler._run_ingest({"order_id": "5107"}))
    assert r.success, r.error
    assert sent[0] == [{"text": "沉淀案例 5107"}]
    assert "CMDB 工单 5107" in r.data["reply"]


def test_sync_case_reports_missing_case(handler, monkeypatch):
    async def fake_a2a_send(parts):
        return {"found": False, "error": "案例不存在：nope"}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    r = asyncio.run(handler._run_sync_case("nope"))
    assert "案例不存在" in r.error


async def _fake_mirror_line(payload):
    return "🪞 飞书案例库镜像已同步：（桩）"


class FakeMirrorSync:
    """镜像同步桩：固定 node_url，不去碰飞书。"""

    node_url = "https://x.feishu.cn/wiki/node1"

    def __init__(self, lark=None, cfg=None):
        pass

    def config_error(self):
        return ""

    async def sync(self, **kw):
        from app.domains.webportal.feishu_case_mirror import MirrorResult

        return MirrorResult(ok=True, node_url=self.node_url, created=True,
                            index_updated=True, verified=True)


def test_sync_case_mirror_writes_back_mapping(handler, monkeypatch):
    """镜像同步成功后必须把 node_url 写回本地案例的 mirror_ref——两边的唯一映射。"""
    import app.domains.webportal.feishu_case_mirror as mirror_mod

    sent: list[list[dict]] = []

    async def fake_a2a_send(parts):
        sent.append(parts)
        return {"updated": True, "mirror_ref": FakeMirrorSync.node_url}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    monkeypatch.setattr(mirror_mod, "CaseMirrorSync", FakeMirrorSync)
    line = asyncio.run(handler._sync_case_mirror({
        "case_id": "demo", "title": "演示", "status": "draft",
        "source_type": "cmdb", "mirror_markdown": "# 演示",
    }))
    assert "飞书案例库镜像已同步" in line
    assert "映射已写回" in line
    assert sent == [[{"text": f"案例映射 demo {FakeMirrorSync.node_url}"}]]


def test_write_mirror_ref_reports_failure_without_raising(handler, monkeypatch):
    """映射写回失败只如实报告，不拖垮镜像同步本身。"""

    async def fake_a2a_send(parts):
        return {"updated": False, "error": "案例不存在：nope"}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    line = asyncio.run(handler._write_mirror_ref("nope", "https://x.feishu.cn/wiki/n"))
    assert "映射未写回" in line
    assert "案例不存在" in line


def test_confirm_case_finalizes_then_resyncs(handler, monkeypatch):
    """人工确认 → 「案例状态 verified」→ 重推镜像；定稿是确认后的独立动作。"""
    sent: list[str] = []

    async def fake_a2a_send(parts):
        sent.append(parts[0]["text"])
        return {"updated": True, "case_id": "demo", "status": "verified",
                "title": "演示", "source_type": "cmdb", "mirror_markdown": "# 演示",
                "case_ref": {"url": FakeMirrorSync.node_url}}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    monkeypatch.setattr(handler, "_sync_case_mirror", _fake_mirror_line)
    r = asyncio.run(handler._run_confirm_case("demo"))
    assert not r.error
    assert "已定稿" in r.data["reply"]
    assert FakeMirrorSync.node_url in r.data["reply"]
    assert sent == ["案例状态 demo verified"]


def test_confirm_case_blocked_without_mirror_ref(handler, monkeypatch):
    """引擎拒定稿（缺 mirror_ref）时如实转述，不硬改成 verified。"""

    async def fake_a2a_send(parts):
        return {"updated": False, "error": "案例「demo」缺 mirror_ref，不能改 verified"}

    monkeypatch.setattr(handler, "_a2a_send", fake_a2a_send)
    r = asyncio.run(handler._run_confirm_case("demo"))
    assert "缺 mirror_ref" in r.error


def test_sql_payload_renders_case_entry_not_source():
    """「案例文档」必须是案例知识库条目（case_ref），不是源头依据。

    此前误把溯源 sources 的飞书手册渲染成「案例文档」，审核侧看到的是
    《…问题汇总》而不是知识库里那一条案例。
    """
    reply = WebportalHandler._format_sql_payload({
        "advisory": False,
        "case_id": "demo",
        "case_title": "演示案例",
        "case_ref": {"title": "演示案例",
                     "url": "https://x.feishu.cn/wiki/ENTRY",
                     "local": "cases/demo.md"},
        "sql": "UPDATE t SET a = 1 WHERE b = 2",
        "risk": "low",
    })
    assert "https://x.feishu.cn/wiki/ENTRY" in reply
    assert "案例文档" in reply
    assert "工单来源" not in reply


def test_sql_payload_flags_missing_mirror_ref():
    """命中案例但缺映射时明说要补同步，而不是静默不给链接。"""
    reply = WebportalHandler._format_sql_payload({
        "advisory": False, "case_id": "demo", "case_title": "演示案例",
        "case_ref": {}, "sql": "UPDATE t SET a = 1 WHERE b = 2", "risk": "low",
    })
    assert "缺 mirror_ref" in reply
    assert "同步案例 demo" in reply
