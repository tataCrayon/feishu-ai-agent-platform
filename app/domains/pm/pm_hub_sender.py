"""PM Hub 群内推送与催办卡片生成脚本
用于向 oc_chat0000000000000000000001 群发送：
1. 09:30 晨报：SOP 节拍与排期雷达
2. 17:30 晚报：今日流水催交与复盘提醒
"""

import sys
import os
import shutil
import subprocess
import json
import logging
from datetime import date

logger = logging.getLogger("pm_hub.sender")

from .pm_win import NO_WINDOW
from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    PMO_GROUP_ID,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar
)
from app.domains.pm.feishu_pm_sync import _run_lark_cli
from app.domains.pm.pm_config import pm_config


def fetch_upcoming_roadshows() -> list[dict]:
    """从飞书审批流抓取近期与待办的客户路演预约"""
    roadshows = []
    try:
        for topic in ["1", "2"]:
            params = {
                "user_id_type": "open_id",
                "user_id": pm_config().identity.open_id,
                "topic": topic,
                "page_size": 30
            }
            cmd = ["api", "GET", "/open-apis/approval/v4/tasks", "--params", json.dumps(params), "--as", "user"]
            res = _run_lark_cli(cmd)
            if res.get("ok"):
                for t in res.get("data", {}).get("tasks", []):
                    if "路演" in t.get("title", ""):
                        sums = {s.get("key"): s.get("value") for s in t.get("summaries", [])}
                        roadshows.append({
                            "code": sums.get("流程编号", ""),
                            "customer": sums.get("客户名称", ""),
                            "theme": sums.get("主题", ""),
                            "date": sums.get("预约日期", ""),
                            "time": sums.get("具体时间段", ""),
                            "type": sums.get("预约类型", "售前路演"),
                            "initiator": t.get("initiator_name", "")
                        })
    except Exception as e:
        logger.warning(f"获取路演审批失败: {e}")

    # 按照客户去重，只保留最新的有效记录
    unique = {}
    for r in roadshows:
        cust = r.get("customer")
        if cust and cust not in unique:
            unique[cust] = r
    return list(unique.values())


def send_group_message(chat_id: str, markdown_text: str):
    lark_bin = "lark-cli.cmd" if shutil.which("lark-cli.cmd") else "lark-cli"
    
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": markdown_text}, ensure_ascii=False)
    }
    
    cmd = [
        lark_bin, "api", "POST", "/open-apis/im/v1/messages",
        "--params", json.dumps({"receive_id_type": "chat_id"}),
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
        logger.error(f"发送失败: {res.stderr}")
    else:
        logger.info("发送成功")





def build_morning_brief(radar: dict) -> str:
    lines = [
        f"📅 【PMO 自动化工作台 · 每日晨报】 {radar.get('date', '')}",
        "───────────────────────",
        "🚀 **当前产品线版本节拍 (SOP V2.3 倒排)**:"
    ]
    for v in radar.get("active_versions", []):
        gate_str = v.get("current_gate") or "前期筹备"
        lines.append(f"• **{v['version']} 版本** | 目标上线: `{v['target_date']}` | 距上线: **{v['workdays_left']}** 个工作日 | 当前闸门: `{gate_str}`")

    # 1. 发版前一周预警检测（周四发版，前一周周二/周三倒计时约 6~7 个工作日）
    advance_alerts = []
    for v in radar.get("active_versions", []):
        days = v.get("workdays_left", 99)
        ver = v.get("version", "")
        # 11.5 已经确认无需提醒，针对后续版本如 11.12
        if ver in ["11.05", "11.5"]:
            continue
        if days in [6, 7]:
            advance_alerts.append(f"• 🔔 **【发版前一周确认提醒】** `{ver}` 版本距上线仅剩 **{days}** 个工作日，请在今明两天重点确认**开发与测试进展及潜在风险**！")
    
    if advance_alerts:
        lines.append("")
        lines.append("📢 **发版前周期关键提醒**:")
        lines.extend(advance_alerts)

    # 2. 客户路演与培训审批前瞻提醒
    roadshows = fetch_upcoming_roadshows()
    if roadshows:
        lines.append("")
        lines.append("🎤 **客户路演与培训日程感知 (审批流直连)**:")
        for r in roadshows[:3]:
            time_str = r.get("time") or r.get("date")
            lines.append(f"• **{r.get('customer')}** | `{r.get('type')}` | 时间: `{time_str}` | 发起人: `{r.get('initiator')}`\n  *诉求: {r.get('theme')}*")

    lines.append("")
    lines.append("⚠️ **重点关注与待跟进事项**:")
    if radar.get("follow_ups"):
        for f in radar["follow_ups"][:4]:
            lines.append(f"• 需求：{f['title']} (版本 {f['version']}) 缺少 **{f['missing']}**，请向技术负责人核对。")
    else:
        lines.append("• 暂无严重阻塞项，所有责任人齐备。")
        
    lines.append("")
    lines.append("💡 **今日 PM 推荐动作**:")
    lines.append("1. 请回复本群列出【今日工作计划】，系统将自动沉淀并同步至多维表格。")
    lines.append("2. 关注 **11.05 版本**（华夏银行等 4 项需求）进入开发与方案关键期；核验 **11.12 版本** 责任人补齐。")
    lines.append("───────────────────────")
    lines.append("🤖 *回复本群即可直接问答需求/记录今日流水/同步日计划*")
    return "\n".join(lines)


def build_evening_review_card(records: list[dict], radar: dict) -> dict:
    """构建晚间日清盘点与交付偏差交互卡片"""
    completed_count = 0
    pending_count = 0
    blocked_count = 0

    pending_items = []
    for r in records:
        f = r.get("fields", {})
        status = f.get("状态", "未开始")
        task_name = f.get("任务", "")
        if isinstance(task_name, list):
            task_name = "".join([x.get("text", "") for x in task_name])
        
        if status == "已完成":
            completed_count += 1
        elif status == "阻塞":
            blocked_count += 1
            pending_items.append(f"🚫 `{task_name}` (阻塞)")
        else:
            pending_count += 1
            if len(pending_items) < 3:
                pending_items.append(f"⏳ `{task_name}` ({status})")

    total = completed_count + pending_count + blocked_count
    completion_rate = f"{(completed_count / total * 100):.0f}%" if total > 0 else "0%"

    elements = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"📊 **日计划完成率**\n`{completed_count}/{total}` ({completion_rate})"
                    }
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"🚨 **阻塞风险项**\n`{blocked_count}` 项"
                    }
                }
            ]
        }
    ]

    if pending_items:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "📌 **待收口/进行中重点事项**：\n" + "\n".join(pending_items)
            }
        })

    # 下一个工作日关键闸门提醒
    next_gate_hints = []
    for v in radar.get("active_versions", []):
        days = v.get("workdays_left", 99)
        ver = v.get("version", "")
        if days == 6:
            next_gate_hints.append(f"• **{ver} 版本**：明天将进入 **T-5 (G5 提测准入窗口)**，需在明晨前锁定自测证据链与冒烟用例")
        elif days <= 2 and days > 0:
            next_gate_hints.append(f"• **{ver} 版本**：仅剩 **{days}** 个工作日，严控代码合流与封版范围")

    # 明日/近期路演前瞻
    roadshows = fetch_upcoming_roadshows()
    if roadshows:
        for r in roadshows[:2]:
            t_str = r.get("time") or r.get("date")
            next_gate_hints.append(f"• 🎤 **【客户路演日程】{r.get('customer')}** ({r.get('theme')}) | `{t_str}`")

    if next_gate_hints:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "🧭 **明日关键事项与闸门前瞻（请确认明日动作）**：\n" + "\n".join(next_gate_hints)
                }
            }
        ])

    elements.extend([
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "💬 **【明日工作交接与发问】**\n**李四，今天这几项收口得如何？明天核心重点攻哪几项？**\n*直接回复本群或私聊（如：“今天华夏银行准备完毕，明天主攻11.12 ESOP出金与准备路演”），系统将立即锁定并沉淀为明日日计划，明晨 09:00 准时向你发起雷达叫醒！*"
            }
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "🔗 打开多维表格【日计划】看板"
                    },
                    "type": "primary",
                    "multi_url": {
                        "url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable001",
                        "pc_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable001",
                        "android_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable001",
                        "ios_url": "https://example.feishu.cn/base/bascnExampleToken0000000000?table=tblExampleTable001"
                    }
                }
            ]
        }
    ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🌙 【PMO 日清复盘 · 交付偏差雷达】 {radar.get('date', '')}"
            },
            "template": "blue" if blocked_count == 0 else "carmine"
        },
        "elements": elements
    }


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    stories = parse_stories(vals)
    radar = evaluate_daily_radar(stories, today=date(2026, 10, 21))
    brief_md = build_morning_brief(radar)
    send_group_message(PMO_GROUP_ID, brief_md)
