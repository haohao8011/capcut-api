<#
  capcut-draft 一键运行脚本
  用法：
    .\run.ps1                                   # 跳过 ASR（用 --skip-asr）
    .\run.ps1 -MainPath inputs\demo.mp4         # 指定主视频
    .\run.ps1 -MainPath inputs\demo.mp4 -WithAsr # 跑 ASR
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
    Write-Error "找不到 venv: $py`n先建 venv: py -3.11 -m venv .venv  并安装依赖"
}

# 设 PYTHONPATH 指向 src
$env:PYTHONPATH = "src"

# 拼参数
$args = @(
    "-m", "capcut_draft.cli",
    "--main", $MainPath,
    "--broll", $BrollDir,
    "--out", $OutDir,
    "--name", $DraftName
)
if (-not $WithAsr) {
    $args += "--skip-asr"
}

# 跑
& $py @args
