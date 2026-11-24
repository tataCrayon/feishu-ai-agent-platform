"""PM Hub Engine - 核心领域与排期差分引擎
1. 读取飞书工作簿 shtcnExampleToken0000000000 (产品线版本计划 sht000 / 每周计划跟踪 sht001)
2. 基于《私募研发流程 SOP-V2.3》的 7 道闸门，计算倒排工作日与今日行动
3. 纯函数式差分计算，无副作用
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from typing import List, Dict, Optional, Any

from app.domains.pm.pm_config import pm_config


_cfg = pm_config()

# 兼容层：值来自 `[projects.pm]`，不是字面量。155 处引用先不动，
# 只有这 5 个定义点改；换人 / 换表改配置即可。
SPREADSHEET_TOKEN = _cfg.outputs.spreadsheet_token
SHEET_ID_VERSIONS = _cfg.outputs.sheet_id_versions   # 产品线版本计划
SHEET_ID_WEEKLY = _cfg.outputs.sheet_id_weekly       # 每周计划跟踪
PM_OPEN_ID = _cfg.identity.open_id
PMO_GROUP_ID = _cfg.push.group_id


@dataclass
class StoryItem:
    version: str                # 例如 "10.29", "11.05", "11.12"
    line: str                   # 业务线：公募基金 / 私募基金 / ESOP
    title: str                  # 需求名称
    dev_owner: str              # 开发责任人
    qa_owner: str               # 测试责任人
    pm_owner: str               # 产品责任人
    target_date: str            # 目标上线日
    status: str                 # 状态：SIT测试 / 实现中 / 待开发
    remark: str                 # 备注/待跟进


@dataclass
class SOPGate:
    gate_id: str               # G1~G7
    name: str                  # 闸门名称
    anchor_t: int              # 相对发版日 T 的倒排工作日 (例如提测 G5 = -5)
    owner_role: str            # 负责角色
    check_standard: str        # 检查标准


SOP_GATES = [
    SOPGate("G1", "需求冻结", -13, "产品经理", "版本需求列表锁定，无待确认项"),
    SOPGate("G2", "评审通过", -11, "项目经理", "PRD无阻塞性歧义，达到可开发标准"),
    SOPGate("G3", "承接承诺", -11, "技术负责人", "签署承接清单，未列入者不得进开发"),
    SOPGate("G4", "方案评审通过", -10, "技术负责人", "技术设计与接口文档定稿"),
    SOPGate("G5", "提测准入", -5, "测试负责人/开发", "自测+联调+冒烟通过，CR合并"),
    SOPGate("G6", "代码冻结", -1, "项目经理/技术负责人", "SIT严重缺陷归零，UAT视觉验收通过"),
    SOPGate("G7", "上线工单确认", 0, "项目经理", "PM提单并在大群同步，清单逐项@收齐回执"),
]


def _calc_workdays_diff(target_dt: date, current_dt: date) -> int:
    """计算当前日期与目标日期之间的工作日差值（跳过周六周日）"""
    if target_dt == current_dt:
        return 0
    step = 1 if target_dt > current_dt else -1
    cur = current_dt
    workdays = 0
    while cur != target_dt:
        cur += timedelta(days=step)
        if cur.weekday() < 5:  # 0~4 为周一至周五
            workdays += step
    return workdays


def fetch_sheet_values(spreadsheet_token: str, range_name: str) -> List[List[Any]]:
    """通过 lark-cli --as user 拉取工作簿指定范围内容。

    走 `feishu_pm_sync._run_lark_cli` 而不是自己拼 subprocess：那条路会把
    身份路由到 PM 用户自己的凭据目录（`pm_identity`）。自己拼就退回
    机器级默认身份——表现是「别人的 token 读你的表」，看起来能读，
    但读到的权限边界和推消息的对象不是同一个人。
    """
    from app.domains.pm.feishu_pm_sync import _run_lark_cli

    res = _run_lark_cli([
        "api", "GET",
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_name}",
    ])
    if not res.get("ok", False):
        raise RuntimeError(f"获取工作簿失败: {str(res)[:300]}")
    return (res.get("data") or {}).get("valueRange", {}).get("values", [])


def parse_stories(values: List[List[Any]]) -> List[StoryItem]:
    """解析 sht000 产品线版本计划表格"""
    stories: List[StoryItem] = []
    current_version = ""
    
    for row in values[2:]:  # 前两行是标题与表头
        if not row or not any(row):
            continue
        v_col = str(row[0]).strip() if len(row) > 0 and row[0] is not None and str(row[0]).strip() else ""
        if v_col:
            current_version = v_col
        
        line = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        title = str(row[2]).strip() if len(row) > 2 and row[2] else ""
        if not title:
            continue
            
        dev = str(row[3]).strip() if len(row) > 3 and row[3] else "待定"
        qa = str(row[4]).strip() if len(row) > 4 and row[4] else "待定"
        pm = str(row[5]).strip() if len(row) > 5 and row[5] else "待定"
        target_date = str(row[6]).strip() if len(row) > 6 and row[6] else current_version
        status = str(row[7]).strip() if len(row) > 7 and row[7] else "待确认"
        remark = str(row[8]).strip() if len(row) > 8 and row[8] else ""
        
        # 归一化版本归属：优先以“目标上线日期”作为版本归属（解决表格合并单元格跨版本或修改目标日期的情况）
        story_version = target_date if target_date in ["10.29", "11.05", "11.12", "11.5"] else current_version
        if story_version == "11.5":
            story_version = "11.05"
        
        stories.append(StoryItem(
            version=story_version,
            line=line,
            title=title,
            dev_owner=dev,
            qa_owner=qa,
            pm_owner=pm,
            target_date=target_date,
            status=status,
            remark=remark
        ))
    return stories


def evaluate_daily_radar(stories: List[StoryItem], today: Optional[date] = None) -> Dict[str, Any]:
    """SOP 节拍与今日行动雷达计算"""
    if today is None:
        today = date.today()
    
    # 映射目标上线日（默认当前年份）
    current_year = today.year
    report: Dict[str, Any] = {
        "date": today.strftime("%Y-%m-%d"),
        "active_versions": [],
        "warnings": [],
        "follow_ups": [],
        "gate_status": []
    }
    
    # 按版本聚合
    version_groups: Dict[str, List[StoryItem]] = {}
    for s in stories:
        version_groups.setdefault(s.version, []).append(s)
        
    for ver_str, v_stories in version_groups.items():
        try:
            parts = ver_str.split(".")
            month = int(parts[0])
            day = int(parts[1])
            target_dt = date(current_year, month, day)
        except Exception:
            continue
            
        workdays_left = _calc_workdays_diff(target_dt, today)
        v_info = {
            "version": ver_str,
            "target_date": target_dt.strftime("%Y-%m-%d"),
            "workdays_left": workdays_left,
            "story_count": len(v_stories),
            "current_gate": None
        }
        
        # 判定当前落在哪个 SOP 闸门附近
        for gate in sorted(SOP_GATES, key=lambda g: g.anchor_t):
            if workdays_left <= -gate.anchor_t: # 倒排倒数
                v_info["current_gate"] = gate.gate_id + " " + gate.name
                
        report["active_versions"].append(v_info)
        
        # 规则 1：提测卡点（T-5）检查
        # 若距离上线不足或等于 5 个工作日，但状态仍为“实现中”或“待开发”
        if workdays_left <= 5 and workdays_left >= 0:
            for s in v_stories:
                if s.status in ["待开发", "实现中"]:
                    report["warnings"].append({
                        "level": "HIGH",
                        "version": ver_str,
                        "title": s.title,
                        "owner": s.dev_owner,
                        "qa": s.qa_owner,
                        "status": s.status,
                        # `workdays_left` 单列一份：文案里已经拼了「剩N工作日」，
                        # 但下游要按天数排序/过滤时只能对句子再正则一次。
                        "workdays_left": workdays_left,
                        "msg": f"按SOP已到提测准入窗口(剩{workdays_left}工作日)，但仍为[{s.status}]",
                        "action": f"催办开发 {s.dev_owner}：请确认是否能在今天/明天提交SIT提测并附带自测记录"
                    })

        # 规则 2：测试责任人轮换校验
        for s in v_stories:
            if ver_str == "10.29":
                if "公募" in s.line and s.qa_owner != "赵六":
                    report["warnings"].append({
                        "level": "MEDIUM",
                        "version": ver_str,
                        "title": s.title,
                        "workdays_left": workdays_left,
                        "msg": f"1029公募测试责任人应为赵六，当前记录为: {s.qa_owner}"
                    })
                elif "私募" in s.line and s.qa_owner != "吴君锋":
                    report["warnings"].append({
                        "level": "MEDIUM",
                        "version": ver_str,
                        "title": s.title,
                        "workdays_left": workdays_left,
                        "msg": f"1029私募测试责任人应为吴君锋，当前记录为: {s.qa_owner}"
                    })
            elif ver_str == "11.12":
                if "公募" in s.line and s.qa_owner != "吴君锋":
                    report["warnings"].append({
                        "level": "MEDIUM",
                        "version": ver_str,
                        "title": s.title,
                        "workdays_left": workdays_left,
                        "msg": f"1112公募测试责任人应为吴君锋，当前记录为: {s.qa_owner}"
                    })
                elif "私募" in s.line and s.qa_owner != "赵六":
                    report["warnings"].append({
                        "level": "MEDIUM",
                        "version": ver_str,
                        "title": s.title,
                        "workdays_left": workdays_left,
                        "msg": f"1112私募测试责任人应为赵六，当前记录为: {s.qa_owner}"
                    })
                    
        # 规则 3：责任人缺失
        for s in v_stories:
            if s.dev_owner in ["待定", ""] or s.qa_owner in ["待定", ""]:
                report["follow_ups"].append({
                    "version": ver_str,
                    "title": s.title,
                    "missing": "开发责任人" if s.dev_owner in ["待定", ""] else "测试责任人",
                    "action": "需向产品/技术负责人确认具体指派人"
                })
                
    return report


if __name__ == "__main__":
    vals = fetch_sheet_values(SPREADSHEET_TOKEN, f"{SHEET_ID_VERSIONS}!A1:N25")
    stories = parse_stories(vals)
    radar = evaluate_daily_radar(stories)
    print("=== PM Hub 节拍与排期雷达实测 ===")
    print(f"解析到版本需求数: {len(stories)}")
    for v in radar["active_versions"]:
        print(f"版本: {v['version']} | 上线日: {v['target_date']} | 剩余工作日: {v['workdays_left']} | SOP状态: {v['current_gate']}")
    print(f"\n[风险预警 ({len(radar['warnings'])} 条)]:")
    for w in radar["warnings"]:
        print(f" - [{w.get('level')}] {w.get('title')} ({w.get('owner', '')}): {w.get('msg')}")
    print(f"\n[待跟进项 ({len(radar['follow_ups'])} 条)]:")
    for f in radar["follow_ups"]:
        print(f" - [{f.get('version')}] {f.get('title')}: 缺少 {f.get('missing')}")
