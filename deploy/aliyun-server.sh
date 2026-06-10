#!/usr/bin/env bash
# ===================================================================
#   capcut-draft 服务端部署脚本 (Ubuntu/Debian on 阿里云 ECS)
#
#   一次性把服务端装起来：
#     1. apt install python3 venv nginx
#     2. clone / 拉代码 到 /opt/capcut-draft
#     3. 建 venv 装 deps
#     4. 写 systemd service (capcut-server)
#     5. nginx 反向代理 + Let's Encrypt
#     6. 跑 migrations
# ===================================================================
set -euo pipefail

APP_DIR=/opt/capcut-draft
APP_USER=capcut
SERVICE_NAME=capcut-server
DOMAIN="${1:-capcut.example.com}"   # ./deploy/aliyun-server.sh capcut.your-domain.com

echo "==> 1. apt 装基础"
apt update
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

echo "==> 2. 系统用户"
id -u $APP_USER >/dev/null 2>&1 || useradd --system --no-create-home --shell /bin/false $APP_USER

echo "==> 3. 准备目录"
mkdir -p $APP_DIR
[ -d $APP_DIR/.git ] || { echo "   !!! 请先 git clone 到 $APP_DIR 再跑本脚本"; exit 1; }
chown -R $APP_USER:$APP_USER $APP_DIR

echo "==> 4. venv + deps"
sudo -u $APP_USER python3 -m venv $APP_DIR/.venv
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install -r $APP_DIR/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
sudo -u $APP_USER $APP_DIR/.venv/bin/pip install gunicorn -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "==> 5. 初始化数据目录"
mkdir -p /var/lib/capcut-draft/{uploads/main,uploads/broll,outputs,logs}
chown -R $APP_USER:$APP_USER /var/lib/capcut-draft

echo "==> 6. 写 .env（如果还没有）"
if [ ! -f $APP_DIR/.env ]; then
  cat > $APP_DIR/.env <<EOF
# 拷到 /opt/capcut-draft/.env
CAPCUT_JWT_SECRET=$(openssl rand -hex 32)
CAPCUT_DB_URL=sqlite:////var/lib/capcut-draft/capcut.db
CAPCUT_UPLOAD_DIR=/var/lib/capcut-draft/uploads
CAPCUT_OUTPUT_DIR=/var/lib/capcut-draft/outputs
CAPCUT_LOG_DIR=/var/lib/capcut-draft/logs
# 清理参数
CAPCUT_CLEANUP_INTERVAL=3600
CAPCUT_CLEANUP_UPLOAD_AGE=604800
CAPCUT_CLEANUP_ZIP_AGE=604800
CAPCUT_CLEANUP_LOG_AGE=2592000
CAPCUT_CLEANUP_OFFLINE_DAYS=30
EOF
  chown $APP_USER:$APP_USER $APP_DIR/.env
  chmod 600 $APP_DIR/.env
fi

echo "==> 7. systemd service"
cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=capcut-draft server (FastAPI)
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn \\
    --bind 127.0.0.1:8000 \\
    --workers 2 \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --timeout 300 \\
    --access-logfile /var/lib/capcut-draft/logs/access.log \\
    --error-logfile /var/lib/capcut-draft/logs/error.log \\
    --capture-output \\
    capcut_draft.web:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME
echo "    服务已启动：systemctl status $SERVICE_NAME"

echo "==> 8. nginx 反代"
cat > /etc/nginx/sites-available/$SERVICE_NAME <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 2048m;   # 仅供旧的"上传到云端"路径，新 C/S 模式用不到

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/$SERVICE_NAME /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

echo "==> 9. HTTPS (Let's Encrypt)"
certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m admin@$DOMAIN || echo "   证书签发失败（可能 DNS 未解析），可手动跑 certbot"

echo
echo "=========================================="
echo "  部署完成"
echo "  服务端 URL: http://$DOMAIN"
echo "  默认管理员: xiaoma / niubi666  ← 首次登录后立即改密"
echo "  日志: journalctl -u $SERVICE_NAME -f"
echo "=========================================="
