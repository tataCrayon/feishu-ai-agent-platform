#!/usr/bin/env python3
"""SVG → PNG（feishu-agent-platform 适配版）。

原 skill 的 rsvg-convert / cairosvg 在本机（Windows 无 GTK 运行库）不可用；
feishu-agent-platform venv 已带 playwright（dashboard 截图同源），用 headless Chromium
对 SVG 元素截图，零系统依赖。

Usage: python svg_to_png_bot.py <report.svg> [report.png]
"""
import asyncio
import pathlib
import sys


async def _convert(svg_path: str, png_path: str) -> None:
    from playwright.async_api import async_playwright

    svg = pathlib.Path(svg_path).read_text(encoding="utf-8")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 2000})
        await page.set_content(
            f'<html><body style="margin:0;background:white">{svg}</body></html>'
        )
        el = await page.query_selector("svg")
        if el is None:
            raise RuntimeError("no <svg> element found in content")
        await el.screenshot(path=png_path)
        await browser.close()


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: svg_to_png_bot.py <report.svg> [report.png]\n")
        return 1
    svg_path = sys.argv[1]
    png_path = sys.argv[2] if len(sys.argv) > 2 else str(
        pathlib.Path(svg_path).with_suffix(".png")
    )
    asyncio.run(_convert(svg_path, png_path))
    print(f"PNG written: {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
