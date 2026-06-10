@echo off
REM ===================================================================
REM   capcut-draft one-click start (double-click to run)
REM
REM   Steps:
REM     1. Check / create venv
REM     2. Check & install deps if needed
REM     3. Start uvicorn in background (default port 8000)
REM     4. Wait until ready
REM     5. Open browser
REM
REM   Stop: double-click stop.bat
REM ===================================================================

setlocal

cd /d "%~dp0"

set PORT=8000
if not "%1"=="" set PORT=%1
set VENV=.venv
set PY=%VENV%\Scripts\python.exe
set LOG_FILE=%~dp0server.log
set WAIT_SCRIPT=%~dp0wait_port.ps1

echo ========================================
echo   capcut-draft one-click start
echo   Port: %PORT%
echo ========================================
echo.

REM --- 1. venv ---
echo [1/4] Checking venv ...
if exist "%PY%" goto VENV_OK
where py >nul 2>nul
if errorlevel 1 goto NO_PY
echo       Creating venv ...
py -3.11 -m venv "%VENV%"
if errorlevel 1 goto ERR_VENV
:VENV_OK
echo       OK

REM --- 2. deps ---
echo [2/4] Checking dependencies ...
"%PY%" -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS
"%PY%" -c "import pyJianYingDraft" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS
goto DEPS_OK
:INSTALL_DEPS
echo       Installing deps (first run ~1-2 min) ...
"%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 goto ERR_DEPS
:DEPS_OK
echo       OK

REM --- 3. start ---
echo [3/4] Starting service on port %PORT% ...

REM kill old instance if any
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING 2^^^>nul') do (
    echo       Killing old PID %%P on port %PORT%
    taskkill /F /PID %%P >nul 2>nul
)

REM start in background
REM NO_COLOR=1 关掉 ANSI 颜色码（cmd 默认不解释，会打成方块）
REM PYTHONIOENCODING=utf-8 强制 UTF-8 输出，避免中文/emoji 乱码
REM PYTHONUNBUFFERED=1 让 print/日志立刻刷出来，不缓冲
set PYTHONPATH=src
set NO_COLOR=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
start "capcut-draft" /MIN "%PY%" -m uvicorn capcut_draft.web:app --host 0.0.0.0 --port %PORT% 1>"%LOG_FILE%" 2>&1

REM --- 4. wait ---
echo [4/4] Waiting for service ...

REM write a tiny powershell waiter
> "%WAIT_SCRIPT%" echo $ErrorActionPreference = 'SilentlyContinue'
>>"%WAIT_SCRIPT%" echo for ($i = 0; $i -lt 30; $i++) {
>>"%WAIT_SCRIPT%" echo     Start-Sleep -Seconds 1
>>"%WAIT_SCRIPT%" echo     $c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue
>>"%WAIT_SCRIPT%" echo     if ($c) { exit 0 }
>>"%WAIT_SCRIPT%" echo }
>>"%WAIT_SCRIPT%" echo exit 1

powershell -NoProfile -ExecutionPolicy Bypass -File "%WAIT_SCRIPT%"
if errorlevel 1 goto ERR_TIMEOUT

echo.
echo ========================================
echo   Service ready: http://localhost:%PORT%/
echo   API docs:     http://localhost:%PORT%/docs
echo   Log file:     %LOG_FILE%
echo   Stop:         double-click stop.bat
echo ========================================
echo.

del "%WAIT_SCRIPT%" >nul 2>nul

start "" "http://localhost:%PORT%/"
timeout /t 2 /nobreak >nul
exit /b 0

REM ============== error handlers ==============
:NO_PY
echo [ERROR] py.exe not found, install Python 3.10-3.12 first
pause
exit /b 1

:ERR_VENV
echo [ERROR] Failed to create venv
pause
exit /b 1

:ERR_DEPS
echo [ERROR] Failed to install deps, check network
pause
exit /b 1

:ERR_TIMEOUT
if exist "%WAIT_SCRIPT%" del "%WAIT_SCRIPT%" >nul 2>nul
echo [ERROR] Timeout, check log:
type "%LOG_FILE%"
pause
exit /b 1