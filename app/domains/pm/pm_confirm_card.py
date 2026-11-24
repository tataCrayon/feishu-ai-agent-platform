"""PM Confirm Card - 强确认交互卡片与动作执行引擎
严格遵循李四“强确认原则”：
1. 组装飞书交互式卡片 (Interactive Card)，向「PMO自动化工作台」群推送变更审批
2. 提供执行动作函数 (execute_meeting_sync)，当李四确认批准后，自动写入多维表格并沉淀本地 log
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Dict, List

from app.domains.pm.feishu_pm_sync import (
    BASE_TOKEN,
    TABLE_ID,
    _run_lark_cli,
    fetch_existing_records,
)
from app.domains.pm.pm_config import owner_name, pm_config

from .pm_win import NO_WINDOW

if TYPE_CHECKING:
    # 只在类型检查时需要。pm_meeting_sensor 已经不产出 MeetingInsight 了
    # （会议认知改成 MeetingDigest，业务过筛交给 pm_daily_triage），
    # 但下面两个会议卡片的构造函数还在，跑在 app/main.py 那条旧链路上。
    # 写成运行时导入会让整个 daemon 起不来 —— 这是真实踩过的坑。
    from app.domains.pm.pm_meeting_sensor import MeetingInsight

logger = logging.getLogger("pm_hub.confirm_card")


def _receive_id_type(receive_id: str) -> str:
    """`ou_` 是人（私聊）、`oc_` 是群。与 `app.protocols.feishu_send` 同规则。"""
    return "open_id" if receive_id.startswith("ou_") else "chat_id"


def build_meeting_confirm_card(insight: MeetingInsight) -> Dict[str, Any]:
    """生成高信息密度、结构清晰的待办审核卡片"""
    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": "📅 **会议时间**\n2026-10-12 13:30"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": "🚀 **目标发版**\n10.29 (剩 6 工作日)"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": "🚦 **当前闸门**\nG4/G5 提测准入窗口"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": "👤 **PMO 负责人**\n李四"
                    }
                }
            ]
        },
        {
            "tag": "hr"
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"📋 **过筛后核心待办事项 ({len(insight.todos)} 项)**\n*已过滤产品内部琐碎事项，仅保留需要推进或阻塞提测的卡点*"
            }
        }
    ]

    # 将每条待办渲染为规范的结构化面板
    for idx, td in enumerate(insight.todos[:4], 1):
        clean_text = td.content.replace("\n", " ").strip()
        risk_label = "🔴 严重 (阻塞 G5 提测)" if td.sop_risk else "🟡 关注"
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**[待办 {idx}]** {clean_text}\n• **责任人**：`{td.owner}`　• **建议截止**：`2026-10-23 (T-5 前)`　• **风险级别**：{risk_label}"
            }
        })

    elements.extend([
        {
            "tag": "hr"
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "💡 **快捷编辑方式**：\n1. **多维表格在线编辑**：已将上述待办同步至表格，[点击直达多维表格编辑](https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable000)\n2. **群内直接回复修改**：回复本条消息，如：“不要第1条，第2条责任人改成张三”"
            }
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "🔗 打开多维表格【日计划】"
                    },
                    "type": "primary",
                    "multi_url": {
                        "url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable000",
                        "pc_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable000",
                        "android_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable000",
                        "ios_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable000"
                    }
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "📖 查看原始妙记"
                    },
                    "type": "default",
                    "multi_url": {
                        "url": insight.app_link,
                        "pc_url": insight.app_link,
                        "android_url": insight.app_link,
                        "ios_url": insight.app_link
                    }
                }
            ]
        }
    ])

    card = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📌 【待办治理 · 需求评审跟进】{insight.title}"
            },
            "template": "turquoise"
        },
        "elements": elements
    }
    return card


def send_interactive_card(chat_id: str, card_payload: Dict[str, Any]) -> Dict[str, Any]:
    """通过飞书 Bot 身份发送交互卡片到群或私聊。

    `receive_id_type` 按 `chat_id` 前缀选：老调用方传的都是群（`oc_`），
    没传前缀时按群处理——写死 `chat_id` 会让 `ou_` 直投报 400（`invalid receive_id`）。
    """
    lark_bin = "lark-cli.cmd" if shutil.which("lark-cli.cmd") else "lark-cli"
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card_payload, ensure_ascii=False)
    }

    cmd = [
        lark_bin, "api", "POST", "/open-apis/im/v1/messages",
        "--params", json.dumps({"receive_id_type": _receive_id_type(chat_id)}),
        "--data", "-",
        "--as", "bot"
    ]

    env = dict(os.environ)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"

    res = subprocess.run(
        cmd,
        input=json.dumps(body, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        shell=True,
        creationflags=NO_WINDOW,
    )
    if res.returncode != 0:
        raise RuntimeError(f"发送交互卡片失败: {res.stderr}")
    return json.loads(res.stdout)


def execute_meeting_sync_to_bitable(insight: MeetingInsight) -> Dict[str, Any]:
    """当李四确认批准后，执行具体的入库与日志动作"""
    now = datetime.now()
    ts_ms = int(datetime(now.year, now.month, now.day, now.hour, now.minute).timestamp() * 1000)

    tasks_to_add = []
    for td in insight.todos[:4]:
        # 截取纯净内容
        clean_content = td.content.split("@")[0].strip()
        task_name = f"[{insight.title[:8]}] {clean_content}"
        tasks_to_add.append({
            "fields": {
                "任务": task_name[:80],
                "计划日期": ts_ms,
                "质量闸门": "G5 准入保障" if td.sop_risk else "日常待办",
                "状态": "未开始",
                "闸门结论": "待检查",
                "负责人": td.owner or owner_name(),
                "备注": f"来源会议: {insight.title} | {td.sop_risk or '推进中'}"
            }
        })

    # 查重并写入多维表格
    existing = fetch_existing_records()
    existing_titles = set()
    empty_ids = []
    for item in existing:
        f = item.get("fields", {})
        t = f.get("任务")
        if not t:
            empty_ids.append(item.get("record_id"))
        else:
            if isinstance(t, list):
                existing_titles.add("".join([x.get("text", "") for x in t]))
            else:
                existing_titles.add(str(t))

    created = 0
    updated = 0
    for t_data in tasks_to_add:
        if t_data["fields"]["任务"] in existing_titles:
            continue
        if empty_ids:
            rid = empty_ids.pop(0)
            cmd = [
                "api", "PUT",
                f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/{rid}",
                "--data", "-",
                "--as", "user"
            ]
            _run_lark_cli(cmd, input_data=json.dumps({"fields": t_data["fields"]}, ensure_ascii=False))
            updated += 1
        else:
            cmd = [
                "api", "POST",
                f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
                "--data", "-",
                "--as", "user"
            ]
            _run_lark_cli(cmd, input_data=json.dumps({"fields": t_data["fields"]}, ensure_ascii=False))
            created += 1

    # 记录到本地 daily-log
    log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../daily-log"))
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{date.today().strftime('%Y-%m-%d')}.md")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n### [{now.strftime('%H:%M')}] 会议智能感知归档：{insight.title}\n")
        f.write(f"- **会议链接**：{insight.app_link}\n")
        f.write(f"- **李四已批准同步**：写入多维表格 {created + updated} 条关键待办\n")
        for td in insight.todos[:4]:
            f.write(f"  - [{td.owner}] {td.content}\n")

    return {
        "created": created,
        "updated": updated,
        "log_path": log_path
    }


if __name__ == "__main__":
    # 手工试发：会议强确认卡片的旧链路（会议认知已迁到 pm_daily_triage，
    # 这里保留是为了能单独拿一场妙记验证卡片版式）。`analyze_meeting_insights`
    # 随会议认知一起迁走，这里不再可用，留着只会给人「能跑」的错觉。
    from app.domains.pm.pm_meeting_sensor import fetch_minute_detail
    from app.domains.pm.pm_hub_engine import (
        SPREADSHEET_TOKEN,
        SHEET_ID_VERSIONS,
        fetch_sheet_values,
        parse_stories,
    )

    print("=== 测试发送强确认卡片到 PMO 自动化工作台 ===")
    print("注意：会议待办抽取已迁到 pm_daily_triage，这里只验证卡片版式。")
    print("卡片发送会因缺 Insight 对象失败，属预期——版式见 build_meeting_confirm_card。")
    detail = fetch_minute_detail("obcnkrf87iv6o2b15bi9652g")
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    stories = parse_stories(vals)
    print(f"妙记 {detail.get('title', '')} / story 行数 {len(stories)}")
    print(f"推送目标 group_id = {pm_config().push.group_id!r}")

def card_message_id(resp: Dict[str, Any]) -> str:
    """从发送结果里取 message_id。拿不到返回空串。

    为什么不直接写 `resp["data"]["message_id"]`：飞书的返回体在不同接口/版本
    下多包一层或少一层，取错只是抛 KeyError，而调用方（发卡片后原地更新）
    拿空串会静默退化成「再发一条新消息」——看着能用，实际是刷屏。
    """
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        return str(data.get("message_id") or "")
    if isinstance(resp, dict):
        return str(resp.get("message_id") or "")
    return ""
