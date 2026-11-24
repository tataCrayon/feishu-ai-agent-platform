"""easyprd 端到端窗口流程单测（fake LLM + fake 台账 + fake 评论写入，零网络）。

覆盖设计稿 §7.2 里可以离线验的用例：无绑定跳过、dry_run 不写、真写时评论+卡片+台账
三处一致、定位失败降级、CR 编号当日递增。

跑法：python -m pytest tests/domains/prd_watch/test_watcher.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch.ledger import COLUMNS, MemoryLedger, StoryBinding
from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL
from app.domains.prd_watch.render import card_text
from app.domains.prd_watch.watcher import PrdWatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_CHAT = "oc_prd_test"
_ITEM = {
    "title": "申购上限 5万→20万",
    "quote_text": "单笔申购上限为人民币5万元",
    "current_text": "单笔申购上限为人民币5万元，单日累计不超过10万元。",
    "suggested_text": "单笔申购上限为人民币20万元。",
    "change_type": "参数调整", "modules": ["申购"], "compliance": "需合规确认",
    "evidence_quote": "额度改到20万", "speaker": "张三", "scenario": "群讨论",
    "rationale": "VIP 客户额度不够", "confidence": 0.93,
}


def _patch_extractor(extractor: object, payload: dict) -> None:
    """把 ChangeExtractor 的 openai client 换成打桩（同 test_extract 的做法）。"""
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    class _C:
        async def create(self, **_kw: object) -> object:
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)],
                usage=types.SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, total_tokens=15,
                    prompt_tokens_details=None, completion_tokens_details=None))

    extractor._client = types.SimpleNamespace(  # type: ignore[attr-defined]
        chat=types.SimpleNamespace(completions=_C()))


def _binding(chat: str = _CHAT) -> StoryBinding:
    return StoryBinding(story_id="8821", story_title="高净值客户申购额度调整",
                        story_url="https://meegle/8821", prd_token="docPRD",
                        chat_id=chat, owners={"产品": "ou_prod", "研发": "ou_dev"})


def _watcher(payload: dict, *, dry_run: bool = True,
             located: bool = True) -> tuple[PrdWatchConfig, PrdWatcher, list, MemoryLedger]:
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT], dry_run=dry_run,
                         ledger_base_token="b", ledger_change_table="t",
                         ledger_bind_table="bt")
    ledger = MemoryLedger(cfg)
    ledger.add_binding(_binding())
    sent: list[tuple[str, Any]] = []   # payload: 卡片 dict 或纯文本

    async def _send(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=ledger, send_card=_send)
    _patch_extractor(w._extractor, payload)
    # 锚点与评论写入打桩（不碰 lark-cli）
    async def _locate(doc: str, quote: str) -> str:
        return "blk1" if located else ""

    w._anchor.locate = _locate  # type: ignore[method-assign]
    async def _excerpt(doc: str, discussion: str = "", max_chars: int = 4000) -> str:
        return "单笔申购上限为人民币5万元，单日累计不超过10万元。"

    w._anchor.excerpt_for = _excerpt  # type: ignore[method-assign]
    return cfg, w, sent, ledger


def _feed(w: PrdWatcher, lines: list[tuple[str, str]]) -> None:
    for who, text in lines:
        asyncio.run(w.handle_message(chat_id=_CHAT, sender_id=f"ou_{who}",
                                     sender_name=who, text=text))


def test_dry_run_identifies_but_writes_nothing() -> None:
    cfg, w, sent, ledger = _watcher({"changes": [_ITEM]}, dry_run=True)
    _feed(w, [("张三", "单笔申购上限从5万提到20万吧，VIP客户不够用")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert result.changes and result.changes[0].title == "申购上限 5万→20万"
    assert result.changes[0].block_id == "blk1"
    assert sent == [], "dry_run 不得发卡片"
    assert ledger._changes == {}, "dry_run 不得写台账"
    assert cfg.dry_run and w.metrics["dry_run"] == 1


def test_emit_writes_comment_card_and_ledger() -> None:
    _cfg, w, sent, ledger = _watcher({"changes": [_ITEM]}, dry_run=False)
    written: list[tuple[str, str, str]] = []

    async def _fake_write(cp, binding, comment, _link=""):
        written.append((binding.prd_token, cp.block_id, comment))
        return "com_1"

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "上限改20万"), ("李四", "单日累计同步到50万")])
    result = asyncio.run(w.flush_now(_CHAT))

    assert len(written) == 1
    doc, block, comment = written[0]
    assert (doc, block) == ("docPRD", "blk1")
    assert "CR-" in comment and "【变更依据】" in comment
    # 卡片现在是交互卡片 dict（对齐飞书项目机器人形态），断言取给人看的文本
    assert len(sent) == 1 and isinstance(sent[0][1], dict), sent
    card = card_text(sent[0][1])
    assert "📋 PRD 变更建议提醒（1 条 · 1 个 Story）" in card
    assert "[高净值客户申购额度调整](https://meegle/8821)" in card   # Story 列给名称（可点）
    assert "https://applink.feishu.cn/client/chat/open?openChatId=" in card  # 变更依据可点
    assert "<at id=ou_prod></at>" in card
    row = next(iter(ledger._changes.values()))
    assert row[COLUMNS["decision"]] == "pending"
    assert row["评论ID"] == "com_1"
    assert result.changes[0].band == BAND_NORMAL


def test_unanchored_falls_back_to_full_comment() -> None:
    _cfg, w, _sent, ledger = _watcher({"changes": [_ITEM]}, dry_run=False,
                                      located=False)
    written: list[tuple[str, str, str]] = []

    async def _fake_write(cp, binding, comment, _link=""):
        written.append((binding.prd_token, cp.block_id, comment))
        return ""

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "新增一条 VIP 风险揭示要求")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert result.unanchored == 1
    assert written[0][1] == ""          # 空 block_id → 整篇评论
    assert "com" not in str(next(iter(ledger._changes.values())).get("评论ID"))


def test_low_confidence_still_emits_flagged() -> None:
    mid = dict(_ITEM, confidence=0.75)
    _cfg, w, _sent, ledger = _watcher({"changes": [mid]}, dry_run=False)

    async def _fake_write(cp, binding, comment, _link=""):
        return "com_x"

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "要不要把额度改大点，还没定")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert result.changes[0].band == BAND_FLAGGED
    assert result.flagged == 1
    # 待确认条也要落台账（否则产品后来回复时无从对上 CR）
    assert next(iter(ledger._changes.values()))["决策状态"] == "pending"


def test_no_binding_group_skipped() -> None:
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT],
                         ledger_base_token="", ledger_change_table="",
                         ledger_bind_table="")
    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=MemoryLedger(cfg))
    _feed(w, [("张三", "额度改20万")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert result.candidates == 0 and w.metrics["no_binding"] == 1


def test_cr_ids_increment_per_day() -> None:
    two = {"changes": [_ITEM, dict(_ITEM, title="赎回改 T+1", change_type="流程变更")]}
    _cfg, w, sent, _ledger = _watcher(two, dry_run=False)
    ids: list[str] = []

    async def _fake_write(cp, binding, comment, _link=""):
        ids.append(comment.splitlines()[0])
        return f"com_{len(ids)}"

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "上限改20万"), ("李四", "赎回改成T+1")])
    asyncio.run(w.flush_now(_CHAT))
    assert ids[0].endswith("-001") and ids[1].endswith("-002"), ids
    assert len(sent) == 1  # 一批变更合成一张卡片，不逐条刷屏


def test_empty_window_noop() -> None:
    _cfg, w, sent, _ledger = _watcher({"changes": []})
    assert asyncio.run(w.flush_now("oc_none")).candidates == 0
    _feed(w, [("张三", "   ")])
    assert asyncio.run(w.flush_now(_CHAT)).changes == []
    assert sent == []


def test_snapshot_shape() -> None:
    _cfg, w, _sent, _ledger = _watcher({"changes": []})
    snap = w.snapshot()
    assert snap["enabled"] and snap["dry_run"]
    assert snap["watch_chats"] == 1 and snap["ledger"] == "ready"
    assert "candidates" in snap["metrics"]


def test_record_hook_receives_result() -> None:
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT], dry_run=True)
    ledger = MemoryLedger(cfg)
    ledger.add_binding(_binding())
    seen: list = []
    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=ledger,
                   record_hook=lambda res, stats: seen.append((res, stats)))
    _patch_extractor(w._extractor, {"changes": [_ITEM]})

    async def _locate(doc: str, quote: str) -> str:
        return "blk1"

    async def _excerpt(doc: str, discussion: str = "", max_chars: int = 4000) -> str:
        return "原文"

    w._anchor.locate = _locate  # type: ignore[method-assign]
    w._anchor.excerpt_for = _excerpt  # type: ignore[method-assign]
    _feed(w, [("张三", "额度改20万")])
    asyncio.run(w.flush_now(_CHAT))
    assert len(seen) == 1
    res, stats = seen[0]
    assert res.changes and stats["messages"] == 1 and stats["window_text"]


def test_multi_story_discussion_splits_by_attribution() -> None:
    """一场会牵涉多个 Story / 多篇 PRD —— 归属不能串篇。

    期望：① 每条评论落在**它自己那篇** PRD 上；② 只发**一张**卡片；
    ③ 模型给的 story_id 不在候选集里 → 丢弃并计数（写进别人的 PRD 比漏报代价高）。
    """
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT], dry_run=False,
                         ledger_base_token="b", ledger_change_table="t",
                         ledger_bind_table="bt")
    ledger = MemoryLedger(cfg)
    ledger.add_binding(StoryBinding(story_id="8821", story_title="RWA申购下单",
                                    prd_token="docA", chat_id=_CHAT,
                                    owners={"产品": "ou_prod"},
                                    prd_url="https://x.feishu.cn/docx/docA"))
    ledger.add_binding(StoryBinding(story_id="9900", story_title="RWA赎回下单",
                                    prd_token="docB", chat_id=_CHAT,
                                    owners={"测试": "ou_qa"},
                                    prd_url="https://x.feishu.cn/docx/docB"))
    sent: list[tuple[str, Any]] = []

    async def _send(chat_id: str, payload: Any) -> None:
        sent.append((chat_id, payload))

    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=ledger, send_card=_send)
    payload = {"changes": [
        dict(_ITEM, story_id="8821"),
        dict(_ITEM, title="赎回改 T+1", story_id="9900"),
        dict(_ITEM, title="编出来的归属", story_id="7777"),   # 候选集外，必须被拦
    ]}
    _patch_extractor(w._extractor, payload)

    async def _locate(doc: str, quote: str) -> str:
        return f"blk_{doc}"

    async def _excerpt(doc: str, discussion: str = "", max_chars: int = 4000) -> str:
        return f"{doc} 的正文节选"

    w._anchor.locate = _locate  # type: ignore[method-assign]
    w._anchor.excerpt_for = _excerpt  # type: ignore[method-assign]
    written: list[tuple[str, str]] = []

    async def _fake_write(cp, binding, comment, _link=""):
        written.append((binding.prd_token, cp.title))
        return f"com_{len(written)}"

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "申购上限改20万；另外赎回到账改成 T+1")])
    result = asyncio.run(w.flush_now(_CHAT))

    # 各归各篇（去重层会先把近义条目合并，这里两条标题不同、主语不同 → 不会被合掉）
    assert sorted(t for t, _ in written) == ["docA", "docB"], written
    assert result.unattributed == 1 and len(result.changes) == 2
    assert w.metrics["unattributed"] == 1
    assert len(sent) == 1, "多 Story 也只发一张卡片"
    header = next(e for e in sent[0][1]["body"]["elements"]
                  if e.get("tag") == "column_set" and e.get("background_style") == "grey")
    data_rows = [e for e in sent[0][1]["body"]["elements"]
                 if e.get("tag") == "column_set" and e is not header]
    # 每条变更 = 两行网格（信息行 + diff 行）+ 一行按钮
    grids = [r for r in data_rows if r["columns"][0]["elements"][0].get("tag") == "markdown"]
    assert len(grids) == 4
    card = card_text(sent[0][1])
    assert "2 条 · 2 个 Story" in card
    flat = "\n".join(str(v) for r in data_rows for v in r.values())
    # 每「条」只带自己那篇 PRD 的链接：串篇就是最严重的错位
    # （原文列现在是块直达链接 `{prd_url}#{block_id}`，所以 docA/docB 会各出现两次，
    #   但同一条变更的两行里绝不能同时出现两篇文档。按钮行不含文档信息，不参与比较。）
    blocks = ["\n".join(str(v) for r in grids[i:i + 2] for v in r.values())
              for i in (0, 2)]
    for msg in blocks:
        assert ("docA" in msg) != ("docB" in msg), msg
    assert "docA#blk_docA" in flat and "docB#blk_docB" in flat   # 可点跳转到原文位置


def test_single_story_path_does_not_require_attribution() -> None:
    """候选只有一篇时不要求模型回填 story_id（旧行为一字不变）。

    多一篇就多一层出错面：单篇群若也走归属校验，模型偶尔不填 story_id 就会
    把所有变更丢光——那是"看起来在跑、其实一条都不写"的最难查故障。
    """
    _cfg, w, _sent, _ledger = _watcher({"changes": [_ITEM]}, dry_run=False)

    async def _fake_write(cp, binding, comment, _link=""):
        return "com_1"

    w._write_comment = _fake_write  # type: ignore[method-assign]
    _feed(w, [("张三", "上限改20万")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert len(result.changes) == 1 and result.unattributed == 0
    assert result.changes[0].story_id == "8821"     # 由调用方补，不靠模型


def test_empty_prd_excerpt_is_not_offered_as_candidate() -> None:
    """正文取不到的 PRD 不进候选：放进去等于邀请模型把变更归给一篇读不到的文档。"""
    cfg = PrdWatchConfig(enabled=True, watch_chats=[_CHAT], dry_run=True)
    ledger = MemoryLedger(cfg)
    ledger.add_binding(StoryBinding(story_id="8821", story_title="A", prd_token="docA",
                                    chat_id=_CHAT))
    ledger.add_binding(StoryBinding(story_id="9900", story_title="B", prd_token="docB",
                                    chat_id=_CHAT))
    w = PrdWatcher(cfg, RouterConfig(api_key="k"), ledger=ledger)
    _patch_extractor(w._extractor, {"changes": [dict(_ITEM, story_id="9900")]})

    async def _excerpt(doc: str, discussion: str = "", max_chars: int = 4000) -> str:
        return "" if doc == "docB" else "有正文"

    w._anchor.excerpt_for = _excerpt  # type: ignore[method-assign]
    _feed(w, [("张三", "赎回到账改 T+1")])
    result = asyncio.run(w.flush_now(_CHAT))
    assert w.metrics["no_excerpt"] == 1
    assert result.unattributed == 1 and result.changes == []


def test_watch_chats_merges_binding_groups() -> None:
    """授权群 = watch_chats ∪ story_chat_map 的群。

    分两处填必然出现填漏一处的静默失效：只填 watch_chats → 收消息但永不产出建议；
    只填 story_chat_map → 消息根本推不到本进程。合并后任一填了即生效。
    """
    from app.domains.prd_watch.binding import BindingSource

    cfg = PrdWatchConfig(enabled=True, watch_chats=["oc_a"])
    w = PrdWatcher(cfg, RouterConfig(api_key="k"))
    assert w.watch_chats() == ["oc_a"]
    binding = BindingSource(client=None, story_chat_map={"8821": "oc_b", "8822": "oc_a"})
    w2 = PrdWatcher(cfg, RouterConfig(api_key="k"), binding=binding)
    assert w2.watch_chats() == ["oc_a", "oc_b"], w2.watch_chats()


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")