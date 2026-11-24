"""绑定解析单测 — Meegle Story + PRD 链接 → StoryBinding（fake 三方，零网络）。

跑法：python -m pytest tests/domains/prd_watch/test_binding.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.domains.prd_watch.binding import BindingSource, doc_url_kind
from app.domains.prd_watch.meegle import MeegleError, Story

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_S1 = Story(story_id="7154141591", title="BE1.0-高危地区识别", status="4-待评审",
            prd_url="https://example.feishu.cn/wiki/AOhYwFrGYikgqMkYt36cPOZVnpW",
            owners={"产品": "黄靖瑶", "测试": "张三", "渠道": "李四"},
            owner_emails={"产品": "a@x.com", "渠道": "z@x.com"})
_S2 = Story(story_id="7153434645", title="ESOP-多公司切换", status="2-需求中",
            prd_url="")   # 需求文档字段被填成了正文摘要（实测存在）
# 列表接口（MQL）不返回 role_members —— 真实形态就是只有字段值，靠详情补人
_S_LIST_ONLY = Story(story_id="7154148035", title="企业用户取现筛选优化",
                     status="4-待评审",
                     prd_url="https://example.feishu.cn/wiki/wikcnExampleToken0000000000")
_S_DETAIL = Story(story_id="7154148035", title="企业用户取现筛选优化", status="4-待评审",
                  prd_url="https://example.feishu.cn/wiki/wikcnExampleToken0000000000",
                  owners={"产品": "王五", "需求提出人": "刘丹"},
                  owner_emails={"产品": "r@x.com"})


class _FakeMeegle:
    def __init__(self, stories: list[Story], error: Exception | None = None,
                 detail: dict[str, Story] | None = None) -> None:
        self.stories = stories
        self.error = error
        # 详情接口能查到、但不在「最近更新列表」里的 Story（一场会的多个 Story
        # 常有排期更早的那几个，靠 ID 直取补齐）
        self.detail = detail or {}
        self.brief_calls: list[str] = []

    def list_stories(self, limit: int = 30, status: str = "", **_kw: object) -> list[Story]:
        if self.error:
            raise self.error
        return self.stories

    def get_story(self, story_id: str, **_kw: object) -> Story:
        """参与人补齐与「列表外直取」都走详情接口（MQL 列表不返回 role_members）。"""
        if self.error:
            raise self.error
        self.brief_calls.append(story_id)
        for story in self.stories:
            if story.story_id == story_id:
                return story
        if story_id in self.detail:
            return self.detail[story_id]
        raise MeegleError(f"Story {story_id} 不存在")


class _FakeLark:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def inspect_url(self, url: str, **_kw: object) -> dict[str, str]:
        self.calls.append(url)
        if self.fail:
            raise RuntimeError("permission_denied 131006")
        return {"obj_token": "docxObj1", "type": "docx", "title": "高危地区识别",
                "token": "docxObj1", "node_token": "AOhYwFrGYikg"}


def _source(stories: list[Story], mapping: dict[str, str], *, lark: _FakeLark | None = None,
            error: Exception | None = None) -> BindingSource:
    return BindingSource(client=_FakeMeegle(stories, error),  # type: ignore[arg-type]
                         lark=lark or _FakeLark(),             # type: ignore[arg-type]
                         story_chat_map=mapping)


def test_binding_from_meegle_uses_prd_obj_token() -> None:
    src = _source([_S1], {"7154141591": "oc_g1"})
    b = asyncio.run(src.binding_for_chat("oc_g1"))
    assert b is not None
    # 关键：用 wiki URL 解析出的 obj_token，不是 node_token（后者 fetch 会报错）
    assert b.prd_token == "docxObj1"
    assert b.story_title == "BE1.0-高危地区识别" and b.source == "meegle"
    assert b.status == "4-待评审"


def test_unmapped_story_is_not_watched() -> None:
    """配了 Meegle 不等于全空间监听——只有 story_chat_map 里的才建绑定。"""
    src = _source([_S1, _S2], {"7153434645": "oc_g2"})
    assert asyncio.run(src.binding_for_chat("oc_g1")) is None
    assert asyncio.run(src.binding_for_story("7154141591")) is None


def test_story_without_doc_url_is_skipped_with_reason() -> None:
    lark = _FakeLark()
    src = _source([_S2], {"7153434645": "oc_g2"}, lark=lark)
    assert asyncio.run(src.binding_for_chat("oc_g2")) is None
    assert lark.calls == []                       # 不是链接就不该去解析
    assert any("不是飞书文档链接" in e for e in src.errors), src.errors


def test_git_tree_url_is_rejected_without_call() -> None:
    """实测有 Story 的「需求文档」填成内部 git tree 地址：不是文档，不该去打 lark-cli。"""
    story = Story(story_id="7152327347", title="基金多批次改造", status="4-待评审",
                  prd_url="http://git.example.com/flp/product_publicfund"
                         "/-/tree/main/BE1.0-BE2.0")
    lark = _FakeLark()
    src = _source([story], {"7152327347": "oc_g5"}, lark=lark)
    assert asyncio.run(src.binding_for_chat("oc_g5")) is None
    assert lark.calls == []
    assert any("不是飞书文档链接" in e for e in src.errors), src.errors


def test_trailing_note_in_prd_field_is_stripped() -> None:
    """实测字段形态「链接 + 空格 + ###第三节！」：尾注不能混进请求。

    混进去时 lark-cli 报的仍是 lacks permission，会被误判成"要授权"（假阳性）。
    """
    story = Story(story_id="7152448608", title="中台存续列表页加引擎结果",
                  status="4-待评审",
                  prd_url="https://example.feishu.cn/wiki/CxNkwgOyRig2 ###第三节！")
    lark = _FakeLark()
    src = _source([story], {"7152448608": "oc_g6"}, lark=lark)
    assert asyncio.run(src.binding_for_chat("oc_g6")) is not None
    assert lark.calls == ["https://example.feishu.cn/wiki/CxNkwgOyRig2"], lark.calls


@pytest.mark.parametrize("url,expect", [
    ("https://example.feishu.cn/wiki/AOhYwFrGYikg", "wiki"),
    ("https://x.feishu.cn/docx/Doxcn1", "docx"),
    ("https://dcmobile.larksuite.com/docs/abc", "docs"),
    ("HTTPS://FEISHU.CN/wiki/abc", "wiki"),               # 大小写不敏感
    ("", ""),
    ("背景：对外API下单时…", ""),                            # 正文摘要
    ("http://git.example.com/x/-/tree/main", ""),   # 内部 git
    ("https://evilfeishu.cn/wiki/abc", ""),                # 伪后缀，不是 *.feishu.cn
    ("https://feishu.cn.evil.com/wiki/abc", ""),          # 伪前缀
])
def test_doc_url_kind(url: str, expect: str) -> None:
    assert doc_url_kind(url) == expect, url


def test_inspect_failure_records_error_not_crash() -> None:
    src = _source([_S1], {"7154141591": "oc_g1"}, lark=_FakeLark(fail=True))
    assert asyncio.run(src.binding_for_chat("oc_g1")) is None
    assert any("解析失败" in e for e in src.errors), src.errors
    # 权限类错误必须翻译成人话：原始输出是 lark-cli 的 JSON 残片，进台账没人看得懂
    assert any("未对机器人身份开放" in e for e in src.errors), src.errors


def test_mapped_but_missing_story_reports_gap() -> None:
    """管理员配了群但列表与直取都拿不到（归档/ID 写错）必须说出来。

    比旧行为多做一步：不在「最近更新 30 条」里不再直接放弃，而是按 ID 直取——
    一场会牵涉的多个 Story 里常有排期更早的那几个，它们不在列表前 30 条。
    """
    src = _source([_S1], {"9999999999": "oc_ghost"})
    asyncio.run(src.scan())
    assert any("9999999999" in e and "取不到" in e for e in src.errors), src.errors


def test_story_missing_from_list_is_fetched_by_id() -> None:
    """配了映射但不在列表里 → 直取详情也要建绑定（一场会的多个 Story 不等都在前排）。"""
    src = _source([_S1], {"7154148035": "oc_g8"})
    src.client = _FakeMeegle([_S1], detail={  # type: ignore[assignment]
        "7154148035": Story(story_id="7154148035", title="企业用户取现筛选优化",
                            status="4-待评审",
                            prd_url="https://x.feishu.cn/wiki/Abc",
                            owners={"产品": "王五"}, space_name="示例空间",
                            url="https://project.feishu.cn/pk/story/detail/7154148035")})
    b = asyncio.run(src.binding_for_chat("oc_g8"))
    assert b is not None and b.story_id == "7154148035"
    assert b.space_name == "示例空间" and b.story_url.endswith("7154148035")


def test_meegle_error_is_surfaced_once() -> None:
    src = _source([], {"7154141591": "oc_g1"}, error=MeegleError("token 过期"))
    assert asyncio.run(src.binding_for_chat("oc_g1")) is None
    assert any("token 过期" in e for e in src.errors), src.errors


def test_owners_keeps_notify_roles_only() -> None:
    """渠道/前端等业务角色不进 @ 名单，产品/测试要进。"""
    b = asyncio.run(_source([_S1], {"7154141591": "oc_g1"}).binding_for_chat("oc_g1"))
    assert b is not None
    assert "黄靖瑶" in b.owners.values() and "张三" in b.owners.values()
    assert "李四" not in b.owners.values()


def test_people_filled_via_brief_only_for_mapped() -> None:
    """列表没给参与人时按详情补；且只补配了群映射的那条，不给全空间 1300 条都拉。"""
    fake = _FakeMeegle([_S_LIST_ONLY])
    fake_detail = _FakeMeegle([_S_DETAIL])

    def _get_story(story_id: str, **_kw: object) -> Story:
        fake.brief_calls.append(story_id)
        return fake_detail.stories[0]

    fake.get_story = _get_story                      # type: ignore[method-assign]
    src = BindingSource(client=fake, lark=_FakeLark(),  # type: ignore[arg-type]
                        story_chat_map={"7154148035": "oc_g9"})
    b = asyncio.run(src.binding_for_chat("oc_g9"))
    assert b is not None and b.owners.get("产品") == "王五"
    assert fake.brief_calls == ["7154148035"], fake.brief_calls


def test_snapshot_shape() -> None:
    src = _source([_S1], {"7154141591": "oc_g1"})
    asyncio.run(src.scan())
    snap = src.snapshot()
    assert snap["source"] == "meegle" and snap["bound_chats"] == 1
    assert snap["mapped"] == 1


def test_scan_is_cached() -> None:
    """同一进程内多次旁路只扫一次 Meegle（Story 列表 1300+ 条，每条消息都拉会拖死旁路）。"""
    lark = _FakeLark()
    src = _source([_S1], {"7154141591": "oc_g1"}, lark=lark)
    asyncio.run(src.binding_for_chat("oc_g1"))
    asyncio.run(src.binding_for_chat("oc_g1"))
    assert len(lark.calls) == 1


_HAS_MEEGLE_TOKEN = bool(os.environ.get("MEEGLE_USER_TOKEN")
                         or os.environ.get("FEISHU_MCP_TOKEN")
                         or (Path.home() / ".claude" / "feishu_token.txt").is_file())
# 空间名进了配置就不再是通用值：本机跑真连需要显式给空间，否则 examplecorp 必然 404。
_MEEGLE_SPACE = os.environ.get("MEEGLE_SPACE", "")


@pytest.mark.skipif(not (_HAS_MEEGLE_TOKEN and _MEEGLE_SPACE), reason="无飞书项目凭证或空间名，跳过真连")
def test_real_meegle_readonly() -> None:
    """只读真连：确认空间能列出 Story 并读到 PRD 链接字段。"""
    from app.domains.prd_watch.meegle import MeegleClient

    client = MeegleClient(_MEEGLE_SPACE)
    stories = client.list_stories(limit=5)
    assert stories and all(s.story_id for s in stories)
    assert any(s.prd_url.startswith("http") for s in stories), "PRD 字段口径变了，检查 prd_field"

