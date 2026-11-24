"""easyprd 变更识别单测 — 打桩 LLM，只验分带与解析防御（不联网）。

跑法：python -m pytest tests/domains/prd_watch/test_extract.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

from app.config import PrdWatchConfig, RouterConfig
from app.domains.prd_watch.extract import ChangeExtractor
from app.domains.prd_watch.models import BAND_FLAGGED, BAND_NORMAL

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    async def create(self, **_kwargs: object) -> object:
        msg = types.SimpleNamespace(content=self._content)
        usage = types.SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            prompt_tokens_details=None, completion_tokens_details=None,
        )
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                    usage=usage)


def _payload(*items: dict) -> str:
    return json.dumps({"changes": list(items)}, ensure_ascii=False)


def _extractor(content: str, **cfg: object) -> ChangeExtractor:
    ex = ChangeExtractor(RouterConfig(api_key="k"), PrdWatchConfig(**cfg))  # type: ignore[arg-type]
    ex._client = types.SimpleNamespace(  # type: ignore[assignment]
        chat=types.SimpleNamespace(completions=_FakeCompletions(content)))
    return ex


def _run(ex: ChangeExtractor, text: str = "[14:00] 张三: 额度改成20万") -> tuple[list, dict]:
    # 同步包装：仓内无 pytest-asyncio，既有异步测试都走 asyncio.run（见 test_webportal_auth）
    return asyncio.run(ex.extract(text, "单笔申购上限为人民币5万元。"))


_ITEM = {"title": "申购上限 5万→20万", "quote_text": "单笔申购上限",
         "current_text": "单笔申购上限为人民币5万元。",
         "suggested_text": "单笔申购上限为人民币20万元。",
         "change_type": "参数调整", "modules": ["申购"], "compliance": "无",
         "evidence_quote": "额度改成20万", "speaker": "张三", "scenario": "群聊",
         "rationale": "VIP 客户不够用", "confidence": 0.9}


def test_normal_band() -> None:
    ex = _extractor(_payload(_ITEM))
    points, stats = _run(ex)
    assert len(points) == 1 and points[0].band == BAND_NORMAL
    assert points[0].title == "申购上限 5万→20万"
    assert stats["candidates"] == 1 and stats["dropped"] == 0


def test_dropped_below_min() -> None:
    low = dict(_ITEM, confidence=0.5)
    ex = _extractor(_payload(low))
    points, stats = _run(ex)
    assert points == [] and stats["dropped"] == 1, stats
    assert stats["candidates"] == 1  # 丢弃也要计数，否则阈值无从调


def test_flagged_band() -> None:
    mid = dict(_ITEM, confidence=0.8)
    ex = _extractor(_payload(mid), extract_min_confidence=0.7,
                    extract_flag_confidence=0.85)
    points, stats = _run(ex)
    assert points[0].band == BAND_FLAGGED and stats["flagged"] == 1


def test_empty_changes() -> None:
    ex = _extractor('{"changes": []}')
    points, stats = _run(ex, "[14:00] 张三: 中午吃什么")
    assert points == [] and stats["candidates"] == 0


def test_fenced_json_parsed() -> None:
    ex = _extractor(f"```json\n{_payload(_ITEM)}\n```")
    points, _ = _run(ex)
    assert len(points) == 1


def test_garbage_output_is_error_not_empty() -> None:
    """网关吐废话时不得伪装成「没有变更」——静默漏变更比报错难查。"""
    ex = _extractor("抱歉，我需要更多信息才能判断。")
    points, stats = _run(ex)
    assert points == [] and "非 JSON" in stats.get("error", ""), stats


def test_type_coercion() -> None:
    dirty = dict(_ITEM, modules="申购,额度", is_new_clause="false", confidence="0.95")
    ex = _extractor(_payload(dirty))
    points, _ = _run(ex)
    cp = points[0]
    assert cp.modules == ["申购", "额度"], cp.modules
    assert cp.confidence == 0.95 and cp.band == BAND_NORMAL
    assert isinstance(cp.is_new_clause, bool) and not cp.is_new_clause


def test_unknown_keys_dropped() -> None:
    item = dict(_ITEM, hallucinated_field="x", confidence=2.0)
    ex = _extractor(_payload(item))
    points, _ = _run(ex)
    assert points[0].confidence == 1.0  # 越界夹取，不进台账当真实置信度


def test_empty_input_no_call() -> None:
    ex = _extractor('{"changes": []}')
    ex._client = None  # type: ignore[assignment]  # 空输入不该走到调用
    points, stats = asyncio.run(ex.extract("   "))
    assert points == [] and stats["candidates"] == 0


def test_llm_exception_reported() -> None:
    class _Boom:
        async def create(self, **_kw: object) -> object:
            raise RuntimeError("网关 503")

    ex = _extractor("")
    ex._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Boom()))  # type: ignore[assignment]
    points, stats = _run(ex)
    assert points == [] and "503" in stats.get("error", ""), stats


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"✅ {_name}")
    print("\n全部通过")
