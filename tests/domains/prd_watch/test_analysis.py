"""需求分析（`analysis.py`）与版本关联（`MeegleClient.list_by_name`）的单测。

覆盖 2026-11-16 这一轮新增的能力，重点在**边界与误报**：

1. 机械自检只报可证明的问题（数量不符、集合差集、删除线残留），不猜；
2. 删除线在 docx XML 里是 `<del>` 标签，不是 markdown 的 `~~`（读错就整段漏掉）；
3. Story 链接解析：链接/裸 ID 认，其它一律返回空（不猜）；
4. 同族需求按名称前缀查，**只列候选**，绝不自动建关系；
5. 设计稿链接三级回退：Story 字段 → 设计工单 → 明确说没有。

跑法：python -m pytest tests/domains/prd_watch/test_analysis.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types

from app.domains.prd_watch import analysis
from app.domains.prd_watch.analysis import (
    StoryDigest,
    build_digest,
    mechanical_findings,
    name_prefix,
    parse_story_ref,
    version_tokens,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _block(bid: str, text: str, tag: str = "p", raw: str = "") -> dict[str, str]:
    return {"block_id": bid, "tag": tag, "text": text, "raw": raw or text}


# ── 1. 机械自检：只报可证明的问题 ──────────────────────────────────
def test_count_mismatch_is_reported_with_block() -> None:
    blocks = [
        _block("h1", "钱包管理&钱包绑定", "h1"),
        _block("b1", "支持的所有权验证币种：BTC、ETH、USDT、USDC、TRX、SOL（共 7 种）。"
                     "区块链与币种为多对多关系。"),
    ]
    found = mechanical_findings(blocks, prd_url="https://x.feishu.cn/wiki/T1")
    assert len(found) == 1
    f = found[0]
    assert "共 7" in f.title and "6" in f.title
    assert f.quote.startswith("支持的所有权验证币种")   # 原文字引用逐字
    assert f.block_id == "b1" and f.jump_url.endswith("#b1")


def test_count_consistent_produces_no_finding() -> None:
    """数量对得上就不该报——这是最容易被"为了有输出"写坏的地方。"""
    blocks = [_block("b1", "支持币种：BTC、ETH、USDT（共 3 种）。")]
    assert mechanical_findings(blocks) == []


def test_chain_set_diff_reports_extra_and_missing() -> None:
    blocks = [
        _block("c1", "支持的区块链网络：BTC、ETH、TRON、Solana 共 4 条"),
        _block("c2", "支持校验主流区块链地址格式：Ethereum、Solana、Tron、Polygon等"),
    ]
    found = [f for f in mechanical_findings(blocks) if f.kind == "集合"]
    assert found, "支持链与校验链的集合差必须被报出来"
    text = found[0].suggestion
    assert "Polygon" in text and "BTC" in text


def test_strikethrough_uses_del_tag_not_markdown() -> None:
    """docx XML 的删除线是 `<del>`，markdown 导出才是 `~~`。

    只认 `~~` 时这个 PRD 一条删除线都读不到（实测 10 个段落全漏）。
    """
    blocks = [
        _block("s1", "注意星企通新增了绑定费用功能", raw="<del>注意星企通新增了绑定费用功能</del>"),
        _block("s2", "费用展示：列表页新增绑定费用字段"),
    ]
    found = [f for f in mechanical_findings(blocks) if f.kind == "删除线"]
    assert found and "1 个段落" in found[0].title
    assert found[0].block_id == "s1"


def test_no_strikethrough_no_finding() -> None:
    blocks = [_block("s1", "普通条款，无划线", raw="普通条款，无划线")]
    assert [f for f in mechanical_findings(blocks) if f.kind == "删除线"] == []


# ── 2. Story 引用解析 ─────────────────────────────────────────────
def test_parse_story_ref_accepts_link_and_bare_id() -> None:
    assert parse_story_ref("https://project.feishu.cn/examplecorp/story/detail/7124060677") == "7124060677"
    assert parse_story_ref("7124060677") == "7124060677"
    assert parse_story_ref("钱包管理") == ""          # 名称不当 ID 猜
    assert parse_story_ref("") == ""


# ── 3. 版本关联：名称前缀 ─────────────────────────────────────────
def test_name_prefix_strips_bracket_and_keeps_product_family() -> None:
    assert name_prefix("【webportal】VA - 钱包管理&钱包绑定") == "【webportal】VA"
    assert name_prefix("【Endor】钱包绑定流程调整") == "【Endor】"
    assert name_prefix("没有前缀的需求") == ""


def test_version_tokens() -> None:
    assert version_tokens("钱包绑定（GitLab原文口径，V1.1 新增必填）") == ["V1.1"]
    assert version_tokens("无版本") == []


# ── 4. build_digest：设计稿三级回退 + 同族候选 ────────────────────
class _FakeStory:
    def __init__(self, **kw: object) -> None:
        self.story_id = "7124060677"
        self.title = "【webportal】VA - 钱包管理&钱包绑定"
        self.status = "5-待开发"
        self.node = "容量评估"
        self.url = "https://p/story/detail/7124060677"
        self.space_name = "空间"
        self.priority = "1"
        self.business = ""
        self.prd_url = ""
        self.prd_token = ""
        self.design_url = ""
        self.design_field = ""
        self.owners = {"产品": "丁浩"}
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeMeegle:
    def __init__(self, story: _FakeStory, workorder: tuple[str, str, str] = ("", "", ""),
                 siblings: list[types.SimpleNamespace] | None = None) -> None:
        self._story = story
        self._workorder = workorder
        self._siblings = siblings or []

    def get_story(self, story_id: str) -> _FakeStory:
        return self._story

    def list_by_name(self, keyword: str, limit: int = 30) -> list[types.SimpleNamespace]:
        self.seen_keyword = keyword
        return self._siblings

    def design_link_from_workorder(self, title: str) -> tuple[str, str, str]:
        return self._workorder

    def sprint_of(self, story_id: str, **_kw: object) -> tuple[str, str, list]:
        """同迭代反查：单测里不联网，默认「不在任何迭代」。"""
        return "", "", []

    @staticmethod
    def story_url(story_id: str) -> str:
        return f"https://project.feishu.cn/examplecorp/story/detail/{story_id}"


class _FakeAnchor:
    def __init__(self, blocks: list[dict[str, str]]) -> None:
        self._blocks = blocks

    async def _all_blocks(self, doc: str) -> list[dict[str, str]]:
        return self._blocks

    async def title(self, doc: str) -> str:
        return "钱包管理&钱包绑定"


def test_design_link_falls_back_to_design_workorder() -> None:
    """Story 上三个设计字段全空时，必须去「设计工单」找（本需求就是这种）。"""
    story = _FakeStory(prd_token="PRD1")
    meegle = _FakeMeegle(story, workorder=(
        "https://www.figma.com/design/ABC/RWA?node-id=1-2", "设计稿链接", "7130054949"))
    digest = asyncio.run(build_digest(story_ref="7124060677", meegle=meegle,
                                      anchor=_FakeAnchor([])))
    assert digest.design_url.startswith("https://www.figma.com")
    assert "设计工单" in digest.design_field


def test_design_link_missing_is_reported_not_guessed() -> None:
    story = _FakeStory()
    meegle = _FakeMeegle(story, workorder=("", "", ""))
    digest = asyncio.run(build_digest(story_ref="7124060677", meegle=meegle,
                                      anchor=_FakeAnchor([])))
    assert digest.design_url == ""
    # 设计稿缺失记在 design_errors：与「没有 PRD」分开，否则排查方向会跑偏
    assert any("设计稿" in e for e in digest.design_errors)


def test_siblings_are_candidates_only() -> None:
    """同族需求只列候选：digest 里是 related 列表，不写任何关系字段。"""
    story = _FakeStory(prd_token="PRD1")
    siblings = [
        types.SimpleNamespace(story_id="7124060677", title="自己", status="x", design_url="", url=""),
        types.SimpleNamespace(story_id="7124009721", title="【webportal】VA - 充币",
                              status="5-待开发", design_url="", url="u"),
    ]
    meegle = _FakeMeegle(story, siblings=siblings)
    digest = asyncio.run(build_digest(story_ref="7124060677", meegle=meegle,
                                      anchor=_FakeAnchor([])))
    assert [r["story_id"] for r in digest.related] == ["7124009721"]  # 自己不在候选里
    assert "前缀" in digest.related_basis
    assert not hasattr(digest, "parent_ids")     # 不产生任何关系写回字段


def test_bad_story_ref_returns_error_digest() -> None:
    digest = asyncio.run(build_digest(story_ref="随便一句话", meegle=_FakeMeegle(_FakeStory())))
    assert digest.story_id == "" and digest.errors


def test_meeting_search_failure_is_not_fatal() -> None:
    """妙记搜索挂了不影响分析主链路（PRD 自检仍要出结果）。"""
    story = _FakeStory(prd_token="PRD1")
    meegle = _FakeMeegle(story)

    async def _boom(_q: str) -> list[dict[str, str]]:
        raise RuntimeError("minutes down")

    digest = asyncio.run(build_digest(story_ref="7124060677", meegle=meegle,
                                      anchor=_FakeAnchor([_block("b1", "共 1 个")]),
                                      search_meetings=_boom))
    assert digest.meeting_errors
    assert digest.story_id == "7124060677"


def test_story_digest_people_order() -> None:
    d = StoryDigest(owners={"测试": "胡萍", "产品": "丁浩", "设计师": "黄昕頔"})
    assert d.people == "产品 丁浩｜测试 胡萍｜设计师 黄昕頔"


# ── 5. 卡片版式与按钮契约（行网格版） ─────────────────────
def test_related_buttons_do_not_write_relations() -> None:
    """同族按钮只带 sid/rel：不产生任何需求关系写回字段。"""
    from app.domains.prd_watch.analysis_card import build_related_buttons
    from app.domains.prd_watch.card_actions import extract_related

    row = build_related_buttons("7124009721")
    buttons = [c["elements"][0] for c in row["columns"]]
    assert [b["value"]["rel"] for b in buttons] == ["analyze", "skip"]
    assert all(b["value"]["sid"] == "7124009721" for b in buttons)
    assert all("cr" not in b["value"] for b in buttons)   # 不会被变更卡片误领
    ev = types.SimpleNamespace(action=types.SimpleNamespace(
        value={"sid": "7124009721", "rel": "analyze"}), chat_id="oc_x")
    assert extract_related(ev) == ("7124009721", "analyze")


def test_finding_row_keeps_suggestion_and_quote_labelled() -> None:
    """每条发现必须带「建议」「原文」「位置」三个标签：网格化后行数变少，
    少了标签读者就分不清哪句是原文、哪句是提议（`**建议：**` 与中文粘连时
    飞书不渲染加粗，所以标签用独立灰字，不拼进加粗串）。"""
    from app.domains.prd_watch.analysis_card import _grid_row
    from app.domains.prd_watch.render import card_text as render_text

    row = _grid_row(analysis.Finding(title="t", quote="原文一句", suggestion="改成 6 个",
                                     kind="数量", section="3.1 币种"), index=1)
    text = render_text({"body": {"elements": row}})
    assert "建议：" in text and "原文：" in text and "位置：" in text
    assert "改成 6 个" in text and "原文一句" in text and "3.1 币种" in text
    assert "**建议：**" not in text


def test_card_marks_capability_test() -> None:
    """尾部必须带「easyprd 能力测试」标注：避免被当成正式评审结论。"""
    from app.domains.prd_watch.analysis_card import _tip_lines

    tips = _tip_lines(StoryDigest(story_id="1"))
    assert any("easyprd 能力测试" in t for t in tips)


def test_ui_section_reports_not_compared_honestly() -> None:
    """没取到 Figma 素材时写「未比对 + 原因」，不能静默写成「未发现差异」。"""
    from app.domains.prd_watch.analysis_card import build_story_card, card_text

    digest = StoryDigest(story_id="1", title="t", prd_url="https://x",
                         design_url="https://www.figma.com/design/K/1",
                         ui_errors=["未配置 Figma token"])
    text = card_text(build_story_card(digest, actions=False))
    assert "未比对" in text and "Figma token" in text


# ── 6. Figma token 解析（配在 mcp-gateway 里也要能找到） ────────────
def test_figma_token_falls_back_to_gateway_backends(tmp_path, monkeypatch) -> None:
    """实测：这台机器的 token 一直在 mcp-gateway 的 figma backend env 里。

    bot 是独立进程，不继承 Claude Code 的 MCP env；只认 .env 就会出现
    「明明配过却报未配置」。
    """
    import json as _json

    from app.domains.prd_watch import ui

    backends = tmp_path / "backends.json"
    backends.write_text(_json.dumps({
        "figma": {"env": {"FIGMA_API_KEY": "figd_from_gateway"}},
    }), encoding="utf-8")
    monkeypatch.setattr(ui, "_GATEWAY_BACKENDS", backends)
    monkeypatch.delenv("FIGMA_API_KEY", raising=False)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert ui.resolve_token() == "figd_from_gateway"


def test_env_token_wins_over_gateway(tmp_path, monkeypatch) -> None:
    import json as _json

    from app.domains.prd_watch import ui

    backends = tmp_path / "backends.json"
    backends.write_text(_json.dumps({"figma": {"env": {"FIGMA_API_KEY": "gw"}}}),
                        encoding="utf-8")
    monkeypatch.setattr(ui, "_GATEWAY_BACKENDS", backends)
    monkeypatch.setenv("FIGMA_API_KEY", "from_env")
    assert ui.resolve_token() == "from_env"


def test_missing_everywhere_returns_empty(tmp_path, monkeypatch) -> None:
    from app.domains.prd_watch import ui

    monkeypatch.setattr(ui, "_GATEWAY_BACKENDS", tmp_path / "nope.json")
    monkeypatch.delenv("FIGMA_API_KEY", raising=False)
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    assert ui.resolve_token() == ""


def test_expired_error_is_distinguished_from_missing() -> None:
    from app.domains.prd_watch.ui import is_expired_error

    assert is_expired_error('{"status":401,"err":"Token has expired"}')
    assert not is_expired_error("想折腾别的错误")


def test_rate_limit_is_not_reported_as_expired() -> None:
    """限流要单独报：处理动作是「等」而不是「换 token」。"""
    from app.domains.prd_watch.ui import is_expired_error, is_rate_limited

    body = '{"status":429,"err":"Rate limit exceeded"}'
    assert is_rate_limited(body) and not is_expired_error(body)
