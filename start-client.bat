@echo off
REM ===================================================================
REM   capcut-draft 客户端启动（双击运行）
REM
REM   第一次运行：
REM     1. 复制 config\client.example.yaml 为 config\client.yaml
REM     2. 修改 server.url 和 client_token（去服务端注册拿）
REM     3. 再双击本文件
REM
REM   停止：直接关掉黑色窗口，或双击 stop-client.bat
REM ===================================================================

setlocal

cd /d "%~dp0"

set VENV=.venv
set PY=%VENV%\Scripts\python.exe
set CONFIG=config\client.yaml
set LOG_FILE=%~dp0client.log

echo ========================================
echo   capcut-draft 客户端启动
echo   Config: %CONFIG%
echo   Log:    %LOG_FILE%
echo ========================================
echo.

REM --- 1. venv ---
if not exist "%PY%" (
    echo [ERROR] venv 不存在，请先双击 start.bat 装服务端
    pause
    exit /b 1
)

REM --- 2. config ---
if not exist "%CONFIG%" (
    echo [ERROR] 客户端配置不存在: %CONFIG%
    echo         请先复制 config\client.example.yaml 为 %CONFIG% 并填好
    pause
    exit /b 1
)

REM --- 3. deps ---
"%PY%" -c "import httpx, fastapi, yaml" >nul 2>nul
if errorlevel 1 (
    echo       Installing client deps ...
    "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple httpx pyyaml fastapi uvicorn >nul
)

REM --- 4. 启动 ---
REM PYTHONPATH=src 让 python -m capcut_draft.client 找得到包
REM PYTHONIOENCODING=utf-8 防中文乱码
REM PYTHONUNBUFFERED=1 日志立即刷出
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set NO_COLOR=1

echo Starting client ...
"%PY%" -m capcut_draft.client -c "%CONFIG%" 1>"%LOG_FILE%" 2>&1
echo.
echo ========================================
echo   Client stopped. Log: %LOG_FILE%
echo ========================================
pause
