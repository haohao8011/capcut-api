@echo off
REM capcut-draft 一键运行（cmd 版）

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
