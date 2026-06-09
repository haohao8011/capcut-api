@echo off
REM capcut-draft 一键运行（cmd 版）
REM
REM 用法：
REM   run.bat                                 CLI 模式（跳过 ASR）
REM   run.bat serve                           启动 Web 服务（端口 8000）
REM   run.bat serve 9000                      改端口
REM   run.bat main.mp4 broll_dir out name     CLI 自定义路径

if "%1"=="serve" goto :serve

set MAIN=%1
if "%MAIN%"=="" set MAIN=inputs\digital_human.mp4
set BROLL=%2
if "%BROLL%"=="" set BROLL=inputs\broll
set OUT=%3
if "%OUT%"=="" set OUT=outputs
set NAME=%4
if "%NAME%"=="" set NAME=AI合成

if not exist .venv\Scripts\python.exe (
    echo 找不到 venv，请先建：py -3.11 -m venv .venv
    exit /b 1
)

set PYTHONPATH=src
.\.venv\Scripts\python.exe -m capcut_draft.cli --main "%MAIN%" --broll "%BROLL%" --out "%OUT%" --name "%NAME%" --skip-asr
exit /b %ERRORLEVEL%

:serve
set PORT=%2
if "%PORT%"=="" set PORT=8000

if not exist .venv\Scripts\python.exe (
    echo 找不到 venv，请先建：py -3.11 -m venv .venv
    exit /b 1
)

set PYTHONPATH=src
echo 启动 capcut-draft Web 服务，端口 %PORT%
.\.venv\Scripts\python.exe -m uvicorn capcut_draft.web:app --host 0.0.0.0 --port %PORT%
