"""feishu_case_mirror：目录索引维护与幂等重同步口径（纯函数，不起 lark-cli）。"""
from __future__ import annotations

from app.config import CaseMirrorConfig
from app.domains.webportal.feishu_case_mirror import (
    CaseMirrorSync,
    apply_index_update,
    source_label,
)

INDEX_MD = """# 案例知识库

Agent 内置案例的飞书镜像，每个子文档与Agent内置 cases/*.md 内容一致。

| 案例 | 状态 | 来源 |
|-|-|-|
| [更新BE后台用户登录邮箱](https://x.feishu.cn/wiki/aaa) | verified | CMDB 工单 |
| [查询欢迎邮件发送情况](https://x.feishu.cn/wiki/bbb) | verified | 飞书操作手册 |

共 2 个案例（verified 2 / draft 0）。
"""


def test_source_label_mapping():
    assert source_label("cmdb") == "CMDB 工单"
    assert source_label("feishu") == "飞书操作手册"
    assert source_label("other") == "other"


def test_insert_row_at_end_of_matching_group():
    out = apply_index_update(INDEX_MD, title="新CMDB案例",
                             node_url="https://x.feishu.cn/wiki/ccc",
                             status="draft", source_label="CMDB 工单")
    lines = out.splitlines()
    # CMDB 组末尾（在飞书操作手册那行之前）
    assert lines[-4].startswith("| [新CMDB案例]")
    assert "https://x.feishu.cn/wiki/ccc" in lines[-4]
    assert lines[-3].startswith("| [查询欢迎邮件发送情况]")
    assert "共 3 个案例（verified 2 / draft 1）。" in out


def test_insert_row_in_feishu_group_at_end():
    out = apply_index_update(INDEX_MD, title="新手册案例",
                             node_url="https://x.feishu.cn/wiki/ccc",
                             status="draft", source_label="飞书操作手册")
    lines = out.splitlines()
    assert lines[-3].startswith("| [新手册案例]")          # 飞书组末尾
    assert lines[-4].startswith("| [查询欢迎邮件发送情况]")  # 原飞书组行仍在前
    assert "共 3 个案例（verified 2 / draft 1）。" in out


def test_same_url_updates_instead_of_duplicating():
    out = apply_index_update(INDEX_MD, title="更新BE后台用户登录邮箱",
                             node_url="https://x.feishu.cn/wiki/aaa",
                             status="draft", source_label="CMDB 工单")
    assert out.count("https://x.feishu.cn/wiki/aaa") == 1
    assert "| [更新BE后台用户登录邮箱](https://x.feishu.cn/wiki/aaa) | draft | CMDB 工单 |" in out
    assert "共 2 个案例（verified 1 / draft 1）。" in out


def test_stats_recount_when_no_stats_line():
    raw = INDEX_MD.split("共 2 个案例")[0]
    out = apply_index_update(raw, title="x", node_url="https://x.feishu.cn/wiki/ccc",
                             status="draft", source_label="CMDB 工单")
    assert "共 3 个案例（verified 2 / draft 1）。" in out


def test_find_node_token_by_title():
    from app.domains.webportal.feishu_case_mirror import _find_node_token
    assert _find_node_token(INDEX_MD, "更新BE后台用户登录邮箱") == "aaa"
    assert _find_node_token(INDEX_MD, "不存在的案例") == ""


def test_config_error_reports_missing_tokens():
    sync = CaseMirrorSync(cfg=CaseMirrorConfig(enabled=True))
    assert "缺 space_id" in sync.config_error()
    sync = CaseMirrorSync(cfg=CaseMirrorConfig(enabled=True, space_id="s"))
    assert "parent_node_token" in sync.config_error()
    sync = CaseMirrorSync(cfg=CaseMirrorConfig(enabled=False, space_id="s"))
    assert "未启用" in sync.config_error()
    sync = CaseMirrorSync(cfg=CaseMirrorConfig(
        enabled=True, space_id="s", parent_node_token="p", index_doc_token="i"))
    assert sync.config_error() == ""
