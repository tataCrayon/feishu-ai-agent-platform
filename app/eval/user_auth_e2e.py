"""per-sender 授权 E2E — Device Flow 全链路实测（发起 → 点击 → 轮询 → 身份读审批）。

用法（项目根）：
    python -m app.eval.user_auth_e2e [--open-id ou_xxx] [--chat-id oc_xxx] [--timeout 540]

流程：
1. start_auth 发起设备授权（HTTP 直连，不碰 lark-cli keychain）
2. 把授权链接以 bot 身份发进指定会话（默认授权测试私聊）
3. 轮询 resolve（每 5s）等待人工点击完成
4. 拿到 token 后以**该身份**读审批（LARKSUITE_CLI_* env 注入子进程），
   验证「按人选身份」通道端到端可用

安全：token/device_code 只在本进程内存，不落盘、不打印（只打印状态与数量）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.domains.webportal import user_auth as ua
from app.protocols.feishu_send import send_text

# 默认：张三（bot 固定登录身份本人）+ 授权测试私聊
_OPEN_ID = "ou_admin000000000000000000000000"
_CHAT_ID = "oc_chat0000000000000000000000"


async def main() -> int:
    p = argparse.ArgumentParser(description="per-sender 授权 E2E")
    p.add_argument("--open-id", default=_OPEN_ID)
    p.add_argument("--chat-id", default=_CHAT_ID)
    p.add_argument("--timeout", type=int, default=540,
                   help="等待点击的秒数（设备码有效期 600s）")
    args = p.parse_args()

    # 1) 发起（进行中会话会复用，不重复发设备码）
    url, user_code = await ua.start_auth(args.open_id)
    if not url:
        print("FAIL device_authorization: 空 URL（端点/凭据问题）")
        return 1
    print(f"OK device flow started, user_code={user_code}")

    # 2) 把链接发进会话（同步 urllib，秒级）
    msg = (
        "🔐 [E2E 授权测试] 请用你的飞书账号点此授权"
        "（10 分钟内有效，仅申请审批只读权限）：\n"
        f"{url}\n"
        "点击完成后脚本会自动继续验证，无需回复。"
    )
    try:
        mid = send_text(args.chat_id, msg)
    except Exception as e:
        print(f"FAIL send auth link: {e}")
        return 1
    print(f"OK auth link sent, message_id={mid}")

    # 3) 轮询等待点击
    deadline = time.time() + args.timeout
    env: dict[str, str] | None = None
    last_state = ""
    while time.time() < deadline:
        await asyncio.sleep(5)
        env, state = await ua.resolve(args.open_id)
        if state != last_state:
            print(f"poll state={state}")
            last_state = state
        if state == "ok":
            break
        if state in ("expired", "wrong_account", "error"):
            print(f"FAIL state={state}")
            return 1
    if env is None:
        print("FAIL timeout: 用户未在时限内点击授权")
        return 1

    # 4) 以该身份读审批（env 注入，不碰 keychain）
    from app.domains.webportal.approval_link import (
        fetch_instance_form,
        list_my_approvals,
    )
    cands = await list_my_approvals(env_extra=env)
    print(f"OK list_my_approvals(env) -> {len(cands)} items")
    for c in cands[:3]:
        print(f"   - topic={c['topic']} {c['definition_name']} task={c['task_id']}")
    if not cands:
        print("FAIL 候选池为空（scope 或 token 权限问题）")
        return 1

    det = await fetch_instance_form(cands[0]["instance_code"], env_extra=env)
    if det is None:
        print("FAIL fetch_instance_form(env) -> None")
        return 1
    sn = det["meta"].get("serial_number", "")
    print(f"OK fetch_instance_form(env) -> serial_number={sn} "
          f"fields={len(det['text'].splitlines())}")
    print("E2E PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
