@echo off
REM ===================================================================
REM   capcut-draft 客户端 · 启动
REM
REM   首次使用：先双击 install-client.bat
REM   之后每次：双击本文件
REM
REM   透传参数：
REM     start-client.bat                  正常启动
REM     start-client.bat --wizard         跑首次配对向导
REM     start-client.bat --reset          清 credentials.json 重配
REM     start-client.bat --no-ui          只跑后台 worker（无人值守）
REM ===================================================================

setlocal
cd /d "%~dp0"

set VENV=.venv-client
set PY=%VENV%\Scripts\python.exe
set LOG_FILE=%~dp0client.log

if not exist "%PY%" (
    echo [ERROR] 客户端没装，请先双击 install-client.bat
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set NO_COLOR=1

"%PY%" -m capcut_draft_client %* 1>"%LOG_FILE%" 2>&1
echo.
echo ========================================
echo   Client stopped. Log: %LOG_FILE%
echo ========================================
pause
