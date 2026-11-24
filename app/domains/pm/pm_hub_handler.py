"""PM Hub Agent Handler - 注册到 feishu-agent-platform 调度器的独立 Handler
支持李四在飞书群/私聊中通过自然语言：
1. 查询当前版本排期与 SOP 闸门倒排 (pm_query_radar)
2. 触发或同步日计划到多维表格 (pm_sync_bitable)
3. 更新指定任务的完成状态或闸门结论 (pm_update_task)
4. 将日常备忘/风险沉淀到本地 daily-log (pm_record_log)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict

from app.agents.handlers import AgentHandler, ToolResult
from app.domains.pm.pm_hub_engine import (
    SPREADSHEET_TOKEN,
    SHEET_ID_VERSIONS,
    fetch_sheet_values,
    parse_stories,
    evaluate_daily_radar,
)
from app.domains.pm.feishu_pm_sync import (
    BASE_TOKEN,
    TABLE_ID,
    fetch_existing_records,
    generate_today_tasks,
    sync_tasks_to_bitable,
    _run_lark_cli,
)
from app.domains.pm.pm_todo_intake import (
    intake_and_sync_todos,
)
from app.domains.pm.pm_meeting_sensor import (
    fetch_recent_minutes,
    fetch_minute_detail,
)
from app.domains.pm.pm_knowledge_hub import (
    query_people_map,
    search_knowledge_base,
    capture_to_inbox,
)
from app.domains.pm.pm_estimator import (
    estimate_prd_effort,
)


def _digest_of(detail: Dict[str, Any]) -> Dict[str, Any]:
    """把 `fetch_minute_detail` 的原始返回摊成「标题 / 链接 / 未完成行动项」。

    会议认知从「Insight + 过筛」改成「Digest + 交给 triage 判」之后，
    `analyze_meeting_insights` 已经不存在。这里只做结构映射，不做业务判断——
    哪条行动项该推给谁由 `pm_daily_triage` 决定。
    """
    from app.domains.pm.pm_meeting_sensor import _to_digest

    digest = _to_digest(detail or {}, {}, 1)
    if digest is None:
        return {"title": "", "link": "", "open_todos": []}
    return {
        "title": digest.title,
        "link": digest.link,
        "open_todos": [t.content for t in digest.open_todos],
    }

logger = logging.getLogger("agents.handlers.pm_hub")

# ── 注册给 LLM 的工具清单 ─────────────────────────────────────────

PM_HUB_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "pm_query_radar",
            "description": "查询当前公募/私募/ESOP各产品线最新版本排期、距离发版工作日倒数以及SOP V2.3的G1-G7闸门预警",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_sync_bitable",
            "description": "自动推导今天的SOP闸门行动，并同步写入或更新到飞书多维表格的「日计划」看板中",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_update_task",
            "description": "更新多维表格日计划中某项任务的状态（如：进行中、已完成、阻塞）或闸门结论（待检查、通过、未通过）",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "任务关键词，例如 '10.29' 或 '提测'",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["未开始", "进行中", "已完成", "阻塞"],
                        "description": "任务状态",
                    },
                    "gate_result": {
                        "type": "string",
                        "enum": ["待检查", "通过", "未通过"],
                        "description": "闸门结论",
                    },
                    "remark": {
                        "type": "string",
                        "description": "补充说明/最新进展",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_record_log",
            "description": "将李四在群里确认的事项、复盘速记或排期变更沉淀记录到本地 daily-log",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "速记流水或变更内容",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["排期变更", "风险记录", "每日复盘", "临时备忘"],
                        "description": "日志分类",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_intake_todos",
            "description": "摄入李四在群聊或私聊中发送的自然语言每日待办，执行SOP V2.3防御性盲区扫描，并自动写入飞书多维表格日计划",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_text": {
                        "type": "string",
                        "description": "用户发送的原始待办文本（可多行）",
                    },
                    "auto_sync": {
                        "type": "boolean",
                        "description": "是否自动写入飞书多维表格日计划看板，默认 true",
                    },
                },
                "required": ["raw_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_scan_recent_meetings",
            "description": "感知并扫描李四名下最近的飞书妙记会议，提纯阻塞交付与直属李四的卡点，并推送到群聊交互确认卡片",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "扫描最近会议场数，默认 3",
                    },
                    "send_card": {
                        "type": "boolean",
                        "description": "是否直接向 PMO 自动化工作台群推送强确认卡片，默认 true",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_confirm_meeting_sync",
            "description": "在李四确认批准某场会议待办后，正式将过筛后的待办写入多维表格日计划并归档至本地 daily-log",
            "parameters": {
                "type": "object",
                "properties": {
                    "minute_token": {
                        "type": "string",
                        "description": "飞书妙记 token",
                    },
                },
                "required": ["minute_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_query_people",
            "description": "在人物地图（People Map）中查询模块负责人、业务线归属、AB角或特定事项应该找谁协同与审批",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "查询关键词，如 'WebPortal前端', '董泓林', '票据业务', '审批'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_search_knowledge",
            "description": "在项目部组织级知识库（L1概念库、L2流程操作库、L3经验决策库及SOP规范）中进行穿透检索",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如 '发版流程', '金融术语', '踩坑记录', '交付规范'",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["L1-业务概念库", "L2-流程操作库", "L3-经验决策库"],
                        "description": "指定知识库层级（可选）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_inbox_capture",
            "description": "将群聊中碎片化灵感、临时踩坑总结或未分类信息暂存到 inbox/ 快存池",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "速记或待分类内容",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "分类标签（可选）",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pm_estimate_effort",
            "description": "运用 12 特征模型预测建议研发人天，支持对比研发报审人天并输出估时偏差与风险旗标（偏高虚高/偏紧风险/合理）",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "需求标题",
                    },
                    "description": {
                        "type": "string",
                        "description": "需求详细描述或技术要点",
                    },
                    "claimed_days": {
                        "type": "number",
                        "description": "研发报审的人天数（可选）",
                    },
                },
                "required": ["title", "description"],
            },
        },
    },
]

# 人设单源在 bot/prompts/agents/pm-hub-agent.md（由 PromptLoader 注入）。
# 这里保留其原始文本，不是为了运行期使用，而是给「改提示词前先看它写了什么」留个
# 就地参考；**不要**在代码里读它拼 system prompt，那会变成两份会漂移的副本。


class PMHubAgentHandler(AgentHandler):
    """PMO 自动化外骨骼专属 Agent Handler

    人设写在 `bot/prompts/agents/pm-hub-agent.md`（单源，`PromptLoader` 读取）；
    本文件只声明工具面与执行逻辑，不再内联一份 system prompt 副本。
    """

    agent_name = "pm-hub-agent"
    # 工具面单源就是这个模块级清单，类属性直接引用它（基类按类属性导出 schema）。
    tools = PM_HUB_TOOLS

    def __init__(self, router_config: Any):
        super().__init__(router_config)

    async def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        logger.info(f"PMHubAgentHandler execute_tool: {name}, args={args}")
        try:
            if name == "pm_query_radar":
                vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
                stories = parse_stories(vals)
                # 目前测试排期基准以当前年份版本为准
                radar = evaluate_daily_radar(stories, today=date.today())
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps(radar, ensure_ascii=False)
                )

            elif name == "pm_sync_bitable":
                vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
                stories = parse_stories(vals)
                radar = evaluate_daily_radar(stories, today=date.today())
                tasks = generate_today_tasks(radar, today_dt=date.today())
                res = sync_tasks_to_bitable(tasks)
                return ToolResult(
                    tool_call_id="",
                    content=f"已成功将今日任务同步至飞书多维表格: 新增 {res['created']} 条, 填充 {res['updated']} 条, 跳过已有 {res['skipped']} 条"
                )

            elif name == "pm_update_task":
                keyword = args.get("keyword", "")
                records = fetch_existing_records()
                matched_rec = None
                for r in records:
                    t_field = r.get("fields", {}).get("任务", "")
                    if isinstance(t_field, list):
                        t_field = "".join([x.get("text", "") for x in t_field])
                    if keyword in str(t_field):
                        matched_rec = r
                        break

                if not matched_rec:
                    return ToolResult(tool_call_id="", content=f"未找到包含关键词 '{keyword}' 的日计划任务")

                rec_id = matched_rec["record_id"]
                fields_to_update: dict[str, Any] = {}
                if args.get("status"):
                    fields_to_update["状态"] = args["status"]
                if args.get("gate_result"):
                    fields_to_update["闸门结论"] = args["gate_result"]
                if args.get("remark"):
                    old_remark = matched_rec.get("fields", {}).get("备注", "")
                    fields_to_update["备注"] = f"{old_remark}\n[更新]: {args['remark']}".strip()

                cmd = [
                    "api", "PUT",
                    f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/{rec_id}",
                    "--data", "-",
                    "--as", "user"
                ]
                _run_lark_cli(cmd, input_data=json.dumps({"fields": fields_to_update}, ensure_ascii=False))
                return ToolResult(tool_call_id="", content=f"任务 '{keyword}' 更新成功: {fields_to_update}")

            elif name == "pm_record_log":
                content = args.get("content", "")
                cat = args.get("category", "每日复盘")
                today_str = date.today().strftime("%Y-%m-%d")
                log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../daily-log"))
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(log_dir, f"{today_str}.md")

                line = f"- **[{datetime.now().strftime('%H:%M')}] [{cat}]**: {content}\n"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
                return ToolResult(tool_call_id="", content=f"已成功沉淀至日志文件: {log_path}")

            elif name == "pm_intake_todos":
                raw_text = args.get("raw_text", "")
                auto_sync = args.get("auto_sync", True)
                res = intake_and_sync_todos(raw_text, auto_sync_bitable=auto_sync)
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps(res, ensure_ascii=False)
                )

            elif name == "pm_scan_recent_meetings":
                limit = args.get("limit", 3)
                mins = fetch_recent_minutes(limit=limit)
                if not mins:
                    return ToolResult(tool_call_id="", content="未拉取到近期的飞书妙记会议记录")

                # 只回「今天有哪些会、各自留下几条未完成行动项」。
                # 业务过筛（哪条归谁、该不该推）统一在 pm_daily_triage，
                # 这里不再做第二套判断——两套边界迟早互相矛盾。
                results = []
                for m in mins:
                    m_token = m.get("minute_token") or m.get("token")
                    if not m_token:
                        continue
                    try:
                        detail = fetch_minute_detail(m_token)
                        digest = _digest_of(detail)
                        results.append({
                            "title": digest["title"],
                            "minute_token": m_token,
                            "open_todos": digest["open_todos"],
                            "link": digest["link"],
                        })
                    except Exception as err:
                        logger.warning(f"处理妙记 {m_token} 失败: {err}")

                return ToolResult(
                    tool_call_id="",
                    content=f"会议感知完成: 处理了 {len(results)} 场会议, 详情: {json.dumps(results, ensure_ascii=False)}"
                )

            elif name == "pm_confirm_meeting_sync":
                # 「确认某场会的行动项」= 把该会里未完成的行动项按自然语言待办
                # 灌进日计划。走 `pm_todo_intake` 这条既有链路，不另建一套写表逻辑，
                # 否则同一张多维表格会有两种写入口径。
                m_token = args.get("minute_token", "")
                if not m_token:
                    return ToolResult(tool_call_id="", content="缺少 minute_token 参数", is_error=True)

                detail = fetch_minute_detail(m_token)
                digest = _digest_of(detail)
                if not digest["open_todos"]:
                    return ToolResult(tool_call_id="", content=f"《{digest['title']}》没有未完成的行动项，无需同步")

                body = "\n".join(f"{i}. {x}" for i, x in enumerate(digest["open_todos"], 1))
                res = intake_and_sync_todos(body, auto_sync_bitable=True)
                return ToolResult(
                    tool_call_id="",
                    content=f"《{digest['title']}》的行动项已入表：{res}"
                )

            elif name == "pm_query_people":
                q = args.get("query", "")
                res = query_people_map(q)
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps(res, ensure_ascii=False)
                )

            elif name == "pm_search_knowledge":
                q = args.get("query", "")
                layer = args.get("layer")
                docs = search_knowledge_base(q, layer=layer)
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps(docs, ensure_ascii=False)
                )

            elif name == "pm_inbox_capture":
                content = args.get("content", "")
                tags = args.get("tags", [])
                cap_res = capture_to_inbox(content, tags=tags)
                return ToolResult(
                    tool_call_id="",
                    content=f"已成功快存至待审池: {cap_res['file_name']}"
                )

            elif name == "pm_estimate_effort":
                t = args.get("title", "")
                d = args.get("description", "")
                c_days = args.get("claimed_days")
                est_data = estimate_prd_effort(title=t, description=d, claimed_days=c_days)
                return ToolResult(
                    tool_call_id="",
                    content=json.dumps(est_data, ensure_ascii=False)
                )

            else:
                return ToolResult(tool_call_id="", content=f"未知工具: {name}", is_error=True)

        except Exception as e:
            logger.exception(f"执行 PM Hub 工具失败: {e}")
            return ToolResult(tool_call_id="", content=f"工具执行异常: {str(e)}", is_error=True)


def build(config: Any, ctx: dict[str, Any]) -> PMHubAgentHandler | None:
    """约定式装配入口（`app/agents/handlers/discovery.py` 优先找 `build`）。

    `PMHubAgentHandler` 原本没有 `agent_name`，也不在这个目录里，所以既不会被
    `auto_discover_handlers` 扫到、也没法注册。这里补上装配器：

    - 未配置 `[projects.pm]` → 返回 None，注册表里就没有这项能力（而不是
      注册一个每次调用都报「未配置」的空壳）；
    - `ctx` 里没有 `router_config` 时取 `app_config().router`，与其它 Handler 一致。
    """
    from app.domains.pm.pm_config import pm_config

    if not pm_config().enabled:
        return None
    router_config = (ctx or {}).get("router_config") or ctx.get("config")
    if router_config is None:
        from app.config import load_config

        router_config = load_config().router
    return PMHubAgentHandler(router_config)


PMHubAgentHandler.agent_name = "pm-hub-agent"