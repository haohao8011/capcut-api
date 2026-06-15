# deploy.ps1 - one-shot deploy capcut-draft to Aliyun ECS
#
# Usage (run from project root):
#   .\scripts\deploy.ps1                # default: tar + scp + untar + restart
#   .\scripts\deploy.ps1 -SkipPip       # skip pip install (faster, no dep change)
#   .\scripts\deploy.ps1 -SkipRestart   # deploy without restart
#
# Prereq:
#   - Aliyun ECS 8.129.83.166 (root)
#   - SSH key D:\Offices\三鼎.pem
#   - First-time deploy must run .\deploy\aliyun-server.sh first
#
# Safety:
#   - .env / *.db / data/ are NEVER packed or uploaded
#   - /var/lib/capcut-draft/capcut.db is untouched
#
[CmdletBinding()]
param(
    [switch]$SkipPip = $false,
    [switch]$SkipRestart = $false
)

$ErrorActionPreference = "Stop"

# -------- config --------
$PROJECT_ROOT = Resolve-Path (Join-Path $PSScriptRoot "..")
$SERVER_IP = "8.129.83.166"
$SERVER_USER = "root"
$KEY = "D:\Offices\三鼎.pem"
$REMOTE_DIR = "/opt/capcut-draft"
$TAR_NAME = "deploy.tar.gz"
$SSH = "ssh"
$SCP = "scp"
$SSH_TARGET = $SERVER_USER + "@" + $SERVER_IP

# SSH opts: skip strict host key check (first connect would prompt)
$SSH_OPTS = @("-i", $KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL", "-o", "LogLevel=ERROR")
$SCP_OPTS = @("-i", $KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL", "-o", "LogLevel=ERROR")

# PowerShell here-string uses CRLF on Windows; bash on Linux chokes on \r.
# Strip CR before sending any remote command.
function Clean-Bash($s) { return ($s -replace "`r", "") }

Write-Host "[deploy] project: $PROJECT_ROOT"
Write-Host "[deploy] target:  $SSH_TARGET : $REMOTE_DIR"
Write-Host ""

# -------- 1. tar --------
Write-Host "[1/6] tar code (exclude .venv / .env / data / *.db / .git)" -ForegroundColor Yellow
$TAR_PATH = Join-Path $PROJECT_ROOT $TAR_NAME
if (Test-Path $TAR_PATH) {
    Remove-Item $TAR_PATH -Force
}
Push-Location $PROJECT_ROOT
try {
    tar -czf $TAR_PATH `
        --exclude=".venv" `
        --exclude=".venv-client" `
        --exclude=".git" `
        --exclude=".env" `
        --exclude="__pycache__" `
        --exclude="*.pyc" `
        --exclude="*.pyo" `
        --exclude=".pytest_cache" `
        --exclude=".ruff_cache" `
        --exclude=".mypy_cache" `
        --exclude="data" `
        --exclude="*.db" `
        --exclude="*.log" `
        --exclude=".qoder" `
        --exclude="screenshots" `
        --exclude="deploy.tar.gz" `
        --exclude=".commit_msg.txt" `
        .
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
}
finally {
    Pop-Location
}
$TAR_SIZE = [math]::Round((Get-Item $TAR_PATH).Length / 1KB, 1)
Write-Host ("    OK: " + $TAR_NAME + " (" + $TAR_SIZE + " KB)") -ForegroundColor Green
Write-Host ""

# -------- 2. scp --------
Write-Host "[2/6] scp upload $TAR_NAME" -ForegroundColor Yellow
$SCP_TARGET = $SSH_TARGET + ":" + $REMOTE_DIR + "/"
& $SCP @SCP_OPTS $TAR_PATH $SCP_TARGET
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
Write-Host "    OK" -ForegroundColor Green
Write-Host ""

# -------- 3. untar + chown --------
Write-Host "[3/6] remote untar + chown capcut" -ForegroundColor Yellow
$REMOTE_CMD = @"
set -e
cd $REMOTE_DIR
tar -xzf $TAR_NAME
rm -f $TAR_NAME
chown -R capcut:capcut $REMOTE_DIR
find $REMOTE_DIR -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo '    OK: untar + chown done'
"@
& $SSH @SSH_OPTS $SSH_TARGET (Clean-Bash $REMOTE_CMD)
if ($LASTEXITCODE -ne 0) { throw "SSH untar failed" }
Write-Host ""

# -------- 4. pip install --------
if (-not $SkipPip) {
    Write-Host "[4/6] pip install -e (common + server)" -ForegroundColor Yellow
    $PIP_CMD = @"
set -e
cd $REMOTE_DIR
sudo -u capcut .venv/bin/pip install -e ./common -e ./server -i https://pypi.tuna.tsinghua.edu.cn/simple
echo '    OK: pip install done'
"@
    & $SSH @SSH_OPTS $SSH_TARGET (Clean-Bash $PIP_CMD)
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} else {
    Write-Host "[4/6] skip pip install (-SkipPip)" -ForegroundColor DarkYellow
}
Write-Host ""

# -------- 5. restart --------
if (-not $SkipRestart) {
    Write-Host "[5/6] restart capcut-server systemd" -ForegroundColor Yellow
    $RESTART_CMD = @"
set -e
systemctl restart capcut-server
sleep 2
systemctl status capcut-server --no-pager | head -8
"@
    & $SSH @SSH_OPTS $SSH_TARGET (Clean-Bash $RESTART_CMD)
    if ($LASTEXITCODE -ne 0) { throw "restart failed" }
} else {
    Write-Host "[5/6] skip restart (-SkipRestart)" -ForegroundColor DarkYellow
}
Write-Host ""

# -------- 6. health check --------
Write-Host "[6/6] health check" -ForegroundColor Yellow
$HEALTH_CMD = @"
echo '--- processes ---'
ps -ef | grep -E 'gunicorn.*capcut_draft_server' | grep -v grep | head -3
echo ''
echo '--- port 8000 ---'
ss -tlnp 2>/dev/null | grep :8000 || echo '(NOT listening)'
echo ''
echo '--- HTTP /api/auth/me ---'
curl -sS -o /dev/null -w 'HTTP %{http_code}  latency %{time_total}s\n' http://127.0.0.1:8000/api/auth/me
"@
& $SSH @SSH_OPTS $SSH_TARGET (Clean-Bash $HEALTH_CMD)
Write-Host ""
Write-Host "[deploy] DONE" -ForegroundColor Green
