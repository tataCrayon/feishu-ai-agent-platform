"""分析意见按钮单测：采纳写评论 / 忽略只记录 / 🙂🙃 打分 / 修正走人工表述。

这里钉住的是一条**容易做错的边界**：分析意见（`AN-`）与讨论变更（`CR-`）
共用按钮外壳与回调入口，但副作用完全不同——

- 讨论变更的采纳是「回复已有评论」（watcher 先写过一条）；
- 分析意见没有评论，采纳是**新增一条**评论锚在原段；
- 🙂/🙃 不产生任何写操作，只落准确率样本（误报多的规则据此收窄）。

跑法：python -m pytest tests/domains/prd_watch/test_analysis_feedback.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

from app.config import PrdWatchConfig
from app.domains.prd_watch.analysis import Finding, StoryDigest
from app.domains.prd_watch.analysis_card import build_action_row, ordered_findings
from app.domains.prd_watch.analysis_feedback import AnalysisFeedback, item_id_for
from app.domains.prd_watch.card_actions import (
    CardActionHandler,
    extract_rating,
    is_analysis_id,
)
from app.domains.prd_watch.ledger import MemoryLedger

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class _FakeLark:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str, str, str]] = []
        self.fail = False

    async def add_comment(self, doc: str, text: str, *, block_id: str = "", **_kw: Any) -> dict:
        if self.fail:
            raise RuntimeError("no comment scope")
        self.comments.append((doc, block_id, text, _kw.get("link_url", "")))
        return {"ok": True}


def _cfg() -> PrdWatchConfig:
    return PrdWatchConfig(enabled=True, dry_run=False, comment_identity="bot")


def _handler(tmp_path: Any, lark: _FakeLark | None = None):
    fb = AnalysisFeedback(tmp_path)
    lark = lark or _FakeLark()
    h = CardActionHandler(_cfg(), MemoryLedger(_cfg()), lark=lark,  # type: ignore[arg-type]
                          feedback=fb)
    h.feedback._feedback = fb
    return h, fb, lark


def _digest() -> StoryDigest:
    return StoryDigest(
        story_id="7124060677", title="【webportal】VA - 钱包管理",
        prd_token="PRD1", prd_url="https://x.feishu.cn/wiki/PRD1",
        findings=[
            Finding(title="数量不符", quote="原文A", suggestion="改成 6 个",
                    block_id="b1", section="绑定钱包", kind="数量"),
            Finding(title="占位符残留", quote="原文B", suggestion="补齐",
                    block_id="b2", section="绑定钱包", kind="占位符"),
        ])


def _event(value: dict[str, Any], chat_id: str = "oc_x") -> Any:
    return types.SimpleNamespace(
        action=types.SimpleNamespace(value=value), chat_id=chat_id,
        message_id="om_1", operator=types.SimpleNamespace(open_id="ou_prod", name="产品"))


def _item(item_id: str):
    from app.domains.prd_watch.analysis_feedback import AnalysisItem

    return AnalysisItem(item_id=item_id, title="t", prd_token="PRD1")


# ── 1. 编号与卡片一致 ───────────────────────────────────────────────
def test_item_id_matches_card_numbering(tmp_path) -> None:
    """卡片显示的序号 = 登记簿的序号，否则点了 3 号写出来的是 4 号。"""
    digest = _digest()
    pairs = ordered_findings(digest)
    assert [i for i, _ in pairs] == [1, 2]
    fb = AnalysisFeedback(tmp_path)
    items = fb.register(digest)
    assert [i.item_id for i in items] == [item_id_for("7124060677", 1),
                                          item_id_for("7124060677", 2)]
    assert items[0].block_id == "b1" and items[1].block_id == "b2"


def test_card_row_has_action_and_rating_buttons() -> None:
    row = build_action_row("AN-1-01")
    buttons = [c["elements"][0] for c in row["columns"]]
    acts = [b["value"].get("act") for b in buttons]
    rates = [b["value"].get("rate") for b in buttons]
    assert acts[:3] == ["adopt", "fix", "ignore"]
    assert rates[3:] == ["useful", "false"]
    assert all(b["behaviors"][0]["type"] == "callback" for b in buttons)


def test_extract_rating_only_claims_rate_key() -> None:
    assert extract_rating(_event({"cr": "AN-1-01", "rate": "useful"})) == ("AN-1-01", "useful")
    assert extract_rating(_event({"cr": "AN-1-01", "act": "adopt"})) == ("", "")
    assert extract_rating(_event({"cr": "AN-1-01", "rate": "meh"})) == ("", "")


def test_prefix_split() -> None:
    assert is_analysis_id("AN-7124060677-01")
    assert not is_analysis_id("CR-20261103-8821-001")


# ── 2. 采纳 = 新增一条评论（锚在原段），不改正文 ─────────────────────
def test_adopt_writes_comment_on_block(tmp_path) -> None:
    h, fb, lark = _handler(tmp_path)
    fb.register(_digest())
    text = asyncio.run(h.handle(_event({"cr": "AN-7124060677-01", "act": "adopt"})))
    assert lark.comments, "采纳必须落一条 PRD 评论"
    doc, block_id, body, link = lark.comments[0]
    assert doc == "PRD1" and block_id == "b1"          # 锚在原段，不飘到文档顶部
    assert "数量不符" in body and "改成 6 个" in body
    assert link == "https://x.feishu.cn/wiki/PRD1"
    assert "已采纳" in text
    assert fb.stats().adopted == 1


def test_adopt_without_prd_token_refuses_honestly(tmp_path) -> None:
    h, fb, lark = _handler(tmp_path)
    digest = _digest()
    digest.prd_token = ""
    fb.register(digest)
    text = asyncio.run(h.handle(_event({"cr": "AN-7124060677-01", "act": "adopt"})))
    assert "没有 PRD 坐标" in text and lark.comments == []


def test_comment_failure_is_reported_not_swallowed(tmp_path) -> None:
    lark = _FakeLark()
    lark.fail = True
    h, fb, _ = _handler(tmp_path, lark)
    fb.register(_digest())
    text = asyncio.run(h.handle(_event({"cr": "AN-7124060677-01", "act": "adopt"})))
    assert "写入失败" in text and "正文" in text
    assert fb.stats().adopted == 0


def test_unknown_item_says_so(tmp_path) -> None:
    h, _fb, _lark = _handler(tmp_path)
    text = asyncio.run(h.handle(_event({"cr": "AN-999-01", "act": "adopt"})))
    assert "找不到" in text


# ── 3. 忽略与打分 ──────────────────────────────────────────────────
def test_ignore_records_without_comment(tmp_path) -> None:
    h, fb, lark = _handler(tmp_path)
    fb.register(_digest())
    asyncio.run(h.handle(_event({"cr": "AN-7124060677-02", "act": "ignore"})))
    assert lark.comments == []


def test_rating_writes_no_comment(tmp_path) -> None:
    h, fb, lark = _handler(tmp_path)
    fb.register(_digest())
    text = asyncio.run(h.handle_rating(_event({"cr": "AN-7124060677-01", "rate": "false"})))
    assert "已记录" in text and lark.comments == []
    stat = fb.stats()
    assert stat.false_positive == 1 and stat.adopted == 0
    assert stat.by_kind["数量"] == [0, 1]


def test_rating_summary_does_not_claim_zero_accuracy_when_unrated(tmp_path) -> None:
    """没人评过 ≠ 准确率 0%：状态面板不能把空样本说成坏结果。"""
    fb = AnalysisFeedback(tmp_path)
    assert "暂无评分" in fb.summary_line()


def test_feedback_persists_across_instances(tmp_path) -> None:
    fb = AnalysisFeedback(tmp_path)
    fb.register(_digest())
    fb.record("AN-7124060677-01", "useful", operator="ou_a")
    again = AnalysisFeedback(tmp_path)
    assert again.get("AN-7124060677-01") is not None      # 重启后老卡片还能点
    assert again.stats().useful == 1


def test_registry_written_by_other_process_is_visible(tmp_path) -> None:
    """另一个进程（MCP 子进程）登记的条目，本进程（main）点了要查得到。

    这是线上真实布局：评审卡由 dsh 的 MCP 子进程发、按钮回调落在 main，
    两边共用同一份 `analysis_items.json`。只在 `__init__` 读一次的话，
    发卡之后才登记的条目在主进程里永远查不到，用户看到「找不到登记记录」。
    """
    import time

    main_side = AnalysisFeedback(tmp_path)     # 主进程启动时构造
    mcp_side = AnalysisFeedback(tmp_path)      # 发卡的那一侧
    time.sleep(0.01)                            # 让 mtime 真的往后走
    mcp_side.register_items([_item("RV-doxcnT-B1")])

    assert main_side.get("RV-doxcnT-B1") is not None


def test_reload_merges_instead_of_replacing(tmp_path) -> None:
    """重载只合并、不覆盖：本进程刚登记还没落盘的条目不能被别人的旧快照冲掉。"""
    import time

    a = AnalysisFeedback(tmp_path)
    b = AnalysisFeedback(tmp_path)
    a.register_items([_item("RV-doxcnT-B1")])
    time.sleep(0.01)
    b.register_items([_item("RV-doxcnT-B2")])   # b 落盘时会先合并 a 的条目
    assert a.get("RV-doxcnT-B1") is not None
    assert a.get("RV-doxcnT-B2") is not None


def test_fix_state_survives_forwarded_card(tmp_path) -> None:
    """卡片被转发到别的会话后，点「修正」再回一句仍然能落评论。

    转发后回调带回的是**转发目标**会话 id；待修正状态若按 (chat_id, open_id)
    存，回复时 chat_id 不同就永远对不上——用户照着提示回一句，却被告知没有待修正条目。
    """
    h, fb, _lark = _handler(tmp_path)
    fb.register(_digest())
    asyncio.run(h.handle(_event({"cr": "AN-7124060677-01", "act": "fix"},
                                chat_id="oc_forward_target")))
    text = asyncio.run(h.apply_fix("oc_original", "ou_prod", "支持 6 种"))
    assert "已按你的表述" in text


# ── 4. 修正：以人的表述为准 ────────────────────────────────────────
def test_fix_prompt_then_apply_human_wording(tmp_path) -> None:
    h, fb, lark = _handler(tmp_path)
    fb.register(_digest())
    prompt = asyncio.run(h.handle(_event({"cr": "AN-7124060677-01", "act": "fix"})))
    assert "回复" in prompt and lark.comments == []
    text = asyncio.run(h.apply_fix("oc_x", "ou_prod", "改成支持 6 种，按实际列出"))
    assert "已按你的表述" in text
    body = lark.comments[0][2]
    assert "改成支持 6 种" in body and "机器原建议不写入" in body


# ── 5. 文字指令：转发来的卡片按钮点不动时的退路 ────────────────────
def test_plain_command_parsing_is_strict() -> None:
    """只认极短一句：判宽了会把群里正常讨论当成指令去写 PRD 评论。"""
    from app.domains.prd_watch.card_actions import parse_plain_command

    assert parse_plain_command("采纳 B1") == ("adopt", "B1")
    assert parse_plain_command("采纳B1") == ("adopt", "B1")
    assert parse_plain_command("B1 采纳") == ("adopt", "B1")
    assert parse_plain_command("判错 q2") == ("false", "Q2")
    assert parse_plain_command("我觉得 B1 采纳一下") == ("", "")
    assert parse_plain_command("采纳") == ("", "")
    assert parse_plain_command("帮我采纳 B1 吧") == ("", "")


def test_plain_command_adopt_writes_comment_like_button(tmp_path) -> None:
    """`采纳 B1` 与点按钮走同一条副作用：在原文块写评论、记一条 adopt 样本。"""
    h, fb, lark = _handler(tmp_path)
    fb.register_items([_item("RV-docKey-B1")])
    text = asyncio.run(h.handle_command("采纳 B1", open_id="ou_prod"))
    assert "已采纳" in text
    assert lark.comments and lark.comments[0][1] == ""   # 这条没锚块，整篇评论


def test_plain_command_ambiguous_code_lists_candidates(tmp_path) -> None:
    """多篇文档都有 B1 时不猜：列出候选，让人补全。"""
    h, fb, _lark = _handler(tmp_path)
    fb.register_items([_item("RV-docA-B1"), _item("RV-docB-B1")])
    text = asyncio.run(h.handle_command("忽略 B1", open_id="ou_prod"))
    assert "都有" in text and "RV-docA-B1" in text and "RV-docB-B1" in text


def test_plain_command_unknown_code_says_so(tmp_path) -> None:
    h, fb, _lark = _handler(tmp_path)
    fb.register(_digest())
    text = asyncio.run(h.handle_command("采纳 B9", open_id="ou_prod"))
    assert "没有编为" in text


def test_review_card_teaches_plain_command() -> None:
    """卡片脚注必须给出文字指令：否则转发后用户只看到「无法直接操作」，没有退路。"""
    from app.domains.prd_watch.review_card import (
        ReviewBundle,
        ReviewItem,
        build_review_card,
    )

    bundle = ReviewBundle(title="x", items=[
        ReviewItem(level="阻断", kind="一致性", title="t1"),
        ReviewItem(level="建议", title="t2"),
    ])
    text = str(build_review_card(bundle, actions=True))
    assert "转发" in text and "采纳" in text and "B1" in text


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
