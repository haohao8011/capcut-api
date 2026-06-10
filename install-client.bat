@echo off
REM ===================================================================
REM   capcut-draft 客户端 · 首次安装（员工机器只点这一次）
REM
REM   步骤：
REM     1. 检测 Python 3.11+
REM     2. 创建 .venv-client\  装轻量客户端依赖
REM     3. 装好直接调用 --wizard，弹窗输 URL + 6 位安装码
REM     4. 装完以后双击 start-client.bat 即可
REM
REM   之后每次启动：双击 start-client.bat
REM ===================================================================

setlocal
cd /d "%~dp0"

set VENV=.venv-client
set PY=%VENV%\Scripts\python.exe

echo ========================================
echo   capcut-draft 客户端 · 首次安装
echo ========================================
echo.

REM --- 1. Python ---
where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] 没找到 Python。请先装 Python 3.11+：
        echo         https://www.python.org/downloads/windows/
        echo         装的时候勾上 "Add Python to PATH"
        pause
        exit /b 1
    )
    set PY_CMD=python
) else (
    set PY_CMD=py -3
)

%PY_CMD% -c "import sys; assert sys.version_info >= (3, 11), 'need 3.11+'" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.11+ 才能跑这个客户端
    %PY_CMD% --version
    pause
    exit /b 1
)

echo       Python: 
%PY_CMD% --version

REM --- 2. venv ---
if not exist "%PY%" (
    echo       Creating venv %VENV%\...
    %PY_CMD% -m venv %VENV%
    if errorlevel 1 (
        echo [ERROR] 建 venv 失败
        pause
        exit /b 1
    )
)

REM --- 3. 装客户端依赖（只装 [client] 套件，不装服务端/gunicorn/etc.） ---
echo       Installing client deps ...
"%PY%" -m pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
"%PY%" -m pip install -e .[client] -q -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [ERROR] pip install 失败
    pause
    exit /b 1
)
echo       Installed OK

echo.
echo ========================================
echo   安装完成！下面开始首次配置
echo ========================================
echo.
echo   找管理员拿一个 6 位安装码
echo   准备好后按任意键继续...
pause >nul

REM --- 4. wizard ---
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

"%PY%" -m capcut_draft.client --wizard
if errorlevel 1 (
    echo.
    echo [WARN] wizard 没跑通，可以重试：
    echo        start-client.bat --wizard
    pause
    exit /b 1
)

echo.
echo ========================================
echo   ✅ 客户端已就绪
echo   以后每次启动：双击 start-client.bat
echo   本地 UI: http://127.0.0.1:8001/
echo ========================================
echo.
echo   现在按任意键启动客户端...
pause >nul

start "" "%~dp0start-client.bat"
exit /b 0
