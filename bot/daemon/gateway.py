#!/usr/bin/env python3
"""
Feishu Message Gateway Daemon (消息网关)
=========================================
- 轮询群消息，检测 @张三 → 写 pending 文件
- 超时检查：10分钟未回复 → 代发「收到，已记录 ✅」
- 零 LLM 消耗，纯 HTTP API 调用

启动：python bot/daemon/gateway.py
"""

import json
import logging
import os
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------- config ----------
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent  # feishu-agent-platform/
CONFIG_TOML = PROJECT_DIR / "config.toml"
GROUPS_FILE = PROJECT_DIR / ".agent" / "todos" / "groups.txt"
LAST_CHECK_FILE = PROJECT_DIR / ".agent" / "todos" / "last_check.json"
PENDING_DIR = PROJECT_DIR / ".agent" / "todos" / "pending"
DONE_DIR = PROJECT_DIR / ".agent" / "todos" / "done"

POLL_INTERVAL = 60          # 秒
REPLY_TIMEOUT = 10 * 60     # 10 分钟
MSGS_PER_FETCH = 10         # 每次拉的消息数
TZ = timezone(timedelta(hours=8))  # CST

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gateway")


# ---------- Feishu API ----------
class FeishuAPI:
    """飞书 OpenAPI 封装"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._token_expires: float = 0

    def _get_token(self) -> str:
        """获取 tenant_access_token（自动缓存+刷新）"""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {data}")
        self._token = data["tenant_access_token"]
        self._token_expires = time.time() + data.get("expire", 3600)
        log.info("token 已刷新")
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def list_messages(self, chat_id: str, page_token: str = "", page_size: int = MSGS_PER_FETCH) -> dict:
        """获取群消息列表（倒序）"""
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": page_size,
            "sort_type": "ByCreateTimeDesc",
        }
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        return resp.json()

    def get_message(self, msg_id: str) -> dict:
        """获取单条消息详情"""
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}",
            headers=self._headers(),
            timeout=10,
        )
        return resp.json()

    def list_reactions(self, msg_id: str) -> dict:
        """获取消息的 reaction 列表"""
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reactions",
            headers=self._headers(),
            timeout=10,
        )
        return resp.json()

    def send_text(self, chat_id: str, text: str) -> dict:
        """发送文本消息到群"""
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers=self._headers(),
            json=body,
            timeout=10,
        )
        return resp.json()


# ---------- helpers ----------
def load_config() -> tuple[str, str]:
    """从 config.toml 读取飞书 app_id + app_secret（凭据优先 .env）"""
    load_dotenv(CONFIG_TOML.parent / ".env")
    with open(CONFIG_TOML, "rb") as f:
        cfg = tomllib.load(f)
    pf = cfg["projects"][0]["platforms"][0]["options"]
    # 凭据不入库：config.toml 留空，从 .env 兜底读取
    return pf["app_id"], os.getenv("FEISHU_APP_SECRET") or pf["app_secret"]


def load_groups() -> list[str]:
    """读取监控群列表"""
    if not GROUPS_FILE.exists():
        log.warning("groups.txt 不存在，无群可监控")
        return []
    lines = GROUPS_FILE.read_text(encoding="utf-8").strip().split("\n")
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def load_last_check() -> dict:
    """读取增量扫描状态"""
    if LAST_CHECK_FILE.exists():
        return json.loads(LAST_CHECK_FILE.read_text(encoding="utf-8"))
    return {}


def save_last_check(state: dict):
    """保存增量扫描状态"""
    LAST_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_CHECK_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def write_pending(chat_id: str, msg_id: str, sender_id: str, text: str, created_at: str):
    """写入待回复文件"""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    file_path = PENDING_DIR / f"{msg_id}.md"
    file_path.write_text(
        f"chat_id: {chat_id}\n"
        f"sender: {sender_id}\n"
        f"msg: {text}\n"
        f"created_at: {created_at}\n"
        f"detected_at: {datetime.now(TZ).isoformat()}\n",
        encoding="utf-8",
    )
    log.info(f"记录 pending: {chat_id} msg={msg_id}")


def parse_pending_file(path: Path) -> dict | None:
    """解析 pending 文件，返回 dict"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = {}
        for line in raw.strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        return data
    except Exception:
        return None


def check_admin_replied(api: FeishuAPI, chat_id: str, since_time: str, admin_id: str) -> bool:
    """
    检查管理员在 since_time 之后是否在群里发过消息或 reaction。
    since_time 格式: 毫秒时间戳字符串（Feishu 格式）
    """
    try:
        since_ts = int(since_time) / 1000  # Feishu 毫秒 → Unix 秒
    except (ValueError, TypeError):
        since_ts = 0

    # 拉取最近消息
    resp = api.list_messages(chat_id)
    if resp.get("code") != 0:
        log.warning(f"查群消息失败: {resp}")
        return False

    items = resp.get("data", {}).get("items", [])
    for item in items:
        msg_create_time = int(item.get("create_time", "0")) / 1000
        if msg_create_time <= since_ts:
            continue

        # 检查消息发送者是否管理员（REST API 中 sender.id 是字符串）
        sender = item.get("sender", {})
        if sender.get("id") == admin_id:
            log.info(f"管理员已回复（消息）: msg_id={item.get('message_id')}")
            return True

        # 检查消息的 reaction
        msg_id = item.get("message_id", "")
        if msg_id:
            r_resp = api.list_reactions(msg_id)
            if r_resp.get("code") == 0:
                for r in r_resp.get("data", {}).get("items", []):
                    if r.get("operator", {}).get("id") == admin_id:
                        log.info(f"管理员已回复（reaction）: msg_id={msg_id}")
                        return True

    return False


def process_pending(api: FeishuAPI, admin_id: str):
    """处理超时 pending"""
    if not PENDING_DIR.exists():
        return

    now = datetime.now(TZ)
    for f in sorted(PENDING_DIR.glob("*.md")):
        data = parse_pending_file(f)
        if not data:
            continue

        created_at = data.get("created_at", "")
        # parse Feishu create_time (毫秒时间戳字符串)
        try:
            created_ts = int(created_at) / 1000
            created_dt = datetime.fromtimestamp(created_ts, tz=TZ)
        except (ValueError, TypeError):
            log.warning(f"无法解析创建时间: {created_at}")
            continue

        elapsed = (now - created_dt).total_seconds()
        if elapsed < REPLY_TIMEOUT:
            continue  # 还没到 10 分钟

        chat_id = data.get("chat_id", "")
        log.info(f"超时检查: chat={chat_id} 已等待 {elapsed:.0f}s")

        # 检查管理员是否已回复
        if check_admin_replied(api, chat_id, created_at, admin_id):
            log.info(f"管理员已回复，删除 pending: {f.name}")
            f.unlink()
            continue

        # 代回复
        sender_name = data.get("sender", "未知")
        reply_text = "收到，已记录 ✅ 张三稍后回复你"
        resp = api.send_text(chat_id, reply_text)
        if resp.get("code") == 0:
            log.info(f"代回复成功: chat={chat_id}")
            # 移到 done
            DONE_DIR.mkdir(parents=True, exist_ok=True)
            f.rename(DONE_DIR / f.name)
        else:
            log.error(f"代回复失败: {resp}")


def scan_group(api: FeishuAPI, chat_id: str, admin_id: str, last_check: dict):
    """扫描单个群的新消息"""
    last_msg_id = last_check.get(chat_id, "")
    new_last = last_msg_id  # 记录本轮看到的最新 msg_id

    resp = api.list_messages(chat_id)
    if resp.get("code") != 0:
        log.warning(f"拉群消息失败 chat={chat_id}: {resp}")
        return new_last

    items = resp.get("data", {}).get("items", [])
    if not items:
        return new_last

    # 第一条（最新）消息就是新的 last_msg_id
    new_last = items[0].get("message_id", new_last)

    for item in items:
        msg_id = item.get("message_id", "")

        # 如果已经处理过这条，后面的更旧，跳过
        if msg_id == last_msg_id:
            break

        # 检查 mentions（REST API 中 mentions[].id 是字符串，不是嵌套 dict）
        mentions = item.get("mentions", [])
        for m in mentions:
            if m.get("id") == admin_id:
                # 解析消息内容
                body_raw = item.get("body", {}).get("content", "{}")
                try:
                    body = json.loads(body_raw)
                except json.JSONDecodeError:
                    body = {}
                text = body.get("text", "")[:80]
                sender = item.get("sender", {}).get("id", "")
                created_at = item.get("create_time", "")
                log.info(f"发现 @张三: chat={chat_id} msg={msg_id} text={text}")
                write_pending(chat_id, msg_id, sender, text, created_at)
                break

    return new_last


# ---------- main ----------
def main():
    log.info("===== 飞书消息网关启动 =====")

    # 加载配置
    app_id, app_secret = load_config()
    log.info(f"app_id={app_id[:8]}...")

    api = FeishuAPI(app_id, app_secret)
    admin_id = "ou_admin000000000000000000000000"

    while True:
        try:
            groups = load_groups()
            if not groups:
                time.sleep(POLL_INTERVAL)
                continue

            last_check = load_last_check()

            for chat_id in groups:
                new_last = scan_group(api, chat_id, admin_id, last_check)
                if new_last:
                    last_check[chat_id] = new_last

            save_last_check(last_check)
            process_pending(api, admin_id)

        except Exception as e:
            log.error(f"循环异常: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
