@echo off
REM ===================================================================
REM   capcut-draft 客户端 · 启动
REM
REM   首次使用：先双击 install-client.bat
REM   之后每次：双击本文件
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

set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set NO_COLOR=1

REM 把所有参数透传（这样 start-client.bat --wizard / --reset 都能用）
"%PY%" -m capcut_draft.client %* 1>"%LOG_FILE%" 2>&1
echo.
echo ========================================
echo   Client stopped. Log: %LOG_FILE%
echo ========================================
pause
