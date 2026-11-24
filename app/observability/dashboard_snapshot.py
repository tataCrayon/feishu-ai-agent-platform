#!/usr/bin/env python
"""Dashboard 控制台一键截图验收（headless 浏览器快照）。

问题背景：
    控制台 UI 改动（如 index.html 布局调整）此前靠 AI 手敲 headless Edge 命令
    截图、再用 PIL 裁剪逐段查看，每次都是临时拼命令（易踩坑：目标目录必须
    预先存在、device-scale-factor 影响像素尺寸、页面渲染需要 virtual-time-budget）。
    本脚本把这些固化为一条命令，UI 改动后 30 秒内拿到三视口快照。

用法：
    python app/observability/dashboard_snapshot.py                 # 默认三视口全页
    python app/observability/dashboard_snapshot.py --url http://127.0.0.1:9112/
    python app/observability/dashboard_snapshot.py --widths 800 1280 1920

产出（logs/dash-shots/，gitignored）：
    shot_{width}.png          # 指定宽度、scale=1 的整页截图
    摘要打印每张图的尺寸与像素是否异常（全白/过小）

退出码：0 = 全部成功；1 = 页面不可达；2 = 无可用浏览器；3 = 部分截图失败
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "logs" / "dash-shots"

# Edge/Chrome 常见安装位置（Windows）；PATH 里有则优先
_CANDIDATES = [
    shutil.which("msedge"),
    shutil.which("chrome"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for path in _CANDIDATES:
        if path and pathlib.Path(path).exists():
            return path
    return None


def page_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def shoot(browser: str, url: str, width: int, out: pathlib.Path, budget_ms: int = 9000) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    height = 2400 if width >= 1200 else 2000
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        f"--window-size={width},{height}",
        "--force-device-scale-factor=1",  # scale=1 保证像素=CSS 尺寸，裁剪坐标不漂移
        f"--virtual-time-budget={budget_ms}",  # 等待 fetch 渲染完成
        f"--screenshot={out}",
        url,
    ]
    # encoding+errors 必须显式给：Edge 在中文 Windows 上混吐 GBK 提示行，
    # text=True 走严格 UTF-8 会在 subprocess 读取线程抛 UnicodeDecodeError
    # （常驻进程里表现为线程堆积后退出，同 mcpreaper._run 的坑）。
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    if not out.exists():
        print(f"  FAIL {width}px: {proc.stderr.strip()[:200]}")
        return False
    size = out.stat().st_size
    tiny = size < 20_000  # 全白页约 10-20KB
    print(f"  OK   {width}px -> {out.name} ({size//1024}KB{'  ⚠ 疑似空白页' if tiny else ''})")
    return not tiny


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:9112/")
    ap.add_argument("--widths", type=int, nargs="+", default=[800, 1280])
    ap.add_argument("--budget", type=int, default=9000, help="virtual-time-budget 毫秒")
    args = ap.parse_args()

    if not page_ok(args.url):
        print(f"页面不可达: {args.url}（先启动 python -m app.main）")
        return 1

    browser = find_browser()
    if not browser:
        print("未找到 Edge/Chrome，安装或将其加入 PATH")
        return 2

    stamp = datetime.datetime.now().strftime("%H%M%S")
    print(f"浏览器: {browser}\n目标: {args.url}")
    results = []
    for width in args.widths:
        out = OUT_DIR / f"shot_{width}_{stamp}.png"
        results.append(shoot(browser, args.url, width, out, args.budget))

    meta = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "url": args.url,
        "widths": args.widths,
        "ok": results,
    }
    (OUT_DIR / "last_run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"产出目录: {OUT_DIR}")
    if all(results):
        return 0
    if not any(results):
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
