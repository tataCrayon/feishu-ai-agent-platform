"""approval_link 单测：短链探测解析、关联梯子、form/附件解析（全 mock，无网络）。"""
from __future__ import annotations

import asyncio
import json

from app.domains.webportal import approval_link as al


def _run(coro):
    return asyncio.run(coro)


# ── 提取 ─────────────────────────────────────────────────────────

def test_extract_codes_dedupe_and_cap():
    text = ("a https://go.feishu.cn/approval/s/aaa111/ b "
            "https://go.feishu.cn/approval/s/bbb222/ "
            "https://go.feishu.cn/approval/s/aaa111/ "
            "https://go.feishu.cn/approval/s/ccc333/ "
            "https://go.feishu.cn/approval/s/ddd444/")
    assert al.extract_approval_codes(text) == ["aaa111", "bbb222", "ccc333"]


def test_extract_flow_numbers_case_insensitive():
    assert al.extract_flow_numbers("处理 data2026100401 和 DATA2026100402") == [
        "DATA2026100401", "DATA2026100402"]
    assert al.extract_flow_numbers("没有编号") == []


def test_extract_applink_instance_ids():
    # 实况 URL（2026-10-08 审批卡片「可查看」形态）：path 参数整体 URL 编码，
    # instanceId 藏在 instanceId%3D<数字> 里
    url = ("https://applink.feishu.cn/client/mini_program/open?appId=cli_example00000001"
           "&mode=appCenter&path=pc%2Fpages%2Fin-process%2Findex%3FauthId%3D7694218108446248183"
           "%26enableTrusteeship%3Dtrue%26instanceId%3D7690853688118758607"
           "%26source%3Dapproval_bot&relaunch=true)")
    assert al.extract_applink_instance_ids(url) == ["7690853688118758607"]
    # markdown 链接形态（右括号截断）
    md = f"[审批-星路-数据提取/修改申请]({url}) 处理一下这个"
    assert al.extract_applink_instance_ids(md) == ["7690853688118758607"]
    # 未编码形态也要认
    plain = "https://applink.feishu.cn/client/mini_program/open?path=x&instanceId=1234567890123"
    assert al.extract_applink_instance_ids(plain) == ["1234567890123"]
    # 无 instanceId / 非 applink → 空
    assert al.extract_applink_instance_ids("https://go.feishu.cn/approval/s/abc/") == []
    # 去重保序、上限 3
    multi = f"{url} {url} {plain} {plain.replace('1234567890123', '9999999999999')}"
    assert al.extract_applink_instance_ids(multi) == [
        "7690853688118758607", "1234567890123", "9999999999999"]


def test_build_guidance_is_specific():
    out = al.build_guidance(link_kind="applink 深链",
                            instance_id="7690853688118758607", candidates=13)
    assert "applink 深链" in out
    assert "7690853688118758607" in out
    assert "13 条" in out           # 说清比对范围
    assert "流程编号" in out        # 最强线索在引导里
    assert "换个说法" not in out     # 不许是无信息的「换个说法再试」
    # 无线索时也给最小可用引导
    bare = al.build_guidance()
    assert "流程编号" in bare and "附件" in bare


# ── 短链探测（mock httpx）────────────────────────────────────────

class _FakeResp:
    def __init__(self, status: int, location: str = ""):
        self.status_code = status
        self.headers = {"location": location} if location else {}


class _FakeClient:
    """go → ShortToLong → 200 终点的固定链。"""
    def __init__(self, *args, **kwargs):
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self._n += 1
        if self._n == 1:
            return _FakeResp(302, "https://oa.feishu.cn/x/ShortToLong"
                                  "?InstanceID=1234567890123&UserID=9876543210123")
        return _FakeResp(200)


def test_probe_short_link_parses_ids(monkeypatch):
    monkeypatch.setattr(al.httpx, "AsyncClient", _FakeClient)
    out = _run(al.probe_short_link("abc123"))
    assert out["instance_id"] == "1234567890123"
    assert out["user_id"] == "9876543210123"
    assert out["code"] == "abc123"


def test_probe_short_link_never_raises(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("net down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(al.httpx, "AsyncClient", _Boom)
    out = _run(al.probe_short_link("abc123"))
    assert out["instance_id"] == ""


# ── 关联梯子 ─────────────────────────────────────────────────────

def _cand(code="U-1", task_id="100", topic="2", sums=None, name="数据修改审批"):
    return {"instance_code": code, "task_id": task_id, "topic": topic,
            "definition_name": name, "title": name, "initiator_name": "张三",
            "status": 1,
            "summaries": sums if sums is not None else
            [{"key": "流程编号", "value": "DATA2026100401"}]}


def test_locate_l0_flow_number_exact():
    cands = [_cand("U-1", "100", "2", [{"key": "流程编号", "value": "DATA2026100401"}]),
             _cand("U-2", "101", "2", [{"key": "流程编号", "value": "DATA2026100402"}])]
    r = al.locate_instance({}, "帮我处理 DATA2026100402 的工单", cands)
    assert r.status == "matched" and r.candidate["instance_code"] == "U-2"
    assert "流程编号" in r.reason and r.level == "L0"


def test_locate_l1_probe_id_equals_task_id():
    cands = [_cand("U-1", "1234567890123"), _cand("U-2", "200")]
    r = al.locate_instance({"instance_id": "1234567890123"}, "审批链接", cands)
    assert r.status == "matched" and r.candidate["instance_code"] == "U-1"
    assert r.level == "L1"


def test_locate_l2_unique_live_candidate():
    cands = [_cand("U-1", "100", "1"), _cand("U-2", "101", "2")]
    r = al.locate_instance({}, "处理这个审批", cands)
    assert r.status == "matched" and r.candidate["instance_code"] == "U-1"
    assert r.level == "L2"


def test_locate_l3_ambiguous_lists_top5():
    cands = [_cand(f"U-{i}", str(100 + i), "2") for i in range(8)]
    r = al.locate_instance({}, "看看这个审批", cands)
    assert r.status == "ambiguous" and len(r.candidates) == 5
    assert r.level == "L3"


def test_locate_none_when_empty():
    r = al.locate_instance({}, "审批", [])
    assert r.status == "none" and r.level == "L3"


# ── instanceId 兜底定位（mock fetch_instance_raw）─────────────────

def _raw_with_tasks(*task_ids: str) -> dict:
    return {"tasks": [{"id": t} for t in task_ids], "form": "[]"}


def test_resolve_by_instance_ids_exact_task_id(monkeypatch):
    """task_id 等值是精确键，不该去拉详情。"""
    async def boom(*a, **k):
        raise AssertionError("不应拉详情")

    monkeypatch.setattr(al, "fetch_instance_raw", boom)
    cands = [_cand("U-1", "7690853688118758607"), _cand("U-2", "101")]
    r = _run(al.resolve_by_instance_ids(["7690853688118758607"], cands))
    assert r.status == "matched" and r.candidate["instance_code"] == "U-1"
    assert r.level == "L1"


def test_resolve_by_instance_ids_ms_alignment(monkeypatch):
    """task_id 不等值时按「实例 ID 创建毫秒 ↔ 任务 ID 毫秒」对齐（±1）。"""
    detail = {
        "U-1": _raw_with_tasks("7680000000000000000"),
        # 与 instanceId 7690853688118758607 同毫秒（>>22 相等）的任务
        "U-2": _raw_with_tasks("7690853688118955215"),
        "U-3": _raw_with_tasks("7681111111111111111"),
    }

    async def fake(code, env_extra=None):
        return detail.get(code)

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    cands = [_cand("U-1", "101"), _cand("U-2", "102"), _cand("U-3", "103")]
    r = _run(al.resolve_by_instance_ids(["7690853688118758607"], cands))
    assert r.status == "matched" and r.candidate["instance_code"] == "U-2"
    assert "毫秒" in r.reason and r.level == "L1"


def test_resolve_by_instance_ids_unique_only(monkeypatch):
    """毫秒对齐命中多条 → ambiguous 交列选，不猜单。"""
    same_bucket = ["7690853688118955215", "7690853688118758607"]

    async def fake(code, env_extra=None):
        return _raw_with_tasks(*same_bucket)

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    cands = [_cand("U-1", "101"), _cand("U-2", "102")]
    r = _run(al.resolve_by_instance_ids(["7690853688118758607"], cands))
    assert r.status == "ambiguous" and len(r.candidates) == 2


def test_resolve_by_instance_ids_none_when_no_hit(monkeypatch):
    async def fake(code, env_extra=None):
        return _raw_with_tasks("7680000000000000000")

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    cands = [_cand("U-1", "101")]
    assert _run(al.resolve_by_instance_ids(["7690853688118758607"], cands)) is None


def test_resolve_by_instance_ids_env_passthrough(monkeypatch):
    """env_extra（per-sender 身份）必须透传到详情拉取。"""
    seen: dict = {}

    async def fake(code, env_extra=None):
        seen["env"] = env_extra
        return _raw_with_tasks("7690853688118955215")

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    env = {"LARKSUITE_CLI_USER_ACCESS_TOKEN": "T"}
    cands = [_cand("U-1", "101")]
    r = _run(al.resolve_by_instance_ids(["7690853688118758607"], cands, env_extra=env))
    assert r.status == "matched" and seen["env"] == env


def test_resolve_by_instance_ids_scans_whole_pool_by_default(monkeypatch):
    """默认扫全池：真实目标常落在「前 N 条」之外，截断等于把它藏起来。

    实证（2026-11-06）：短链 iXHU4TyL / iXb8g4HB 在 limit=15 时返回未命中，
    全池才命中——候选池按 topic 排序，命中的那张排在后面。
    """
    target = "7691940269697355038"   # 与 instanceId 7691940269697322270 同毫秒
    scanned: list[str] = []

    async def fake(code, env_extra=None):
        scanned.append(code)
        if code == "U-0":        # 排序后落在候选池末尾
            return _raw_with_tasks(target)
        return _raw_with_tasks("7680000000000000000")

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    cands = [_cand(f"U-{i}", str(1000 + i), "1") for i in range(30)]
    r = _run(al.resolve_by_instance_ids(["7691940269697322270"], cands))
    assert r is not None and r.status == "matched"
    assert r.candidate["instance_code"] == "U-0"
    assert len(scanned) == 30      # 不截断


def test_resolve_by_instance_ids_limit_still_honoured(monkeypatch):
    """显式 limit 仍然生效（给调用方留收窄口子）。"""
    scanned: list[str] = []

    async def fake(code, env_extra=None):
        scanned.append(code)
        return _raw_with_tasks("7680000000000000000")

    monkeypatch.setattr(al, "fetch_instance_raw", fake)
    cands = [_cand(f"U-{i}", str(1000 + i), "1") for i in range(30)]
    assert _run(al.resolve_by_instance_ids(
        ["7691940269697322270"], cands, limit=5)) is None
    assert len(scanned) == 5


# ── form / 附件解析（mock _lark）────────────────────────────────

_DETAIL = {"ok": True, "data": {"data": {
    "instance_code": "U-1", "definition_name": "星路-数据提取/修改申请",
    "status": "PENDING", "serial_number": "DATA2026100401", "start_time": "1790000000000",
    "form": json.dumps([
        {"id": "w1", "name": "流程编号", "type": "serialNumber", "value": "DATA2026100401"},
        {"id": "w2", "name": "类型", "type": "radioV2", "value": "k1",
         "option": {"key": "k1", "text": "数据修改"}},
        {"id": "w3", "name": "详情", "type": "textarea", "value": "改邮箱 " + "x" * 400},
        {"id": "w4", "name": "附件", "type": "attachmentV2",
         "value": "https://files.example/a.pdf,https://files.example/b.png",
         "ext": "'工单描述.pdf', '截图.png'"},
    ]),
}}}


def test_fetch_instance_form_parses_widgets(monkeypatch):
    async def fake_lark(*args, **kwargs):
        return _DETAIL

    monkeypatch.setattr(al, "_lark", fake_lark)
    det = _run(al.fetch_instance_form("U-1"))
    assert det["meta"]["serial_number"] == "DATA2026100401"
    assert det["meta"]["status"] == "审批中"
    assert "类型: 数据修改" in det["text"]
    assert "详情: 改邮箱" in det["text"] and len(det["text"]) < 500  # 值截断
    assert [a["name"] for a in det["attachments"]] == ["工单描述.pdf", "截图.png"]
    assert det["attachments"][0]["url"].startswith("https://files.example/")


def test_fetch_instance_form_passes_env_extra(monkeypatch):
    """env_extra 必须透传到 _lark（per-sender 身份读取的通道）。"""
    seen: dict = {}

    async def fake_lark(*args, **kwargs):
        seen["kwargs"] = kwargs
        return _DETAIL

    monkeypatch.setattr(al, "_lark", fake_lark)
    env = {"LARKSUITE_CLI_USER_ACCESS_TOKEN": "T", "LARKSUITE_CLI_APP_ID": "cli_x"}
    _run(al.fetch_instance_form("U-1", env_extra=env))
    assert seen["kwargs"].get("env_extra") == env


def test_lark_env_extra_merges_into_subprocess_env(monkeypatch):
    """_lark(env_extra) → 子进程 env 合并注入且保留基础环境；无注入 → None 继承。"""
    captured: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b'{"ok": true}', b"")

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(al.asyncio, "create_subprocess_exec", fake_exec)
    out = _run(al._lark("whoami", env_extra={
        "LARKSUITE_CLI_USER_ACCESS_TOKEN": "T"}))
    assert out.get("ok") is True
    env = captured["env"]
    assert env["LARKSUITE_CLI_USER_ACCESS_TOKEN"] == "T"
    assert "PATH" in env  # 基础环境保留
    _run(al._lark("whoami"))
    assert captured["env"] is None  # 无注入 → 继承父进程（keychain 路径）


def test_widget_value_and_attachments_units():
    assert al._widget_value_text({"type": "radioV2", "value": "k",
                                  "option": {"text": "数据修改"}}) == "数据修改"
    atts = al._widget_attachments({"type": "image", "value": "https://x/i.png",
                                   "ext": "'截图.png'"})
    assert atts == [{"name": "截图.png", "url": "https://x/i.png"}]


def test_format_approval_content_marks_locator_and_files():
    out = al.format_approval_content(
        "流程编号: DATA2026100401", {"definition_name": "星路", "status": "审批中",
                                     "serial_number": "DATA2026100401"},
        [{"filename": "a.pdf"}], located_by="流程编号精确匹配")
    assert "流程编号精确匹配" in out
    assert "DATA2026100401" in out
    assert "附件已随工单携带" in out and "a.pdf" in out
