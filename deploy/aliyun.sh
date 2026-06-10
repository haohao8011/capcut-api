#!/usr/bin/env bash
# 一键部署 capcut-draft 到 Ubuntu 22.04（阿里云 ECS 适用）
#
# 用法（在阿里云服务器上，root 或 sudo）：
#   sudo bash deploy/aliyun.sh
#
# 部署完成后：
#   - 监听 127.0.0.1:8000（gunicorn + uvicorn worker）
#   - systemd 服务：capcut-draft（开机自启，自动重启）
#   - 日志：journalctl -u capcut-draft -f
#   - 数据：./data/capcut.db（用户表 + 任务记录）
#   - 草稿：./outputs/
#   - 监听端口：8000（不备案也能用，IP:8000 访问）
#
# 安全组（阿里云控制台）：放行 22(SSH) + 8000(Web)

set -e

# ===== 可调参数 =====
APP_DIR="/opt/capcut-draft"
APP_USER="root"            # 简单起见用 root，公司内网无所谓
PORT=8000
PYTHON="python3.11"
REPO_URL="https://github.com/xiaoma/capcut-api.git"  # 改成你自己的 git 地址
# 如果还没推到 git，可以先在本地 `rsync -avz` 上传，再注释 git clone 那行

# ===== 1. 系统依赖 =====
echo "==> [1/6] 装系统依赖"
apt-get update -qq
apt-get install -y --no-install-recommends \
    "$PYTHON" "$PYTHON-venv" "$PYTHON-dev" \
    ffmpeg \
    git curl wget \
    nginx certbot python3-certbot-nginx \
    build-essential libffi-dev libssl-dev
echo "    OK"

# ===== 2. 拉代码 =====
echo "==> [2/6] 拉代码到 $APP_DIR"
if [ ! -d "$APP_DIR" ]; then
    git clone "$REPO_URL" "$APP_DIR"
    # 或者用 rsync 同步本地代码：rsync -avz --exclude='.venv' --exclude='data' ./ root@<IP>:/opt/capcut-draft/
fi
cd "$APP_DIR"
echo "    OK ($(git rev-parse --short HEAD 2>/dev/null || echo 'no-git'))"

# ===== 3. venv + 依赖 =====
echo "==> [3/6] 装 Python 依赖（venv）"
if [ ! -d ".venv" ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# 阿里云镜像加速（可选）
# .venv/bin/pip install -q -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
echo "    OK"

# ===== 4. 数据目录 + 默认管理员 seed =====
echo "==> [4/6] 初始化 data/ 目录（SQLite + 默认管理员 xiaoma）"
mkdir -p data outputs uploads/main uploads/broll config
# 让服务先跑一次来 seed 管理员
.venv/bin/python -c "
import sys
sys.path.insert(0, 'src')
from capcut_draft import auth
auth.init_db()
if auth.seed_admin():
    print('    已 seed 默认管理员 xiaoma/niubi666 — 首次登录后请改密！')
else:
    print('    管理员 xiaoma 已存在')
"

# ===== 5. systemd service =====
echo "==> [5/6] 写 systemd service（开机自启）"
cat > /etc/systemd/system/capcut-draft.service <<'EOF'
[Unit]
Description=capcut-draft - digital human + B-roll -> JianYing draft
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/capcut-draft
Environment="PYTHONPATH=/opt/capcut-draft/src"
Environment="NO_COLOR=1"
Environment="PYTHONIOENCODING=utf-8"
Environment="PYTHONUNBUFFERED=1"
# 重要：生产部署必须改这个值！用 openssl rand -hex 32 生成
Environment="CAPCUT_JWT_SECRET=PLEASE-CHANGE-ME-WITH-openssl-rand-hex-32"
ExecStart=/opt/capcut-draft/.venv/bin/gunicorn \
    -k uvicorn.workers.UvicornWorker \
    --bind 127.0.0.1:8000 \
    --workers 2 \
    --timeout 600 \
    --access-logfile - \
    --error-logfile - \
    capcut_draft.web:app
Restart=always
RestartSec=5
StandardOutput=append:/var/log/capcut-draft.log
StandardError=append:/var/log/capcut-draft.err

[Install]
WantedBy=multi-user.target
EOF

# 生成新 JWT 密钥（覆盖默认）
NEW_SECRET=$(openssl rand -hex 32)
sed -i "s|Environment=\"CAPCUT_JWT_SECRET=.*\"|Environment=\"CAPCUT_JWT_SECRET=$NEW_SECRET\"|" /etc/systemd/system/capcut-draft.service
echo "    OK（JWT 密钥已随机生成，存在 /etc/systemd/system/capcut-draft.service）"

# ===== 6. 启动 =====
echo "==> [6/6] 启动 + 开机自启"
systemctl daemon-reload
systemctl enable capcut-draft
systemctl restart capcut-draft
sleep 2
systemctl status capcut-draft --no-pager | head -10

# ===== 7. 防火墙提示 =====
echo ""
echo "============================================================"
echo " ✅ 部署完成！"
echo "============================================================"
echo ""
echo " 访问：http://<你的阿里云公网IP>:8000/"
echo " 首次登录：xiaoma / niubi666 （登录后立即改密！）"
echo ""
echo " 常用命令："
echo "   sudo systemctl status capcut-draft    # 看状态"
echo "   sudo systemctl restart capcut-draft   # 重启"
echo "   sudo journalctl -u capcut-draft -f    # 实时日志"
echo "   tail -f /var/log/capcut-draft.log     # access log"
echo ""
echo " 阿里云安全组（控制台）：放行 22(SSH) + 8000(TCP)"
echo " 当前安全组规则："
ss -tlnp | grep -E ':8000\b' || echo "  ! 端口未监听，看 systemctl status"
echo "============================================================"
