@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title AgentMate Bot Launcher

echo ============================================
echo  AgentMate Bot - 一键启动
echo ============================================

rem 本脚本在 scripts/ 下，仓库根 = 脚本目录的上一级（换机器/换路径都不用改）
set PROJECT_DIR=%~dp0..

rem ── 代理绕过（2026-09-13 修复「API Error: Connection refused」）──
rem 根因：Windows 系统代理(127.0.0.1:7897)开启时，httpx/openai SDK 会读注册表
rem 系统代理（urllib getproxies），aigw 网关请求经代理出口 IP 发出，
rem 触发 aigw 令牌 IP 白名单 403（access_denied）。NO_PROXY 让公司域名直连。
rem 注意：Python 的 urllib NO_PROXY 不支持通配后缀，须逐个列全公司域名。
set NO_PROXY=localhost,127.0.0.1,10.*,192.168.*,172.16.*,172.17.*,172.18.*,172.19.*,172.20.*,172.21.*,172.22.*,172.23.*,172.24.*,172.25.*,172.26.*,172.27.*,172.28.*,172.29.*,172.30.*,172.31.*,aigw.example.com,example.com,example.com,example.com,example.com,example.com,example.com,feishu.cn,larksuite.com,cmdb.example.com
rem cmdb.example.com 是 ingest 沉淀的内网 CMDB（.local 后缀不被
rem example.com 覆盖，urllib NO_PROXY 无通配后缀，须显式列全——A2A 服务继承本 env）
set no_proxy=%NO_PROXY%
echo [代理] NO_PROXY 已注入（aigw/公司域名/内网直连，绕过系统代理）

echo [前置] 清理 MCP 孤儿进程（nacos-mcp 泄漏防护）...
echo   清理 nacos-mcp 整棵进程树（cmd/npx/node/nacos-mcp.exe）...
wmic process where "commandline like '%%vinkugor%%' or commandline like '%%nacos-mcp%%'" call terminate >nul 2>&1
taskkill /F /IM nacos-mcp.exe >nul 2>&1
taskkill /F /IM node.exe /FI "WINDOWTITLE eq nacos*" >nul 2>&1
timeout /t 2 /nobreak >nul
tasklist /FI "IMAGENAME eq nacos-mcp.exe" 2>nul | find /I "nacos-mcp" >nul
if !errorlevel! equ 0 (
    echo   ⚠️ 仍有残留 nacos-mcp，尝试二次清理...
    wmic process where "commandline like '%%vinkugor%%' or commandline like '%%nacos-mcp%%'" call terminate >nul 2>&1
    taskkill /F /IM nacos-mcp.exe >nul 2>&1
    timeout /t 1 /nobreak >nul
) else (
    echo   ✅ MCP 孤儿进程已清理
)
echo.

echo [1/5] 检查 MemoryCore Gateway (8420)...
curl -s http://127.0.0.1:8420/health >nul 2>&1
if !errorlevel! equ 0 (
    echo   ✅ MemoryCore Gateway 已在运行
) else (
    echo   ⚠️ MemoryCore Gateway 未运行，正在代为启动（最小化窗口后台跑）...
    start /min "MemoryCore" cmd /c "cd /d %USERPROFILE%\IdeaProjects\tdai-agent-memory\MemoryCore && npx tsx src/gateway/server.ts"
    echo   等待 MemoryCore 就绪（最多 18 秒）...
    set /a MEM_TRIES=0
    :wait_memcore
    timeout /t 3 /nobreak >nul
    curl -s http://127.0.0.1:8420/health >nul 2>&1
    if !errorlevel! equ 0 (
        echo   ✅ MemoryCore Gateway 已启动
    ) else (
        set /a MEM_TRIES+=1
        if !MEM_TRIES! lss 6 goto wait_memcore
        echo   ❌ MemoryCore 18 秒内未就绪，请手动检查：
        echo      cd /d "%USERPROFILE%\IdeaProjects\tdai-agent-memory\MemoryCore"
        echo      npx tsx src/gateway/server.ts
        echo   ⏩ 继续启动其他组件...
    )
)

echo.
echo [2/5] 检查 cc-connect (9111)...
curl -s -X POST http://127.0.0.1:9111/hook >nul 2>&1
if !errorlevel! equ 0 (
    echo   ✅ cc-connect 已在运行
) else (
    echo   ⚠️ cc-connect 未运行，正在启动...
    rem 2026-09-14：改用执行通道专用配置 cc-exec.toml（feishu 平台占位，不与 feishu-agent-platform 抢飞书消息）
    start "cc-connect" cmd /c "cc-connect --config D:\Users\devuser\.cc-connect\cc-exec.toml"
    echo   等待 cc-connect 启动...
    timeout /t 5 /nobreak >nul
    curl -s -X POST http://127.0.0.1:9111/hook >nul 2>&1
    if !errorlevel! equ 0 (
        echo   ✅ cc-connect 启动成功
    ) else (
        echo   ❌ cc-connect 启动失败，请检查 config.toml
    )
)

echo.
echo [3/5] 检查 webportal-order-agent A2A (8080)...
rem 工单→SQL 引擎是独立 A2A 服务（v3.0.0 去 vendor），bot 的工单能力依赖它；
rem Agent Card 可发现 = 服务就绪（Bot 启动后的健康探测也打这个端点）
curl -s http://127.0.0.1:8080/.well-known/agent-card.json >nul 2>&1
if !errorlevel! equ 0 (
    echo   ✅ webportal A2A 服务已在运行
) else (
    echo   ⚠️ webportal A2A 服务未运行，正在启动...
    start "webportal-order-agent A2A" cmd /c "cd /d D:\Projects\vscode-projects\webportal-order-agent && python -m order_agent.a2a_server"
    echo   等待 A2A 服务就绪（最多 15 秒）...
    set /a A2A_TRIES=0
    :wait_a2a
    timeout /t 3 /nobreak >nul
    curl -s http://127.0.0.1:8080/.well-known/agent-card.json >nul 2>&1
    if !errorlevel! equ 0 (
        echo   ✅ webportal A2A 服务已启动
    ) else (
        set /a A2A_TRIES+=1
        if !A2A_TRIES! lss 5 goto wait_a2a
        echo   ❌ A2A 服务 15 秒内未就绪（工单能力将红探测），手动检查：
        echo      cd /d "D:\Projects\vscode-projects\webportal-order-agent" ^&^& python -m order_agent.a2a_server
    )
)

echo.
echo [4/5] 启动 Bot 机器人...
echo.
cd /d "%PROJECT_DIR%"
echo   使用系统 python 执行: python -m app.main
echo.
echo   ⏳ Bot 正在后台启动，请稍候...
echo   ============================================
echo.
rem 控制台窗口仍保留实时输出；同时由进程内的 RotatingFileHandler 落 logs/bot/app.log
start "AgentMate Bot" cmd /c "python -m app.main"

echo   等待 Bot 启动完成...
timeout /t 5 /nobreak >nul

echo.
echo [5/5] 检查 Dashboard (9112)...
curl -s http://127.0.0.1:9112/ >nul 2>&1
if !errorlevel! equ 0 (
    echo   ✅ Dashboard 已在运行 (http://127.0.0.1:9112)
) else (
    echo   ⚠️ Dashboard 尚未就绪（Bot 启动后由 asyncio task 自动加载）
)

echo.
echo ============================================
echo  📋 启动摘要
echo ============================================
echo.

rem 检查 MemoryCore 状态
curl -s http://127.0.0.1:8420/health >nul 2>&1
if !errorlevel! equ 0 ( set MEMORY_STATUS=✅ 运行中 ) else ( set MEMORY_STATUS=❌ 未运行 )

rem 检查 cc-connect 状态
curl -s -X POST http://127.0.0.1:9111/hook >nul 2>&1
if !errorlevel! equ 0 ( set CC_STATUS=✅ 运行中 ) else ( set CC_STATUS=❌ 未运行 )

rem 检查 webportal A2A 状态
curl -s http://127.0.0.1:8080/.well-known/agent-card.json >nul 2>&1
if !errorlevel! equ 0 ( set A2A_STATUS=✅ 运行中 ) else ( set A2A_STATUS=❌ 未运行 )

rem 检查 Dashboard 状态
curl -s http://127.0.0.1:9112/ >nul 2>&1
if !errorlevel! equ 0 ( set DASH_STATUS=✅ 运行中 ) else ( set DASH_STATUS=⚠️ 启动中 )

echo  MemoryCore Gateway  !MEMORY_STATUS!
echo  cc-connect          !CC_STATUS!
echo  webportal A2A       !A2A_STATUS!
echo  Dashboard           !DASH_STATUS!
echo  Bot                 ✅ 已在后台运行

echo.
echo  Dashboard 地址: http://127.0.0.1:9112
echo.

echo ============================================
echo  Bot 已在后台运行，关闭此窗口不影响后台进程。
echo.
echo  查看 Bot 日志: 切换到标题为 "AgentMate Bot" 的窗口
echo  重启 Bot:      cd /d "%PROJECT_DIR%" ^&^& python -m app.main
echo  查看 cc-connect 日志: cc-connect daemon logs -f
echo.
echo  后台进程不受 Bot 停止影响：
echo    - cc-connect (9111)  持续运行
echo    - webportal A2A (8080) 持续运行（工单→SQL 引擎，独立仓库 webportal-order-agent）
echo    - Dashboard (9112)   持续运行
echo    - MemoryCore (8420)  持续运行
echo ============================================
echo.
pause