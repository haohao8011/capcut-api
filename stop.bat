@echo off
REM 停止 capcut-draft 服务端（按端口找进程 kill）
setlocal
set PORT=8000
if not "%1"=="" set PORT=%1

echo 停止服务端（端口 %PORT%）...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING 2^^^>nul') do (
    echo   Killing PID %%P
    taskkill /F /PID %%P >nul 2>nul
)
echo Done.
timeout /t 2 /nobreak >nul
