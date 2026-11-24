"""进程内通道两级时限（CrewOrchestrator._inproc_with_deadline）行为验证。

不跑完整 LangGraph，直接对协程打桩——覆盖四条真实中断面：
1. 快速完成 → 原样返回，不转后台
2. 超过同步窗口 → 立即让出并发额度返回 LATE_DELIVERY，任务本体继续跑并直投结果
3. 超过硬上限 → 取消任务 + 诚实失败回执（不再永久占槽）
4. _agent_hard_timeout 不被过低的声明值砍掉

跑法：python tests/orchestrator/test_inproc_deadline.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Windows 控制台默认 cp936，✅/中文断言消息直接 print 会 UnicodeEncodeError
# （同 ai_consultant 的处理，独立可跑脚本必须自带）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 直接运行脚本时 sys.path[0] 是本目录（app/orchestrator/），仓库根不在路径上，
# 下面那行 `from app.orchestrator import ...` 会 ModuleNotFoundError。
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.orchestrator import crew as crew_mod  # noqa: E402  (sys.path 引导必须先跑)

SYNC_BAK = crew_mod._INPROC_SYNC_WAIT_SEC


def _patch(obj: object, **attrs: object) -> None:
    """给实例打桩（覆写方法/私有属性）。

    走循环里的 setattr 而不是逐行赋值：mypy 会把「实例属性 = 方法」判成
    赋值类型冲突，而测试就是要替换它们；键非字面量也不触发 ruff B010。
    """
    for key, value in attrs.items():
        setattr(obj, key, value)



STATE = {"chat_id": "oc_test", "session_key": "s1", "sender_id": "ou_1",
         "sender_name": "t", "message_obj": None}


def _make_crew(delivered: list[str]) -> crew_mod.CrewOrchestrator:
    """最小 CrewOrchestrator：绕开 __init__（依赖 registry/llm），只挂所需属性。"""
    c = crew_mod.CrewOrchestrator.__new__(crew_mod.CrewOrchestrator)
    _patch(c, _registry=None, _display_name=lambda name: name)

    async def _send(chat_id: str, text: str) -> None:
        delivered.append(f"{chat_id}|{text}")

    crew_mod.set_late_delivery_sender(_send)
    return c


async def case_fast() -> None:
    delivered: list[str] = []
    c = _make_crew(delivered)

    async def quick() -> tuple[str, str | None]:
        return "秒回", None

    resp, err = await c._inproc_with_deadline("daily-agent", quick(), STATE)
    assert resp == "秒回" and err is None, (resp, err)
    assert delivered == [], delivered
    print("✅ 快任务：原样返回，不转后台")


async def case_late() -> None:
    delivered: list[str] = []
    c = _make_crew(delivered)
    crew_mod._INPROC_SYNC_WAIT_SEC = 0.05
    _patch(c, _agent_hard_timeout=lambda name: 5)
    try:
        async def slow() -> tuple[str, str | None]:
            await asyncio.sleep(0.15)
            return "迟到但成功的结果", None

        resp, err = await c._inproc_with_deadline("webportal-agent", slow(), STATE)
        assert resp == crew_mod.LATE_DELIVERY and err is None, (resp, err)
        assert delivered == []  # 已让出额度，结果还没到
        await asyncio.sleep(0.4)
        assert len(delivered) == 1, delivered
        assert "迟到但成功的结果" in delivered[0], delivered
        assert "✅" in delivered[0] and delivered[0].startswith("oc_test"), delivered
        print("✅ 慢任务：立即释放并发额度 + 真结果稍后直投原会话")
    finally:
        crew_mod._INPROC_SYNC_WAIT_SEC = SYNC_BAK


async def case_hard_timeout() -> None:
    delivered: list[str] = []
    c = _make_crew(delivered)
    crew_mod._INPROC_SYNC_WAIT_SEC = 0.05
    _patch(c, _agent_hard_timeout=lambda name: 1)
    try:
        async def hang() -> tuple[str, str | None]:
            await asyncio.sleep(30)
            return "永远到不了", None

        resp, _ = await c._inproc_with_deadline("webportal-agent", hang(), STATE)
        assert resp == crew_mod.LATE_DELIVERY
        await asyncio.sleep(1.2)
        assert len(delivered) == 1, delivered
        assert "⚠️" in delivered[0] and "超过" in delivered[0], delivered
        print("✅ 挂死任务：到硬上限即取消 + 诚实失败回执（不永久占槽）")
    finally:
        crew_mod._INPROC_SYNC_WAIT_SEC = SYNC_BAK
        crew_mod.set_late_delivery_sender(None)


def case_hard_floor() -> None:
    c = crew_mod.CrewOrchestrator.__new__(crew_mod.CrewOrchestrator)
    floor = int(SYNC_BAK) + 30
    _patch(c, _registry=types.SimpleNamespace(get=lambda n: types.SimpleNamespace(timeout_sec=60)))
    assert c._agent_hard_timeout("daily-agent") == floor, c._agent_hard_timeout("daily-agent")
    _patch(c, _registry=types.SimpleNamespace(get=lambda n: types.SimpleNamespace(timeout_sec=720)))
    assert c._agent_hard_timeout("webportal-agent") == 720
    _patch(c, _registry=types.SimpleNamespace(get=lambda n: None))
    assert c._agent_hard_timeout("unknown") == floor
    print(f"✅ 硬上限：过低声明(60s)抬到地板 {floor}s，正常声明(720s)照用")


async def main() -> int:
    await case_fast()
    await case_late()
    await case_hard_timeout()
    case_hard_floor()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
