@echo off
chcp 65001 >nul
setlocal

title AgentMate PM Daemon

rem PM 工作台守护进程 —— 独立于 bot 主进程跑。
rem 为什么不并进 app.main：这里的 3 秒轮询与每日全量扫描（5-7 分钟）会和群消息
rem 应答抢 CPU 与 lark-cli 子进程，一次全量扫描能把应答拖到超时。
rem 两者共用 .threads.jsonl / .watch_state.json，但生命周期各自独立。

set PROJECT_DIR=%~dp0..

rem ── 代理绕过（与 start-bot.bat 同口径）──
rem 不注入会让 lark-cli 与 aigw 网关请求经系统代理出口，触发 IP 白名单 403。
set NO_PROXY=localhost,127.0.0.1,10.*,192.168.*,172.16.*,172.17.*,172.18.*,172.19.*,172.20.*,172.21.*,172.22.*,172.23.*,172.24.*,172.25.*,172.26.*,172.27.*,172.28.*,172.29.*,172.30.*,172.31.*,aigw.example.com,example.com,example.com,example.com,example.com,example.com,example.com,feishu.cn,larksuite.com
set no_proxy=%NO_PROXY%

cd /d "%PROJECT_DIR%"

echo ============================================
echo  PM 工作台守护进程
echo  日志: logs/pm_daemon.log
echo ============================================
echo.
echo 前提：
echo   1. config.toml [projects.pm] 已填 identity / outputs / push
echo   2. lark-cli 已登录（读消息走 user 身份：lark-cli auth login）
echo   3. FEISHU_APP_SECRET / LLM_API_KEY 在 .env 里
echo.

rem 未启用时 pm_run 以 2 退出，这里如实反映，不做「看起来在跑」的假象
python -m app.domains.pm.pm_run
set RC=%ERRORLEVEL%
if %RC% neq 0 (
    echo.
    echo [退出] 守护进程未启动（exit=%RC%），原因见上方日志。
)
pause
