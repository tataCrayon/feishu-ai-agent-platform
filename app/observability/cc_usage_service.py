"""Claude Code 通道用量统计 — 扫描 ~/.claude/projects/<本项目>/*.jsonl 转录。

背景：feishu-agent-platform 的费用统计若不覆盖 CC 通道，就只覆盖编排层（路由/直答），
dev-bot 经 cc-connect 执行时 Claude Code 在网关侧烧的 token 是统计盲区。
实测转录文件每条 assistant 消息带完整 usage（input/output/cache_read + model），
本模块把它聚合出来，作为「Claude Code 通道费用」独立指标。

口径与坑：
- 去重：同一 API 响应可能拆成多条记录（thinking/text 分块），按 message.id 去重；
  无 message.id 的记录按 uuid 计。
- <synthetic> 模型（本地合成消息）无真实计费，跳过。
- 模型名归一：glm-5-3-flash-260828 → glm-5.3-flash（点号被网关替换为连字符 + 版本后缀）。
- 缓存读按 cache 价计（estimate_cost 的 cache_read_input_tokens 口径）。
- 时间按 UTC 记录、+8 时区归日。
- 性能：转录目录数百 MB，逐文件按 (mtime,size) 缓存聚合结果到
  logs/cc_usage/cache.json；文件未变不重读，只有新增文件/变更文件才解析。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.llm import estimate_cost

logger = logging.getLogger("cc_usage")

TZ = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CACHE_FILE = PROJECT_DIR / "logs" / "cc_usage" / "cache.json"

# 本项目对应的 Claude Code 转录目录（目录名 = 工作目录路径转写）
_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"


def _project_dirs() -> list[Path]:
    """定位 feishu-agent-platform 相关的转录目录（含 .claude/skills 子项目路径变体）。"""
    if not _TRANSCRIPT_ROOT.is_dir():
        return []
    out = []
    for d in _TRANSCRIPT_ROOT.iterdir():
        if d.is_dir() and re.search(r"feishu-agent-platform", d.name, re.IGNORECASE):
            out.append(d)
    return out


def _norm_model(model: str) -> str:
    """glm-5-3-flash-260828 → glm-5.3-flash；识别不了的原样返回。"""
    if not model or model == "<synthetic>":
        return model
    m = re.match(r"^(glm|deepseek|qwen|kimi)-(\d)-(\d)(-flash|-pro|-max|-turbo)?", model)
    if m:
        family, major, minor, suffix = m.groups()
        return f"{family}.{major}.{minor}{suffix or ''}"
    return model


def _parse_transcript(fp: Path) -> dict[str, dict[str, dict[str, int]]]:
    """解析单个转录文件 → {归日: {模型: {input, output, cache_read, calls}}}。

    按模型分桶：切换模型（model_switch.py）后历史天仍按原模型定价，不串价。
    """
    per_day: dict[str, dict[str, dict[str, int]]] = {}
    seen: set[str] = set()
    try:
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                model = msg.get("model", "")
                if not model or model == "<synthetic>":
                    continue
                key = msg.get("id") or rec.get("uuid")
                if not key or key in seen:
                    continue
                seen.add(key)
                ts = rec.get("timestamp", "")
                try:
                    day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d")
                except ValueError:
                    continue
                bucket = per_day.setdefault(day, {}).setdefault(
                    _norm_model(model), {"input": 0, "output": 0, "cache_read": 0, "calls": 0})
                bucket["input"] += int(usage.get("input_tokens", 0) or 0)
                bucket["output"] += int(usage.get("output_tokens", 0) or 0)
                bucket["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
                bucket["calls"] += 1
    except OSError as e:
        logger.warning("cc_usage: failed to read %s: %s", fp, e)
    return per_day


def _load_cache() -> dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def scan_cc_usage(days: int = 7) -> dict[str, Any]:
    """聚合最近 N 天的 Claude Code 通道用量与成本（带文件级增量缓存）。

    Returns:
        {days: [{date, calls, input_tokens, output_tokens, cache_read_tokens,
                 cost_usd}], totals: {...}, scanned_files, cached_files}
    """
    cache = _load_cache()
    files_scanned = 0
    files_cached = 0
    for d in _project_dirs():
        for fp in sorted(d.glob("*.jsonl")):
            stat = fp.stat()
            sig = f"{stat.st_mtime_ns}:{stat.st_size}"
            entry = cache.get(fp.name)
            if entry and entry.get("sig") == sig:
                files_cached += 1
                continue
            per_day = _parse_transcript(fp)
            cache[fp.name] = {"sig": sig, "days": per_day}
            files_scanned += 1
    _save_cache(cache)

    cutoff = (datetime.now(TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    # {date: {model: {input, output, cache_read, calls}}}
    agg: dict[str, dict[str, dict[str, int]]] = {}
    for entry in cache.values():
        for day, models in (entry.get("days") or {}).items():
            if day < cutoff:
                continue
            # 兼容旧缓存格式（v1：扁平桶无模型维度）→ 归到 default_model
            if "calls" in models:
                models = {_default_model(): models}
            for model, b in models.items():
                a = agg.setdefault(day, {}).setdefault(
                    model, {"input": 0, "output": 0, "cache_read": 0, "calls": 0})
                for k in a:
                    a[k] += b.get(k, 0)

    out_days = []
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
              "cache_read_tokens": 0, "cost_usd": 0.0}
    for day in sorted(agg):
        day_cost = 0.0
        day_calls = day_in = day_out = day_cache = 0
        for model, b in agg[day].items():
            # 网关回显的 input_tokens 已含 cache_read（实测 671560 含 670720，
            # 否则超上下文窗口），故非缓存部分 = input - cache_read，缓存部分按 cache 价。
            day_cost += estimate_cost(model, {
                "prompt_tokens": b["input"],
                "completion_tokens": b["output"],
                "cache_read_input_tokens": b["cache_read"],
            })
            day_calls += b["calls"]
            day_in += b["input"]
            day_out += b["output"]
            day_cache += b["cache_read"]
        out_days.append({
            "date": day,
            "calls": day_calls,
            "input_tokens": day_in,
            "output_tokens": day_out,
            "cache_read_tokens": day_cache,
            "cost_usd": round(day_cost, 8),
        })
        totals["calls"] += day_calls
        totals["input_tokens"] += day_in
        totals["output_tokens"] += day_out
        totals["cache_read_tokens"] += day_cache
        totals["cost_usd"] = round(totals["cost_usd"] + day_cost, 6)
    return {
        "model": _default_model(),
        "days": out_days,
        "totals": totals,
        "scanned_files": files_scanned,
        "cached_files": files_cached,
    }


def _default_model() -> str:
    from app.config import load_config

    try:
        return load_config().router.model or "glm-5.3-flash"
    except Exception:
        return "glm-5.3-flash"
