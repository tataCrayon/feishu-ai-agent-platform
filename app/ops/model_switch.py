#!/usr/bin/env python3
"""模型一键切换 — 把全仓模型触点收口成一条命令。

背景：模型三要素虽单源在根 .env，但换模型仍要手动改 4 处
（根 .env / 上游工单服务 .env / config.toml CC 映射 / MODEL_PRICES 定价表），
漏一处就出现「路由用新模型、CC 通道用旧模型」的口径分裂。本脚本原子改齐 +
自动验证 + 打印回滚命令。

用法：
    python app/ops/model_switch.py show                       # 当前各触点模型状态
    python app/ops/model_switch.py set <model> --input 0.14 --output 0.43 [--cache 0.01]
                                                                  # 切换（价格 USD/1M，来自 aigw 定价页）
    python app/ops/model_switch.py set <model> ... --no-cc    # 只切编排层，不动 config.toml CC 映射
    python app/ops/model_switch.py verify                     # 一致性 + 网关连通 + function calling 冒烟
    python app/ops/model_switch.py set ... --dry-run          # 只打印将改什么

注意：
- CC 通道（Claude Code）实际请求的模型由 aigw 控制台「模型映射」决定，
  本脚本改的是 config.toml 里的 provider env 副本；控制台须同步改 + 重启 cc-connect。
- 切换后须重启 bot（python -m app.main）生效；golden 门禁须重跑：
  python app/eval/router_intent_eval.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
# 工单 A2A 服务（webportal-order-agent）独立仓库 .env —— 引擎模型触点。
# v3.0.0 去 vendor 后原 bot/skills/order-sql 两份 .env 随引擎迁出，
# 触点改为上游仓库（本机 checkout，不存在时跳过不计入一致性）。
UPSTREAM_ENV = Path(r"D:\Projects\vscode-projects\webportal-order-agent\.env")
ENV_FILES = [PROJECT_DIR / ".env"] + ([UPSTREAM_ENV] if UPSTREAM_ENV.exists() else [])
CONFIG_TOML = PROJECT_DIR / "config.toml"
CLIENT_PY = PROJECT_DIR / "app" / "llm" / "client.py"


def _rel(fp: Path) -> str:
    """展示用相对路径（PROJECT_DIR 外的触点如上游仓库显示绝对路径）。"""
    try:
        return str(fp.relative_to(PROJECT_DIR))
    except ValueError:
        return str(fp)

# config.toml 中承载模型名的 env 键（CC 映射副本）
_CC_ENV_KEYS = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def _read(fp: Path) -> str:
    return fp.read_text(encoding="utf-8")


def _write(fp: Path, content: str) -> None:
    fp.write_text(content, encoding="utf-8")


def _env_model(fp: Path) -> str:
    m = re.search(r"^LLM_MODEL=(.*)$", _read(fp), re.M)
    return m.group(1).strip() if m else "(缺行)"


def _env_key(fp: Path) -> str:
    m = re.search(r"^LLM_API_KEY=(.{0,8})", _read(fp), re.M)
    return m.group(1) if m else "(缺行)"


def _pricing_of(model: str) -> dict | None:
    """从 client.py 定价表读某模型的三档价（USD/1K）。"""
    m = re.search(
        rf'"{re.escape(model)}":\s*\{{([^}}]*)\}}', _read(CLIENT_PY))
    if not m:
        return None
    vals = dict(re.findall(r'"(input|output|cache)":\s*([0-9.e-]+)', m.group(1)))
    return {k: float(v) for k, v in vals.items()}


def cmd_show() -> int:
    print("── 模型触点现状 ──")
    for fp in ENV_FILES:
        print(f"  {_rel(fp)}: LLM_MODEL={_env_model(fp)}  KEY={_env_key(fp)}…")
    cc_models = set(re.findall(r'ANTHROPIC_DEFAULT_\w+_MODEL(?:_NAME)? = "([^"]+)"', _read(CONFIG_TOML)))
    print(f"  config.toml CC 映射: {cc_models or '(无)'}")
    m = re.search(r'^      model = "([^"]+)"', _read(CONFIG_TOML), re.M)
    print(f"  config.toml provider model: {m.group(1) if m else '(无)'}")
    try:
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
        sys.path.insert(0, str(PROJECT_DIR))
        from app.config import load_config
        cfg = load_config()
        print(f"  运行时 router.model: {cfg.router.model}")
    except Exception as e:
        print(f"  运行时 router.model: (加载失败 {e})")
    return 0


def _set_env_model(new: str, dry: bool) -> None:
    for fp in ENV_FILES:
        s = _read(fp)
        if f"LLM_MODEL={new}" in s:
            print(f"  [skip] {_rel(fp)} 已是 {new}")
            continue
        old = _env_model(fp)
        if "LLM_MODEL=" in s:
            s2 = re.sub(r"^LLM_MODEL=.*$", f"LLM_MODEL={new}", s, flags=re.M)
        else:
            s2 = s.rstrip("\n") + f"\nLLM_MODEL={new}\n"
        print(f"  [env] {_rel(fp)}: {old} → {new}")
        if not dry:
            _write(fp, s2)


def _set_cc_mapping(new: str, dry: bool) -> None:
    s = _read(CONFIG_TOML)
    out = s
    for key in _CC_ENV_KEYS:
        out = re.sub(rf'({key} = ")[^"]*(")', rf"\g<1>{new}\g<2>", out)
    out = re.sub(r'^      model = "[^"]*"', f'      model = "{new}"', out, flags=re.M)
    n = sum(len(re.findall(rf'{k} = "{re.escape(new)}"', out)) for k in _CC_ENV_KEYS)
    print(f"  [cc] config.toml: {n} 处映射 + provider model → {new}")
    if not dry:
        _write(CONFIG_TOML, out)


def _set_pricing(new: str, inp: float, outp: float, cache: float | None, dry: bool) -> None:
    s = _read(CLIENT_PY)
    entry = f'"{new}": {{"input": {inp / 1e6}, "output": {outp / 1e6}'
    entry += f', "cache": {cache / 1e6}}}' if cache is not None else "}"
    if re.search(rf'"{re.escape(new)}":\s*\{{', s):
        s2 = re.sub(rf'"{re.escape(new)}":\s*\{{[^}}]*\}}', entry, s, count=1)
        print(f"  [price] 更新已有条目: {entry}")
    else:
        anchor = "MODEL_PRICES: dict[str, dict[str, float]] = {\n"
        s2 = s.replace(anchor, anchor + f"    # 当前铁律模型（model_switch.py 维护）\n    {entry},\n", 1)
        print(f"  [price] 新增条目: {entry}")
    if not dry:
        _write(CLIENT_PY, s2)


def cmd_set(args) -> int:
    new = args.model
    print(f"══ 切换目标模型 → {new}{'（dry-run）' if args.dry_run else ''} ══")
    _set_env_model(new, args.dry_run)
    if args.no_cc:
        print("  [skip] --no-cc：不动 config.toml CC 映射（编排层-only 切换）")
    else:
        _set_cc_mapping(new, args.dry_run)
    if args.input is not None:
        _set_pricing(new, args.input, args.output, args.cache, args.dry_run)
    else:
        print("  [warn] 未提供 --input/--output 定价：MODEL_PRICES 未更新，"
              "费用统计会走 DEFAULT_PRICES 兜底（不准）")
    if args.dry_run:
        return 0
    print("\n── 验证 ──")
    rc = cmd_verify(argparse.Namespace(live=True))
    print("\n回滚：python app/ops/model_switch.py set <旧模型> --input <旧价> --output <旧价> [--cache <旧缓存价>]")
    print("生效：重启 bot；CC 通道另需 aigw 控制台同步改「模型映射」+ 重启 cc-connect")
    print("门禁：python app/eval/router_intent_eval.py")
    return rc


def cmd_verify(args) -> int:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,aigw.example.com,example.com")
    sys.path.insert(0, str(PROJECT_DIR))
    failures = []

    # 1) 各触点一致性
    models = {_env_model(fp) for fp in ENV_FILES}
    try:
        from app.config import load_config
        cfg = load_config()
        models.add(cfg.router.model)
        runtime_model = cfg.router.model
    except Exception as e:
        failures.append(f"config 加载失败: {e}")
        runtime_model = "?"
    if len(models) > 1:
        failures.append(f"触点模型不一致: {models}")
    else:
        print(f"  [ok] 触点一致: {models}")

    # 2) 定价表收录
    model = models.pop() if len(models) == 1 else runtime_model
    prices = _pricing_of(model)
    if prices:
        print(f"  [ok] MODEL_PRICES 收录 {model}: {prices}")
    else:
        failures.append(f"{model} 不在 MODEL_PRICES（费用统计走兜底价，不准）")

    # 3) 网关连通 + function calling 冒烟（路由关键路径）
    if getattr(args, "live", True):
        try:
            key = cfg.router.api_key
            base = cfg.router.base_url.rstrip("/")
            body = {
                "model": model,
                "messages": [{"role": "user", "content": "你能做什么？"}],
                "tools": [{"type": "function", "function": {
                    "name": "route_to_help",
                    "description": "帮助/自我介绍/能力查询：用户问机器人能做什么时使用",
                    "parameters": {"type": "object", "properties": {}},
                }}],
                "tool_choice": "auto",
                "max_tokens": 200,
            }
            req = urllib.request.Request(
                f"{base}/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(req, timeout=60))
            msg = resp["choices"][0]["message"]
            if msg.get("tool_calls"):
                print(f"  [ok] function calling 冒烟通过: {msg['tool_calls'][0]['function']['name']}")
            else:
                failures.append(f"function calling 未触发（content={str(msg.get('content'))[:60]}）"
                                "——路由依赖 tool_calls，该模型可能不适合做 router")
        except Exception as e:
            failures.append(f"网关冒烟失败: {e}")

    if failures:
        print("\n── 验证失败 ──")
        for f in failures:
            print(f"  [FAIL] {f}")
        return 1
    print("  验证全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="当前各触点模型状态")
    sp = sub.add_parser("set", help="切换模型（价格 USD/1M，取自 aigw 定价页）")
    sp.add_argument("model")
    sp.add_argument("--input", type=float, help="输入价 $/1M")
    sp.add_argument("--output", type=float, help="输出价 $/1M")
    sp.add_argument("--cache", type=float, help="缓存读价 $/1M")
    sp.add_argument("--no-cc", action="store_true", help="只切编排层，不动 config.toml CC 映射")
    sp.add_argument("--dry-run", action="store_true")
    vp = sub.add_parser("verify", help="一致性 + 网关冒烟")
    vp.add_argument("--no-live", action="store_true", help="跳过网关调用")
    args = ap.parse_args()
    if args.cmd == "show":
        return cmd_show()
    if args.cmd == "set":
        return cmd_set(args)
    return cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
