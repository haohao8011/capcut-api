@echo off
REM ===================================================================
REM   Stop capcut-draft web service
REM ===================================================================

setlocal
cd /d "%~dp0"

set PORT=8000
if not "%1"=="" set PORT=%1

echo Stopping service on port %PORT% ...

set FOUND=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo   Killing PID %%P ...
    taskkill /F /PID %%P >nul 2>nul
    if not errorlevel 1 set FOUND=1
)

if "%FOUND%"=="1" (
    echo   Stopped.
) else (
    echo   No service running on port %PORT%.
)

pause
exit /b 0