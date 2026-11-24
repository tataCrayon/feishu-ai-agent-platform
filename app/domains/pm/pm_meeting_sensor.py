"""会议感知 —— 妙记 → 结构化行动项。

这个模块只做一件事：把李四名下的妙记拉下来，整理成「一场会、一份摘要、若干行动项」。

它**不做业务过筛**。上一版在这里硬编码了一堆判断：

    if "李四" in content or "PM" in content or "排期" in content: ...
    elif version_hint == "10.29": sop_risk = "跨团队评估卡点，影响 G5 提测"

这类规则的问题不是写得不好，是它和 pm_scope 形成了两套边界：
同一条消息，群里发一次判「不归我」，会里说一次判「归我」，两边迟早打架。
现在统一交给 pm_daily_triage 的三档去判，边界只有一个来源。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pm_hub.minutes")

from .pm_win import NO_WINDOW

# lark-cli 单次调用要起一个 node，7 秒上下。一次拉几场会要 7 + 7N 秒，
# 所以放在 18:00 过审备料里跑，不进 180 秒的感知循环。
CLI_TIMEOUT_SEC = 90

# 一段会议摘要超过这个长度，推送里只留前段。
# 摘要本身是 markdown 大纲，几千字，全塞进推送他也不会读。
SUMMARY_LIMIT = 600


@dataclass
class MeetingTodo:
    content: str
    done: bool = False


@dataclass
class MeetingDigest:
    token: str
    title: str
    link: str
    summary: str
    todos: List[MeetingTodo] = field(default_factory=list)
    started_at: str = ""      # 会里 description 带的开始时间，解析不出来就空
    held_on: str = ""         # YYYY-MM-DD，用于「是不是今天的会」

    @property
    def open_todos(self) -> List[MeetingTodo]:
        return [t for t in self.todos if not t.done]


def _run_cli_json(cmd: List[str]) -> Dict[str, Any]:
    """跑一条 lark-cli 命令并解析 JSON。

    注意 stderr：`minutes +detail` 会往 stderr 打进度行，
    returncode 仍是 0。所以不能因为 stderr 有内容就判失败。
    """
    lark_bin = "lark-cli.cmd" if shutil.which("lark-cli.cmd") else "lark-cli"
    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

    try:
        res = subprocess.run(
            [lark_bin] + cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=CLI_TIMEOUT_SEC,
            shell=True,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"lark-cli 超时（{CLI_TIMEOUT_SEC}s）: {' '.join(cmd[:2])}")

    if res.returncode != 0:
        raise RuntimeError(f"lark-cli 退出码 {res.returncode}: {(res.stderr or res.stdout)[:200]}")

    body = (res.stdout or "").strip()
    if not body:
        raise RuntimeError("lark-cli 没有输出")

    # stdout 前面偶尔会混入进度行，从第一个 { 开始截
    start = body.find("{")
    if start > 0:
        body = body[start:]
    return json.loads(body)


def fetch_recent_minutes(days: int = 1, limit: int = 10) -> List[Dict[str, Any]]:
    """拉最近 N 天我名下的妙记列表。

    用 `--owner-ids me` 而不是硬编码 open_id：token 换了、账号改了都不用动代码。
    `--start/--end` 在服务端就过滤掉历史会，省得后面自己按字符串猜日期 ——
    `display_info` 是给人看的混排文本，解析它就是个坑。
    """
    today = date.today()
    cmd = [
        "minutes", "+search",
        "--owner-ids", "me",
        "--as", "user",
        "--page-size", str(max(1, min(limit, 30))),
        "--start", (today - timedelta(days=days - 1)).strftime("%Y-%m-%d"),
        "--end", (today + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    res = _run_cli_json(cmd)
    if not res.get("ok"):
        raise RuntimeError(f"查询妙记列表失败: {str(res)[:200]}")
    return (res.get("data") or {}).get("items") or []


def fetch_minute_detail(minute_token: str) -> Dict[str, Any]:
    """拉一场会的 AI 摘要与行动项。不拉逐字稿 —— 那个几百 KB，用不上。"""
    cmd = [
        "minutes", "+detail",
        "--minute-tokens", minute_token,
        "--summary",
        "--todo",
        "--as", "user",
    ]
    res = _run_cli_json(cmd)
    if not res.get("ok"):
        raise RuntimeError(f"获取妙记详情失败: {str(res)[:200]}")
    minutes = (res.get("data") or {}).get("minutes") or []
    if not minutes:
        raise RuntimeError(f"未找到 token={minute_token} 的妙记内容")
    return minutes[0]


def _to_digest(detail: Dict[str, Any], meta: Dict[str, Any], days: int = 1) -> Optional[MeetingDigest]:
    """把原始 JSON 收成 MeetingDigest。空录音返回 None。

    「新录音」且时长 0 的这种，`+search` 也会返回，但 detail 里没有 artifacts。
    不过滤掉的话，每天会给他推几条「新录音，无内容」。
    """
    artifacts = detail.get("artifacts") or {}
    summary = (artifacts.get("summary") or "").strip()
    raw_todos = artifacts.get("todos") or []

    if not summary and not raw_todos:
        logger.debug("跳过空妙记: %s", detail.get("minute_token"))
        return None

    todos = [
        MeetingTodo(
            content=" ".join(str(t.get("content") or "").split()),
            done=bool(t.get("is_done")),
        )
        for t in raw_todos
        if str(t.get("content") or "").strip()
    ]

    meta_data = meta.get("meta_data") or {}
    link = meta_data.get("app_link") or f"https://example.feishu.cn/minutes/{detail.get('minute_token', '')}"

    return MeetingDigest(
        token=detail.get("minute_token") or meta.get("token") or "",
        title=(detail.get("title") or meta_data.get("description") or "未命名会议").split("\n")[0][:40],
        link=link,
        summary=summary[:SUMMARY_LIMIT],
        todos=todos,
        held_on=date.today().strftime("%Y-%m-%d"),
    )


def collect_today(days: int = 1) -> List[MeetingDigest]:
    """拉最近 N 天的会，整理成 digest 列表。

    任何一场失败都不影响其他场 —— 单场 detail 拿不到是常事（权限、转码中），
    不能因为一场会就让 18:00 的过审整体失败。
    """
    try:
        items = fetch_recent_minutes(days=days)
    except Exception as e:
        logger.warning("拉妙记列表失败（今天不并入会议）: %s", e)
        return []

    logger.info("妙记列表 %d 场", len(items))

    out: List[MeetingDigest] = []
    for meta in items:
        token = meta.get("token")
        if not token:
            continue
        try:
            detail = fetch_minute_detail(token)
        except Exception as e:
            logger.warning("拉妙记详情失败 %s: %s", token, e)
            continue
        digest = _to_digest(detail, meta, days)
        if digest:
            out.append(digest)

    logger.info(
        "妙记整理完成：%d 场有效，%d 条未完成行动项",
        len(out), sum(len(d.open_todos) for d in out),
    )
    return out


def render(digests: List[MeetingDigest]) -> str:
    """给人看的会议摘要。空列表返回空串。"""
    if not digests:
        return ""

    lines = [f"【今天开了 {len(digests)} 场会】"]
    for d in digests:
        lines.append("")
        lines.append(f"· {d.title}")
        opened = d.open_todos
        if opened:
            lines.append(f"  会里留下 {len(opened)} 件没做完：")
            for t in opened[:5]:
                lines.append(f"    - {t.content[:60]}")
            if len(opened) > 5:
                lines.append(f"    …另有 {len(opened) - 5} 件")
        else:
            lines.append("  没有留下行动项。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    found = collect_today(days=3)
    print(render(found) or "最近三天没有可用妙记。")
    for d in found:
        print(f"\n[{d.held_on}] {d.title} → {d.link}")