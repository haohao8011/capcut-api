<#
  capcut-draft 一键运行脚本
  用法：
    # CLI 模式
    .\run.ps1                                   # 默认：跳过 ASR
    .\run.ps1 -WithAsr                          # 跑 ASR
    .\run.ps1 -MainPath other.mp4 -DraftName XX  # 自定义

    # Web 服务模式
    .\run.ps1 -Serve                            # 启动 API，监听 8000
    .\run.ps1 -Serve -Port 9000                 # 改端口
    .\run.ps1 -Serve -Host 127.0.0.1            # 只允许本机
#>

param(
    [string]$MainPath = "inputs\digital_human.mp4",
    [string]$BrollDir = "inputs\broll",
    [string]$OutDir = "outputs",
    [string]$DraftName = "AI合成",
    [switch]$WithAsr = $false,
    [switch]$Serve = $false,
    [string]$Host = "0.0.0.0",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# 切到脚本所在目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 用 venv 里的 Python
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "找不到 venv: $py  请先建: py -3.11 -m venv .venv"
}

# 设 PYTHONPATH 指向 src
$env:PYTHONPATH = "src"

if ($Serve) {
    Write-Host "==> 启动 capcut-draft Web 服务" -ForegroundColor Cyan
    Write-Host "    监听: http://${Host}:${Port}" -ForegroundColor Cyan
    Write-Host "    页面: http://localhost:${Port}/" -ForegroundColor Cyan
    Write-Host "    API 文档: http://localhost:${Port}/docs" -ForegroundColor Cyan
    Write-Host "    Ctrl+C 退出" -ForegroundColor Gray
    & $py -m uvicorn capcut_draft.web:app --host $Host --port $Port
    exit $LASTEXITCODE
}

# 拼参数（不能用 $args，PowerShell 保留）
$cliArgs = @(
    "-m", "capcut_draft.cli",
    "--main", $MainPath,
    "--broll", $BrollDir,
    "--out", $OutDir,
    "--name", $DraftName
)
if (-not $WithAsr) {
    $cliArgs += "--skip-asr"
}

# 跑
& $py @cliArgs