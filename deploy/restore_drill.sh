#!/usr/bin/env bash
# ===================================================================
#   capcut-draft 备份恢复演练脚本（建议每月手动跑一次）
#
#   流程：
#     1. 找最新的 capcut.db.*.gz 备份
#     2. 解压到临时目录
#     3. 跑 SQLite integrity_check
#     4. 查关键表的记录数
#     5. 报告 PASS / FAIL
#     6. 清理临时文件
#
#   用法（生产服务器上）：
#     sudo /opt/capcut-draft/deploy/restore_drill.sh
#   或指定备份目录：
#     BACKUP_DIR=/var/backups/capcut /opt/capcut-draft/deploy/restore_drill.sh
# ===================================================================
set -uo pipefail

# 备份目录（默认 /var/backups/capcut；aliyun-server.sh 里如果改了这里也改）
BACKUP_DIR="${BACKUP_DIR:-/var/backups/capcut}"
DATA_DIR="${CAPCUT_DRAFTS_DIR_PARENT:-/var/lib/capcut-draft}"

PASS=0
FAIL=0
RESULTS=()

check() {
    local name="$1"
    local ok="$2"
    local extra="${3:-}"
    if [ "$ok" = "1" ]; then
        PASS=$((PASS+1))
        RESULTS+=("  [PASS] $name${extra:+ — $extra}")
    else
        FAIL=$((FAIL+1))
        RESULTS+=("  [FAIL] $name${extra:+ — $extra}")
    fi
}

echo "=========================================="
echo " capcut-draft 备份恢复演练"
echo " 时间: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " 备份目录: $BACKUP_DIR"
echo "=========================================="

# 1. 找最新备份
if [ ! -d "$BACKUP_DIR" ]; then
    check "备份目录存在" 0 "$BACKUP_DIR 不存在"
    echo
    for r in "${RESULTS[@]}"; do echo "$r"; done
    echo "=========================================="
    echo " 0 pass / 1 fail"
    exit 1
fi
LATEST=$(ls -1t "$BACKUP_DIR"/capcut.db.*.gz 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    check "存在 .gz 备份文件" 0 "$BACKUP_DIR 下无 capcut.db.*.gz"
    echo
    for r in "${RESULTS[@]}"; do echo "$r"; done
    echo "=========================================="
    echo " 0 pass / 1 fail"
    exit 1
fi
check "找到最新备份" 1 "$(basename $LATEST) ($(stat -c %s $LATEST 2>/dev/null || stat -f %z $LATEST) bytes)"

# 2. 解压到临时目录
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
TMPDB="$TMPDIR/capcut.db"
if ! gunzip -c "$LATEST" > "$TMPDB" 2>/dev/null; then
    check "解压备份" 0 "gunzip 失败"
else
    check "解压备份" 1 "→ $TMPDB"
fi

# 3. SQLite 完整性检查
if command -v sqlite3 >/dev/null 2>&1; then
    INTEGRITY=$(sqlite3 "$TMPDB" "PRAGMA integrity_check;" 2>&1)
    if [ "$INTEGRITY" = "ok" ]; then
        check "SQLite 完整性" 1 "integrity_check=ok"
    else
        check "SQLite 完整性" 0 "$INTEGRITY"
    fi
else
    check "SQLite 完整性" 0 "sqlite3 命令未安装，跳过"
fi

# 4. 用 Python 查关键表记录数（不依赖 sqlite3 命令）
TABLE_CHECK=$(python3 - <<PYEOF 2>&1
import sqlite3
try:
    conn = sqlite3.connect("$TMPDB")
    cur = conn.cursor()
    tables = ["users", "clients", "drafts", "tasks"]
    for t in tables:
        try:
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"{t}={n}", end=" ")
        except sqlite3.OperationalError as e:
            print(f"{t}=MISSING({e})", end=" ")
    conn.close()
except Exception as e:
    print(f"ERROR: {e}")
PYEOF
)
check "关键表可读" 1 "$TABLE_CHECK"

# 5. 校验 .env 的 DB URL 路径
if [ -f "$DATA_DIR/capcut.db" ]; then
    PROD_SIZE=$(stat -c %s "$DATA_DIR/capcut.db" 2>/dev/null || stat -f %z "$DATA_DIR/capcut.db")
    BACKUP_SIZE=$(stat -c %s "$TMPDB" 2>/dev/null || stat -f %z "$TMPDB")
    RATIO=$(python3 -c "print(f'{$BACKUP_SIZE/$PROD_SIZE:.1%}' if $PROD_SIZE else 'N/A')" 2>/dev/null || echo "N/A")
    check "备份非空且合理" 1 "生产=${PROD_SIZE}B 备份=${BACKUP_SIZE}B（${RATIO}）"
else
    check "生产 DB 存在" 0 "$DATA_DIR/capcut.db 不存在"
fi

# 总结
echo
echo "----------------------------------------"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo "----------------------------------------"
TOTAL=$((PASS+FAIL))
echo " 总结: $PASS pass / $FAIL fail (total $TOTAL)"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
    echo
    echo "!! 备份恢复演练失败，请检查："
    echo "  1. 备份 cron 还在跑？→ crontab -l | grep capcut"
    echo "  2. 备份文件是否损坏？→ gunzip -t $LATEST"
    echo "  3. 磁盘是否写满？→ df -h $BACKUP_DIR"
    exit 1
fi

echo
echo "OK，备份可恢复。建议每月跑一次："
echo "  echo '0 3 1 * *  root  /opt/capcut-draft/deploy/restore_drill.sh >> /var/log/capcut-drill.log 2>&1' >> /etc/crontab"
exit 0
