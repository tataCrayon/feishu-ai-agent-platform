"""多身份隔离 —— 每个 PM 用户一套 lark-cli 凭据目录，互不覆盖。

为什么需要这一层
----------------
lark-cli 的 user 身份是**机器级单例**：`auth login` 换人会把整台机器的登录态
换掉，PRD 读写、妙记、案例镜像这些依赖「张三」的旁路会跟着一起变。

`LARKSUITE_CLI_CONFIG_DIR` 提供了隔离点：指向不同目录 = 不同的用户目录
（实测：新目录 `auth list` 返回空、`config.json` 里 users 为空 = 干净身份，
原目录完全不受影响）。所以本模块只做一件事——**把「谁是当前用户」翻译成
「哪套目录 + 哪些环境变量」**，调用方（`feishu_pm_sync._run_lark_cli`）照此执行。

目录布局
--------

    ~/.lark-cli/pm/<open_id>/        # 每个 PM 用户一套，含自己的 token
    ~/.lark-cli/hermes/              # 既有机器级登录（PRD/妙记等旁路在用，不要动）

三条不变量
----------
1. **不自动建目录**：`auth_home()` 只算路径。目录由 `auth login` 自己创建——
   提前建空目录会让 lark-cli 认为「已配置但无 token」，错误信息变成误导性的
   `not configured`。
2. **不读不存在的用户**：`ensure_ready()` 校验目录里是否真有这个 open_id 的
   登录态，没登录就返回原因，让调用方如实说「还没授权」而不是发一条空消息。
3. **缺省不切换**：`open_id` 为空时返回空 env（走机器级默认身份），
   保持「未接 PM 也不影响其他功能」。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("pm_hub.identity")

# 所有 PM 用户的凭据根目录。与机器级登录的 hermes/ 平级，互不覆盖。
PM_HOME_ROOT = Path.home() / ".lark-cli" / "pm"

# lark-cli 读这个环境变量决定配置目录（实测有效；不在 CLI 的 --help 里，
# 是从二进制字符串表里找到的，改版本时要重新确认）。
CONFIG_DIR_ENV = "LARKSUITE_CLI_CONFIG_DIR"

# hermes 是「外部上下文」标记。设了它、目录里又没绑定过，lark-cli 会拒绝工作
# （错误原文：hermes context detected but lark-cli is not bound to it）。
# PM 目录是独立上下文，必须把它摘掉。
_EXTERNAL_CONTEXT_ENVS = ("HERMES_HOME", "OPENCLAW_HOME", "LARK_CHANNEL")


def auth_home(open_id: str) -> Path:
    """某个 PM 用户的 lark-cli 配置目录。不创建。"""
    key = (open_id or "").strip()
    if not key:
        raise ValueError("open_id 为空：无法定位凭据目录")
    return PM_HOME_ROOT / key


def _seed_config(home: Path) -> None:
    """给新目录铺一份最小 config.json（只含 app 凭据，users 为空 = 未登录）。

    为什么必须铺：空目录下 `auth login` 会直接报 `not configured`，
    要求先跑 `config init --new` —— 那会**新建一个应用**，不是我们想要的。
    铺一份「已有 app、还没有用户」的配置，`auth login` 就会正常发起 device flow
    （实测：铺完之后 `--no-wait` 立刻返回 verification_url）。

    appSecret 沿用机器级配置里的 keychain 引用，不复制明文——凭据仍只有一份。
    """
    from app.config import load_config

    cfg_path = home / "config.json"
    if cfg_path.exists():
        return
    home.mkdir(parents=True, exist_ok=True)
    try:
        app_id = load_config().feishu.app_id
    except Exception as e:
        logger.warning("读 app_id 失败，无法为该目录铺配置: %s", e)
        return
    cfg = {"apps": [{
        "appId": app_id,
        # keychain 引用是机器级的，指向同一个应用凭据；不在这里复制 secret 明文
        "appSecret": {"source": "keychain", "id": f"appsecret:{app_id}"},
        "brand": "feishu",
        "defaultAs": "user",
        "strictMode": "off",
        "users": [],
    }]}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已为 %s 铺好 lark-cli 配置（未登录）", home)


def user_env(open_id: str) -> Dict[str, str]:
    """调 lark-cli 时该注入的环境变量。

    `open_id` 为空 → 返回空 dict，走机器级默认身份（未接 PM 时的行为不变）。
    非空 → 指向该用户目录，并摘掉外部上下文标记。
    """
    key = (open_id or "").strip()
    if not key:
        return {}
    env = {CONFIG_DIR_ENV: str(auth_home(key))}
    for name in _EXTERNAL_CONTEXT_ENVS:
        env[name] = ""
    return env


def _clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """在 os.environ 基础上叠加 extra，并把值为空串的键**删掉**。

    为什么要删而不是留空串：hermes 那类变量判的是「存在与否」，
    留一个 `HERMES_HOME=""` 仍然算「有上下文」。删掉才是真的摘除。
    """
    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    for k, v in (extra or {}).items():
        if v == "":
            env.pop(k, None)
        else:
            env[k] = v
    return env


def run_cli(cmd: list, *, open_id: str = "", input_data: Optional[str] = None,
            timeout: int = 60) -> Dict[str, Any]:
    """按用户身份跑一条 lark-cli 命令，返回解析后的 JSON。

    这是 `feishu_pm_sync._run_lark_cli` 的底层实现：那层只负责补 cmd，
    身份与超时都在这里。

    Raises:
        RuntimeError: 超时 / 非零退出 / JSON 解析失败。调用方据此判断「没拿到数据」。
    """
    lark_bin = "lark-cli.cmd" if shutil.which("lark-cli.cmd") else "lark-cli"
    env = _clean_env(user_env(open_id))
    try:
        res = subprocess.run(
            [lark_bin] + list(cmd),
            input=input_data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            shell=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"lark-cli 超时（{timeout}s）: {' '.join(cmd[:4])}") from e
    if res.returncode != 0:
        raise RuntimeError(f"lark-cli 执行失败: {res.stderr or res.stdout}")
    try:
        return json.loads(res.stdout)
    except Exception as e:
        raise RuntimeError(f"JSON 解析失败 ({e}): {res.stdout[:300]}") from e


def start_login(open_id: str) -> Dict[str, Any]:
    """发起该用户的 device flow，返回 `verification_url` / `device_code` / `expires_in`。

    调用方（`/pm 授权`）拿 URL 生成二维码发给用户，**不能在同一个流程里阻塞轮询**
    ——用户看不到 URL 就永远不会去点。授权完成后由 `finish_login` 收尾。
    """
    key = (open_id or "").strip()
    if not key:
        raise ValueError("open_id 为空：没有可授权的对象")
    home = auth_home(key)
    _seed_config(home)

    out = run_cli(
        ["auth", "login", "--no-wait", "--json",
         # 只申请 PM 能力用得到的域：多要一个就是多一份授权风险
         "--domain", "im,sheets,base,wiki,docs,minutes,contact"],
        open_id=key, timeout=90,
    )
    url = str(out.get("verification_url") or out.get("verification_uri") or "")
    code = str(out.get("device_code") or "")
    if not (url and code):
        raise RuntimeError(f"lark-cli 没有返回授权链接: {str(out)[:200]}")
    return {
        "open_id": key,
        "verification_url": url,
        "device_code": code,
        "expires_in": int(out.get("expires_in") or 600),
    }


def finish_login(open_id: str, device_code: str,
                 timeout: int = 45) -> Tuple[bool, str]:
    """授权完成后收尾：拿 token、写进该用户自己的目录。

    返回 (ok, 人话)。不抛异常——失败原因要能原样回给用户，
    尤其是「他没点授权」这种最常见的情况。

    超时策略：`auth login --device-code` 会**轮询到用户授权为止**，
    用户没点就一直等。所以先自己对一次待办时间，再给一个短超时兜底——
    用户可能根本没扫，不能让 daemon 的应答线程陪它等满 120 秒。
    """
    key = (open_id or "").strip()
    if not (key and device_code):
        return False, "缺 open_id 或 device_code，无法完成授权"

    # 先看是不是已经授权好了。用户可能在浏览器点完、lark-cli 已经把 token 写下来，
    # 这时再跑 `--device-code` 反而会报「设备码已使用」——先判一次，幂等。
    already, who = ensure_ready(key)
    if already:
        return True, f"已登录：{who}"

    try:
        out = run_cli(["auth", "login", "--device-code", device_code],
                      open_id=key, timeout=45)
    except Exception as e:
        msg = str(e)
        # lark-cli 在非交互环境下会以 exit≠0 回一段「请确保 timeout>=600s」的说明，
        # 不是真的失败；token 可能已经落盘，再判一次才准。
        ok_now, who_now = ensure_ready(key)
        if ok_now:
            return True, f"已登录：{who_now}"
        if "超时" in msg:
            return False, ("还没检测到授权完成。扫码后在飞书里点「确认授权」，"
                           "再回我一句「授权好了」。")
        return False, f"授权没完成：{msg[:200]}"

    if not out.get("ok", True):
        ok_now, who_now = ensure_ready(key)
        if ok_now:
            return True, f"已登录：{who_now}"
        msg = str((out.get("error") or {}).get("message") or out)[:160]
        return False, f"授权没完成：{msg}"

    ok, who = ensure_ready(key)
    if ok:
        return True, f"已登录：{who}"
    return False, f"登录态写进去了但读不回来：{who}"


def wait_ready(open_id: str, timeout: float = 600.0, interval: float = 5.0) -> bool:
    """等授权完成（轮询自己的凭据目录）。

    为什么要轮询而不是只靠用户回一句话：用户在浏览器点完「确认授权」之后，
    飞书不会回调我们任何东西——不主动查，就只能等他记得回来说一声。
    查的是本地凭据目录（`whoami`），不消耗飞书接口配额。

    返回是否在超时前就绪。调用方据此决定要不要主动推一条「已经好了」。
    """
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        ok, _ = ensure_ready(open_id)
        if ok:
            return True
        _time.sleep(interval)
    return False


def ensure_ready(open_id: str) -> Tuple[bool, str]:
    """该用户的凭据是否可用。返回 (ok, 人话原因)。

    这是「能不能以他的身份读消息」的唯一判据，调用方必须看它——
    跳过它直接调 lark-cli，失败会表现为「扫回来一堆空」，
    看起来像没数据，实际是没授权。
    """
    key = (open_id or "").strip()
    if not key:
        return False, "未配置 PM 身份（[projects.pm.identity].open_id 为空）"

    home = auth_home(key)
    if not home.exists():
        return False, f"他还没授权：目录 {home} 不存在，先发 `/pm 授权` 走一次登录"

    data = whoami(key)
    if not data:
        return False, f"凭据读不到：{home} 里没有可用登录态，重跑 `/pm 授权`"

    on_behalf = data.get("onBehalfOf") or {}
    got = str(on_behalf.get("openId") or "")
    if got and got != key:
        return False, (
            f"目录里的登录身份是 {on_behalf.get('userName')}({got})，"
            f"不是配置里的 {key}——目录被别的账号占用了"
        )
    return True, f"{on_behalf.get('userName')}({got or key})"


def whoami(open_id: str) -> Dict[str, Any]:
    """该用户目录下实际生效的身份。未登录 / 目录不存在时返回 `{}`。"""
    if not (open_id or "").strip():
        return {}
    try:
        data = run_cli(["whoami"], open_id=open_id, timeout=30)
    except Exception as e:
        logger.debug("whoami 失败 open_id=%s: %s", open_id, e)
        return {}
    if not data.get("ok", True) and "onBehalfOf" not in data:
        return {}
    return data


def list_users() -> list:
    """本机已授权的 PM 用户（扫目录，不依赖网络）。"""
    if not PM_HOME_ROOT.exists():
        return []
    out = []
    for d in sorted(PM_HOME_ROOT.iterdir()):
        if not d.is_dir():
            continue
        info = whoami(d.name)
        on_behalf = info.get("onBehalfOf") or {}
        out.append({
            "open_id": d.name,
            "home": str(d),
            "user_name": on_behalf.get("userName") or "",
            # 目录存在但 whoami 空 = 建过目录没登录成功，也要显式列出
            "ready": bool(on_behalf.get("openId")),
        })
    return out
