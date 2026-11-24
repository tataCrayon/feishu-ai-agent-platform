@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title WebPortal-pilot Launcher

echo ============================================
echo  WebPortal-pilot - 启动
echo ============================================
echo.

set PROJECT_DIR=D:\Users\devuser\IdeaProjects\webportal-pilot

echo [1/2] 检查 WebPortal-pilot (8000)...
curl -s http://127.0.0.1:8000/api/health >nul 2>&1
if %errorlevel% equ 0 (
    echo   ✅ WebPortal-pilot 已在运行
) else (
    echo   ⚠️ WebPortal-pilot 未运行，正在启动...
    cd /d "%PROJECT_DIR%"
    start "WebPortal-pilot" cmd /c "uv run uvicorn app.main:app --host 127.0.0.1 --port 8000"
    echo   等待启动...
    timeout /t 5 /nobreak >nul
    curl -s http://127.0.0.1:8000/api/health >nul 2>&1
    if !errorlevel! equ 0 (
        echo   ✅ WebPortal-pilot 启动成功
    ) else (
        echo   ❌ WebPortal-pilot 启动失败，请手动检查
    )
)

echo.
echo ============================================
echo  启动完成，按任意键退出
echo ============================================
pause
