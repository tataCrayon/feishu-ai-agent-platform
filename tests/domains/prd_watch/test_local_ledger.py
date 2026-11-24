"""本地台账（SQLite）契约测试 — 绑定 / CR 状态机 / 知识地图 三张表。

守的四条不变量（见 `local_ledger.py` docstring）：
1. Story + 群 是绑定的主键，重复 upsert 只更新不新增；
2. CR 编号唯一，改状态只动状态字段，不把正文抹空；
3. 「没有归属 Story 的会议」不落库（不制造假归属）；
4. 未知表名导出时拒绝，不拼 SQL。
"""

from __future__ import annotations

import asyncio

from app.domains.prd_watch.local_adapters import LocalCrAdapter, LocalKnowledgeAdapter
from app.domains.prd_watch.local_ledger import LocalBinding, LocalLedger, LocalStory


def _ledger(tmp_path) -> LocalLedger:
    return LocalLedger(tmp_path / "ledger.db")


def test_binding_upsert_is_idempotent_per_story_and_chat(tmp_path):
    led = _ledger(tmp_path)
    assert led.upsert_binding(LocalBinding(story_id="s1", chat_id="oc_a",
                                           story_title="旧名", prd_token="T1"))
    assert led.upsert_binding(LocalBinding(story_id="s1", chat_id="oc_a",
                                           story_title="新名", prd_token="T1"))
    rows = led.bindings_for_chat("oc_a")
    assert len(rows) == 1
    assert rows[0].story_title == "新名"
    # 同一个 Story 绑到另一个群是合法形态（一场会多个群）
    led.upsert_binding(LocalBinding(story_id="s1", chat_id="oc_b", prd_token="T1"))
    assert len(led.bindings_for_chat("oc_b")) == 1


def test_binding_without_prd_token_is_not_usable(tmp_path):
    led = _ledger(tmp_path)
    led.upsert_binding(LocalBinding(story_id="s1", chat_id="oc_a", prd_token=""))
    rows = led.bindings_for_chat("oc_a")
    assert rows and not rows[0].usable
    assert rows[0].as_binding().usable is False


def test_cr_status_change_keeps_body(tmp_path):
    led = _ledger(tmp_path)
    assert led.save_cr({"cr_id": "CR-1", "title": "改费率",
                        "suggested_text": "费率 0.5%", "status": "pending"})
    assert led.set_cr_status("CR-1", "已采纳", decided_by="张三")
    row = led.get_cr("CR-1")
    assert row["status"] == "已采纳" and row["decided_by"] == "张三"
    assert row["title"] == "改费率" and row["suggested_text"] == "费率 0.5%"


def test_cr_adapter_pending_and_save_partial(tmp_path):
    led = _ledger(tmp_path)
    led.save_cr({"cr_id": "CR-2", "title": "t", "status": "pending"})
    adapter = LocalCrAdapter(led)

    async def run():
        assert [r.cr_id for r in await adapter.pending()] == ["CR-2"]
        # 只带状态的部分更新不能把标题抹空
        from app.domains.prd_watch.cr_store import CrRow

        await adapter.save(CrRow(cr_id="CR-2", status="已忽略"))
        row = await adapter.get("CR-2")
        assert row.status == "已忽略" and row.title == "t"

    asyncio.run(run())


def test_knowledge_adapter_find_and_changes(tmp_path):
    led = _ledger(tmp_path)
    led.upsert_story(LocalStory(story_id="s1", story_name="虚拟币申赎",
                                prd_url="https://x/wiki/abc"))
    led.add_change(story_id="s1", title="改费率", content="c", before="a", after="b",
                   source="会议", result="待确认", date="2026-11-23",
                   proposer="李", trace="t", prd_url="u")
    adapter = LocalKnowledgeAdapter(led)

    async def run():
        assert (await adapter.find_story("s1")).story_name == "虚拟币申赎"
        assert (await adapter.find_story("abc")).story_id == "s1"      # PRD 链接命中
        assert await adapter.find_story("不存在的名字") is None
        story = await adapter.find_story("s1")
        changes = await adapter.changes_for(story)
        assert len(changes) == 1 and changes[0].title == "改费率"
        assert changes[0].date == "2026-11-23"
        assert await adapter.story_by_record_id("s1") is not None

    asyncio.run(run())


def test_meeting_without_story_is_not_stored(tmp_path):
    led = _ledger(tmp_path)
    adapter = LocalKnowledgeAdapter(led)

    class _Mat:
        title = "接口对齐"
        minute_token = "m1"
        meeting_date = "2026-11-23"
        organizer = "卢燕"
        text = "……"
        minutes_url = ""
        notes_url = ""

    async def run():
        assert await adapter.add_meeting(_Mat(), None, None) == ""
        assert led.stats()["meeting"] == 0

    asyncio.run(run())


def test_dump_rejects_unknown_table(tmp_path):
    led = _ledger(tmp_path)
    out = led.dump("cr; DROP TABLE binding")
    assert out.startswith("⚠️") and "binding" in out
    assert led.stats()["binding"] == 0


def test_stats_counts(tmp_path):
    led = _ledger(tmp_path)
    led.upsert_binding(LocalBinding(story_id="s1", chat_id="oc_a", prd_token="T"))
    led.save_cr({"cr_id": "CR-1", "status": "pending"})
    stats = led.stats()
    assert stats["binding"] == 1 and stats["cr"] == 1 and stats["cr_pending"] == 1
