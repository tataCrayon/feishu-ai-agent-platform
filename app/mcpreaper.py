"""MCP 孤儿进程清理器 — 彻底防止 nacos-mcp 进程泄漏。

背景：cc-connect 为每个 Claude 子会话拉起 MCP 子进程，
子会话退出时 MCP 进程未被正确回收 → 残留孤儿进程 → 大量向 Nacos 发 auth 登录请求。

本模块：
1. 启动时全量清理：杀掉所有 nacos-mcp 进程（一个不留）
2. 定期巡检：后台任务每 2 分钟检查一次，发现即杀
3. 白名单保护：只处理 nacos-mcp 相关进程
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("mcpreaper")

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], timeout: int = 20) -> str:
    """跑一条 Windows 系统命令，输出按 GBK 兜底解码。

    wmic/tasklist 输出中文系统消息时是 GBK 编码；text=True 固定按 UTF-8 解码
    会在读取线程里抛 UnicodeDecodeError（每次巡检一条，线程堆积后进程退出）。
    """
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    for enc in ("utf-8", "gbk"):
        try:
            return result.stdout.decode(enc, errors="replace")
        except UnicodeDecodeError:
            continue
    return result.stdout.decode("utf-8", errors="replace")


def _is_windows() -> bool:
    return sys.platform == "win32"


# ── 进程枚举 ──


# 需清理的 MCP 关键字（命令行模糊匹配整棵进程树）
# nacos-mcp 泄漏：claude 子会话退出时，cmd→npx→node nacos-mcp 整棵进程树残留，
# 持续向 Nacos 发 auth 登录请求，触发安全软件 PasswordStealer.HTTP 告警。
# 可执行名（nacos-mcp.exe）只是叶子；cmd/node/npx 父进程名各不相同，必须按命令行关键字匹配。
MCP_KEYWORDS = ("nacos-mcp", "vinkugor/nacos-mcp", "@vinkugor/nacos-mcp")


def _cmdline_matches(cmdline: str) -> bool:
    """判断命令行是否命中 MCP 泄漏关键字。"""
    cl = cmdline.lower()
    return any(k in cl for k in MCP_KEYWORDS)


def _collect_all_cmdlines() -> list[dict]:
    """枚举所有进程的 PID + 命令行（Windows 用 wmic，跨进程树根因匹配）。"""
    procs: list[dict] = []
    try:
        out = _run(
            ["wmic", "process", "get", "ProcessId,ParentProcessId,Name,CommandLine", "/format:csv"],
        )
        for line in out.stdout.strip().splitlines():
            # CSV 格式: Node,CommandLine,Name,ParentProcessId,ProcessId
            parts = line.split(",")
            if len(parts) < 5:
                continue
            # 命令行可能含逗号被拆散，安全取最后两列（ParentProcessId, ProcessId）
            pid = parts[-1].strip()
            parent = parts[-2].strip()
            if not pid.isdigit():
                continue
            cmdline = ",".join(parts[1:-3]).strip().strip('"')
            procs.append({
                "pid": int(pid),
                "parent": int(parent) if parent.isdigit() else 0,
                "name": parts[-3].strip('" ') if len(parts) >= 3 else "",
                "commandline": cmdline,
            })
    except Exception as e:
        logger.debug(f"wmic enumerate failed: {e}")
    return procs


def list_mcp_processes() -> list[dict]:
    """列出所有 nacos-mcp 相关进程（含整棵 cmd/node/npx 进程树），多种策略确保不漏。"""
    results: list[dict] = []
    if not _is_windows():
        try:
            out = _run(
                ["pgrep", "-af", "nacos-mcp"],
                timeout=15,
            )
            for line in out.stdout.splitlines():
                parts = line.strip().split(" ", 1)
                if parts:
                    results.append({"pid": int(parts[0]), "name": "nacos-mcp", "commandline": parts[1] if len(parts) > 1 else ""})
        except Exception as e:
            logger.debug(f"pgrep failed: {e}")
        return results

    # Windows：按命令行关键字匹配整棵进程树（根因清理）
    all_procs = _collect_all_cmdlines()
    matched = [p for p in all_procs if _cmdline_matches(p["commandline"])]
    if matched:
        logger.debug(f"wmic cmdline: {len(matched)} nacos-mcp 相关进程")
        return matched

    # 兜底策略：直接查 nacos-mcp.exe（wmic 失败或未命中时）
    try:
        out = _run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq nacos-mcp.exe"],
            timeout=15,
        )
        for line in out.stdout.strip().splitlines():
            line = line.strip()
            if not line or not line.startswith('"'):
                continue
            parts = line.split(",")
            if len(parts) >= 2:
                pid = parts[1].strip('" ')
                if pid.isdigit():
                    results.append({"pid": int(pid), "name": "nacos-mcp", "commandline": ""})
        if results:
            logger.debug(f"tasklist fallback: {len(results)} nacos-mcp.exe")
            return results
    except Exception:
        pass

    return results


def kill_process(pid: int, name: str) -> bool:
    """杀掉指定进程。"""
    try:
        if _is_windows():
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
            )
            return r.returncode == 0
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=15)
            return True
    except Exception as e:
        logger.warning(f"Failed to kill {name} (pid={pid}): {e}")
        return False


def reap_orphans(expected: int = 0, dry_run: bool = False) -> int:
    """清理 MCP 孤儿进程。

    Args:
        expected: 期望保留数（默认 0，一个不留）。
        dry_run: 只报告不清理。

    Returns:
        清理掉的进程数。
    """
    procs = list_mcp_processes()
    if not procs:
        return 0

    logger.info(f"MCP scan: {len(procs)} nacos-mcp found (expected ≤ {expected})")

    killed = 0
    for p in procs:
        if dry_run:
            killed += 1
        else:
            if kill_process(p["pid"], p["name"]):
                killed += 1

    if killed > 0:
        logger.info(f"MCP reaper: killed {killed} nacos-mcp processes")
    return killed


# ── 定期巡检 ──


async def _watch_loop(interval_sec: int, expected: int, dry_run: bool = False):
    """后台定期巡检，每 interval_sec 秒杀一次。"""
    logger.info(f"MCP reaper watch started (interval={interval_sec}s, expected≤{expected})")
    while True:
        try:
            await asyncio.to_thread(reap_orphans, expected, dry_run)
        except Exception as e:
            logger.warning(f"MCP reaper cycle failed: {e}")
        await asyncio.sleep(interval_sec)


def start_watcher(interval_sec: int = 120, expected: int = 0, dry_run: bool = False) -> asyncio.Task:
    """启动后台巡检任务（供 app.main 集成）。"""
    return asyncio.create_task(_watch_loop(interval_sec, expected, dry_run))


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(description="MCP 孤儿进程清理器")
    parser.add_argument("--watch", action="store_true", help="常驻巡检模式")
    parser.add_argument("--interval", type=int, default=120, help="巡检间隔秒数")
    parser.add_argument("--expect", type=int, default=0, help="期望的最大 MCP 进程数")
    parser.add_argument("--dry-run", action="store_true", help="只报告不清理")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.watch:
        asyncio.run(_watch_loop(args.interval, args.expect, args.dry_run))
    else:
        killed = reap_orphans(args.expect, args.dry_run)
        print(f"清理完成：杀掉 {killed} 个孤儿进程")


if __name__ == "__main__":
    main()