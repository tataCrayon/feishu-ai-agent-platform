"""PM Requirement Quality & Effort Estimator - PRD 质量评估与 12 特征人天预测引擎
面向示例科技项目经理李四：
1. PRD 质量机审打分 (v2.4 规则)：
   - 标题与背景 (1.5分)
   - 业务规则与流程 (3.5分)
   - 需求粒度 (2.0分)
   - 范围与依赖 (3.0分)
   - 总分 10.0 分，>=7.0 分判定为评审准入通过
2. 12 特征人天预测 (effort-efficiency 模型)：
   - 基于质量分 Q (4维) + 复杂度 C (4维) + 可信度 (4维)
   - 加载 pre-trained 模型 effort_model_12feat.joblib
   - 快速测算研发建议人天与偏差比，为 PM 评审把关提供客观数据支撑
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("pm_hub.estimator")

# parents: [0]=handlers [1]=agents [2]=app [3]=feishu-agent-platform [4]=project
# 层级要落在项目根：指到上一级时「项目管理的skill」永远找不到 ——
# 工时分项和 PRD 评分一直是静默失败的。
PROJECT_ROOT = Path(__file__).resolve().parents[4]
EFFORT_SKILL_DIR = PROJECT_ROOT / "项目管理的skill" / "skill" / "effort-efficiency"
PRD_SCORE_SCRIPT_DIR = PROJECT_ROOT / "项目管理的skill" / "skill" / "prd-quality-score" / "scripts"

if str(PRD_SCORE_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(PRD_SCORE_SCRIPT_DIR))
if str(EFFORT_SKILL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(EFFORT_SKILL_DIR / "scripts"))


def assess_prd_text(title: str, description: str) -> Dict[str, Any]:
    """对单条 PRD 进行 v2.4 质量评分。

    这里必须用 skill 的 `score_prd`：sys.path 里 effort-efficiency 优先于
    prd-quality-score，`import prd_quality_scorer` 拿到的是前者的模块，
    而它没有 `score_one_text` —— 之前一直 AttributeError 被吞成 success=False。
    """
    try:
        import prd_quality_scorer
        result = prd_quality_scorer.score_prd(
            title=title, description=description, meta={}
        )
        
        # 提取关键分项
        total_score = result.get("总分", 0.0)
        max_score = result.get("满分", 10.0)
        grade = result.get("评审准入等级", "未评级")
        reasons = result.get("主要扣分原因", [])
        
        passed = float(total_score) >= 7.0

        return {
            "success": True,
            "title": title,
            "total_score": total_score,
            "max_score": max_score,
            "grade": grade,
            "is_admissible": passed,
            "breakdown": {
                "标题与背景": result.get("标题与背景", 0.0),
                "业务规则与流程": result.get("业务规则与流程", 0.0),
                "需求粒度": result.get("需求粒度", 0.0),
                "范围与依赖": result.get("范围与依赖", 0.0),
            },
            "deductions": reasons if isinstance(reasons, list) else [str(reasons)],
            "char_count": result.get("描述字符数", len(description)),
        }
    except Exception as e:
        logger.exception(f"PRD 评分执行异常: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "PRD 质量评分模块执行异常"
        }


def _skill_feature_row(
    title: str, description: str, meta: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """走 skill 的权威 12 维。

    这里曾经自己造特征（c1 = 文本字数/400、c2/c3/c4 靠关键词命中凑），
    然后把伪造值喂给模型 —— 出来的数是假的。feature-spec.md 明令禁止
    「绕过 Excel 自造 12 维」，所以改成直接调 skill 的 build_feature_row。
    """
    import extract_features as ef

    item = {
        "id": str((meta or {}).get("work_item_id") or ""),
        "month": 0,
        "title": title,
        "description": description,
        "wiki": "",
        "manhours": {},
        "meta": meta or {},
    }
    return ef.build_feature_row(item, date.today().year)


def _confidence_block(row: Dict[str, Any]) -> Optional[str]:
    """可信度 0 表示连人天都不该出（feature-spec：Q 0~4 不出人天）。"""
    try:
        conf = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        return "可信度解析失败"
    if conf <= 0:
        return (
            "PRD 信息量不足以估时（质量分不到 5 分档）。"
            "补齐业务规则、范围与依赖后再估。"
        )
    return None


def estimate_prd_effort(
    title: str,
    description: str,
    claimed_days: Optional[float] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """PRD 质量打分 + skill 12 维模型预测人天。

    meta 可传业务线 / 优先级 / 已评审 / 模块，用来算可信度的事前修正。
    不传就按字段缺失处理（可信度会被扣 5~10），不会假装信息完整。
    """
    score_res = assess_prd_text(title, description)

    try:
        import numpy as np
        import predict_12feat_export as pred_mod

        model_path = EFFORT_SKILL_DIR / "models" / "effort_model_12feat.joblib"
        if not model_path.exists():
            return {
                "success": False,
                "message": f"未在 {model_path} 找到预训练人天模型",
            }

        row = _skill_feature_row(title, description, meta)
        blocked = _confidence_block(row)
        if blocked:
            return {"success": False, "message": blocked, "features": row}

        bundle = pred_mod.load_bundle(model_path)
        model = bundle["model"]
        feature_cols = list(bundle["feature_cols"])
        scale_off = float(bundle["scale_off"])

        X = np.array(
            [[float(row.get(c, 0.0) or 0.0) for c in feature_cols]], dtype=float
        )
        raw = pred_mod.predict_raw(model, X)[0]
        final_days = round(float(raw * scale_off), 1)
        confidence = float(row.get("confidence") or 0)

        diff_analysis = None
        if claimed_days is not None and claimed_days > 0:
            diff_pct = round(((claimed_days - final_days) / final_days) * 100, 1)
            is_over = claimed_days > final_days * 1.3
            is_under = claimed_days < final_days * 0.7
            diff_analysis = {
                "claimed_days": claimed_days,
                "predicted_days": final_days,
                "diff_percent": f"{diff_pct}%",
                "risk_flag": (
                    "估时偏高(可能虚高需挑战)" if is_over
                    else ("估时偏紧(可能低估排期风险)" if is_under else "估时合理")
                ),
            }

        return {
            "success": True,
            "title": title,
            # prd_score 用 row 的 q_total，和喂进模型的是同一个数。
            # 混用 assess_prd_text 的分会和模型输入自相矛盾。
            "prd_score": row.get("q_total"),
            "prd_grade": score_res.get("grade"),
            "is_admissible": score_res.get("is_admissible"),
            "ai_predicted_days": final_days,
            "confidence": confidence,
            "features": {
                k: row.get(k)
                for k in (
                    "q_business_rules", "q_scope_dependency",
                    "q_title_background", "q_granularity",
                    "q_total", "c_business", "c_cross_system_rule",
                    "c_test_rule", "c_risk", "c_total",
                    "conf_base_from_q", "conf_deduct_from_c",
                    "conf_prior_adj",
                )
            },
            "claimed_vs_predicted": diff_analysis,
            "pmo_advice": (
                f"PRD 得分 {row.get('q_total')}（{score_res.get('grade')}），"
                f"skill 模型预测 {final_days} 人天，可信度 {confidence:.0f}%。"
                + (
                    f" 研发填报 {claimed_days} 人天，偏差 {diff_analysis['diff_percent']}"
                    f"（{diff_analysis['risk_flag']}）。"
                    if diff_analysis else ""
                )
                + (
                    " 注意：可信度低于 85% 说明 PRD 信息不全，这个数只能当参考。"
                    if confidence < 85 else ""
                )
            ),
        }

    except Exception as e:
        logger.exception(f"工时预测异常: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "12特征模型预测执行异常",
        }