"""per-sender 飞书授权状态机 — Device Flow + 内存 token + lark-cli env 注入。

背景：approval instances/tasks 只支持 user 身份（--as bot 被拒）。bot 的固定
登录身份（keychain）只能读自己可见的审批；群里其他人 @bot 丢审批链接时，
需要「以发送者身份」读取——发送者无权看、bot 无权看，双双定位失败。

身份选择机制（keychain/profile）在 lark-cli 侧不可多开（profile 要求唯一
app-id，同 app 复用被拒），故绕开 lark-cli 的凭据层：

- 授权：直接 HTTP Device Flow
  accounts.feishu.cn/oauth/v1/device_authorization 发起
  → open.feishu.cn/open-apis/authen/v2/oauth/token 按需轮询（消息到达时一次，
  无后台任务：点完授权重发消息即续上）。全程不碰 keychain、不切 profile。
- 存储：token 按真实 open_id 存进程内存（仓库安全约定：凭据只进内存/环境
  变量，绝不落文件）。access 过期走 refresh_token；设备码 10 分钟过期。
- 使用：LARKSUITE_CLI_USER_ACCESS_TOKEN + LARKSUITE_CLI_APP_ID 环境变量按次
  注入 lark-cli 子进程（实测 env 注入优先于 keychain，可按人选择身份）。
- 绑定：完成后 whoami 取真实授权者 open_id 入库——他人误点链接不会错绑到
  发送者名下（点错人 → wrong_account → 让发送者自己再授权一次）。

安全：device_code / token 不打日志（只记 open_id 与状态）。
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import CONFIG_TOML
from app.domains.webportal.approval_link import _lark

logger = logging.getLogger("webportal.user_auth")

# 审批只读 + 身份回读（whoami 绑定授权者）——最小面
_SCOPE = (
    "approval:approval:read approval:instance:read "
    "approval:task:read auth:user.id:read"
)
_DEVICE_URL = "https://accounts.feishu.cn/oauth/v1/device_authorization"
_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_SESSION_TTL = 600.0          # 设备码有效期（飞书下发 expires_in=600）
_FIXED_TTL = 600.0            # 固定登录身份缓存


@dataclass
class _Session:
    device_code: str
    verification_url: str
    user_code: str
    expires_at: float


@dataclass
class _Token:
    access: str
    refresh: str
    expires_at: float
    refresh_expires_at: float | None = None


# ── 内存态（进程生命周期；重启即清，重新授权）────────────────────
_sessions: dict[str, _Session] = {}
_tokens: dict[str, _Token] = {}
_fixed: tuple[float, str | None] = (0.0, None)
_app_cfg: dict[str, str] = {}


def session_ids(session_key: str) -> tuple[str, str]:
    """`feishu:{chat_id}:{sender_open_id}` → (sender_open_id, chat_id)。

    解析不了（非飞书格式/缺段）→ ("", "")：调用方退回固定身份，不触发授权。
    """
    parts = (session_key or "").split(":")
    if len(parts) == 3 and parts[0] == "feishu":
        return parts[2], parts[1]
    return "", ""


def _cfg() -> dict[str, str]:
    if not _app_cfg:
        try:
            with open(CONFIG_TOML, "rb") as f:
                data = tomllib.load(f)
            pf = data["projects"][0]["platforms"][0]["options"]
            _app_cfg["app_id"] = str(pf.get("app_id") or "")
            _app_cfg["app_secret"] = str(pf.get("app_secret") or "")
        except Exception as e:
            logger.warning("read feishu app config failed: %s", e)
    return _app_cfg


def _app_id() -> str:
    return os.getenv("FEISHU_APP_ID") or _cfg().get("app_id", "")


def _app_secret() -> str:
    # 与 config.FeishuConfig 同源：.env 覆盖 config.toml
    return os.getenv("FEISHU_APP_SECRET") or _cfg().get("app_secret", "")


async def _post(url: str, payload: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        logger.warning("POST %s failed: %s", url.rsplit("/", 1)[-1], e)
        return 0, {}
    try:
        body = resp.json()
    except Exception:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _env_raw(access: str) -> dict[str, str]:
    return {
        "LARKSUITE_CLI_USER_ACCESS_TOKEN": access,
        "LARKSUITE_CLI_APP_ID": _app_id(),
    }


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def who_open_id(access: str) -> str | None:
    """whoami（带 env token）→ 真实授权者 open_id；失败 None。"""
    data = await _lark("whoami", "--as", "user", env_extra=_env_raw(access))
    ob = data.get("onBehalfOf") or {}
    if ob.get("openId"):
        return str(ob["openId"])
    ident = (data.get("identities") or {}).get("user") or {}
    if ident.get("openId"):
        return str(ident["openId"])
    return None


async def fixed_open_id() -> str | None:
    """bot 固定登录身份（keychain）的 open_id，600s 缓存；失败 None。"""
    global _fixed
    now = time.time()
    if _fixed[0] > now:
        return _fixed[1]
    data = await _lark("auth", "status", "--json")
    ident = (data.get("identities") or {}).get("user") or {}
    oid = str(ident.get("openId") or "") or None
    _fixed = (now + _FIXED_TTL, oid)
    if oid:
        logger.info("fixed identity open_id=%s", oid[:12])
    else:
        logger.warning("fixed identity unknown: %s",
                       str((data.get("error") or {}).get("message", ""))[:80])
    return oid


def token_env_sync(open_id: str) -> dict[str, str] | None:
    """已存且未过期 → lark-cli env；过期 → None（refresh 交给 resolve 异步做）。"""
    tok = _tokens.get(open_id)
    if tok and tok.expires_at > time.time() + 60:
        return _env_raw(tok.access)
    return None


def session_url(open_id: str) -> str:
    """进行中会话的授权链接（无/已过期返回空串）。"""
    sess = _sessions.get(open_id)
    if sess and time.time() <= sess.expires_at:
        return sess.verification_url
    return ""


def clear(open_id: str) -> None:
    _tokens.pop(open_id, None)
    _sessions.pop(open_id, None)


async def start_auth(open_id: str) -> tuple[str, str]:
    """发起（或复用 10 分钟内进行中的）设备授权 → (verification_url, user_code)。

    失败 → ("", "")，调用方退回旧引导文案。
    """
    sess = _sessions.get(open_id)
    now = time.time()
    if sess and now <= sess.expires_at:
        return sess.verification_url, sess.user_code
    status, body = await _post(_DEVICE_URL, {
        "client_id": _app_id(),
        "scope": _SCOPE,
        "client_secret": _app_secret(),
    })
    code = str(body.get("device_code") or "")
    if status != 200 or not code:
        logger.warning("device_authorization failed status=%s", status)
        return "", ""
    url = str(body.get("verification_uri_complete")
              or body.get("verification_uri") or "")
    user_code = str(body.get("user_code") or "")
    ttl = min(_num(body.get("expires_in")) or _SESSION_TTL, _SESSION_TTL)
    _sessions[open_id] = _Session(code, url, user_code, now + ttl)
    logger.info("auth session started open_id=%s ttl=%ds", open_id[:12], int(ttl))
    return url, user_code


async def _refresh(open_id: str, tok: _Token) -> _Token | None:
    if not tok.refresh:
        _tokens.pop(open_id, None)
        return None
    if tok.refresh_expires_at and tok.refresh_expires_at <= time.time():
        _tokens.pop(open_id, None)
        return None
    status, body = await _post(_TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh,
        "client_id": _app_id(),
        "client_secret": _app_secret(),
    })
    access = str(body.get("access_token") or body.get("accessToken") or "")
    if status != 200 or not access:
        logger.warning("refresh failed status=%s err=%s",
                       status, str(body.get("error") or "")[:60])
        _tokens.pop(open_id, None)
        return None
    now = time.time()
    exp = _num(body.get("expires_in") or body.get("expiresIn")) or 7200
    refresh = str(body.get("refresh_token") or body.get("refreshToken")
                  or tok.refresh)
    new = _Token(access, refresh, now + exp,
                 _num(body.get("refresh_expires_in")
                      or body.get("refreshExpiresIn")))
    _tokens[open_id] = new
    logger.info("token refreshed open_id=%s", open_id[:12])
    return new


async def resolve(open_id: str) -> tuple[dict[str, str] | None, str]:
    """解析发送者身份 → (lark-cli env 或 None, 状态)。

    状态：ok / no_session（无 token 无会话）/ pending（授权进行中）/
    expired（设备码过期，会话已清）/ wrong_account（授权者≠发送者，token 已
    按真实授权者入库）/ error（网络或响应异常）。
    按需轮询：仅存在进行中会话时打一次 token 端点（无后台任务）。
    """
    if not open_id:
        return None, "no_session"
    tok = _tokens.get(open_id)
    if tok is not None:
        if tok.expires_at > time.time() + 60:
            return _env_raw(tok.access), "ok"
        refreshed = await _refresh(open_id, tok)
        if refreshed is not None:
            return _env_raw(refreshed.access), "ok"
        # refresh 失败已清 token → 继续看有没有设备授权会话

    sess = _sessions.get(open_id)
    now = time.time()
    if sess is None:
        return None, "no_session"
    if now > sess.expires_at:
        _sessions.pop(open_id, None)
        logger.info("auth session expired open_id=%s", open_id[:12])
        return None, "expired"

    status, body = await _post(_TOKEN_URL, {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": sess.device_code,
        "client_id": _app_id(),
        "client_secret": _app_secret(),
    })
    access = str(body.get("access_token") or body.get("accessToken") or "")
    if status == 200 and access:
        owner = await who_open_id(access) or open_id
        exp = _num(body.get("expires_in") or body.get("expiresIn")) or 7200
        refresh = str(body.get("refresh_token") or body.get("refreshToken") or "")
        _tokens[owner] = _Token(access, refresh, now + exp,
                                _num(body.get("refresh_expires_in")
                                     or body.get("refreshExpiresIn")))
        _sessions.pop(open_id, None)
        logger.info("authorized open_id=%s owner=%s",
                    open_id[:12], owner[:12])
        if owner != open_id:
            return None, "wrong_account"
        return _env_raw(access), "ok"

    err = str(body.get("error") or "")
    if err in ("authorization_pending", "slow_down"):
        return None, "pending"
    if "expired" in err or "denied" in err:
        _sessions.pop(open_id, None)
        logger.info("device code %s open_id=%s", err, open_id[:12])
        return None, "expired"
    logger.warning("token poll failed status=%s err=%s",
                   status, err[:60] or "empty")
    return None, "error"
