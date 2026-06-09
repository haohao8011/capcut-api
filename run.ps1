<#
  capcut-draft 一键运行脚本
  用法：
    .\run.ps1                                   # 默认：跳过 ASR
    .\run.ps1 -WithAsr                          # 跑 ASR
    .\run.ps1 -MainPath other.mp4 -DraftName XX  # 自定义
#>

param(
    [string]$MainPath = "inputs\digital_human.mp4",
    [string]$BrollDir = "inputs\broll",
    [string]$OutDir = "outputs",
    [string]$DraftName = "AI合成",
    [switch]$WithAsr = $false
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