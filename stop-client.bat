@echo off
REM 停止客户端：关掉 UI 端口（默认 8001）对应的进程
setlocal
set PORT=8001
if not "%1"=="" set PORT=%1

echo 停止客户端（端口 %PORT%）...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING 2^^^>nul') do (
    echo   Killing PID %%P
    taskkill /F /PID %%P >nul 2>nul
)
echo Done.
timeout /t 2 /nobreak >nul
