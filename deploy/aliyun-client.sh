#!/usr/bin/env bash
# ===================================================================
#   capcut-draft 客户端安装脚本 (员工机器：Ubuntu/Debian 或 WSL2)
#
#   一次性把客户端装成 systemd user service：
#     1. 装 pyhton venv + 客户端依赖
#     2. 拷示例 config 到 ~/.config/capcut-draft/client.yaml
#     3. 装 systemd --user service，登录即自启
# ===================================================================
set -euo pipefail

# 客户端代码也部署在 /opt/capcut-draft（与服务端共用 repo，client 子包是可选的）
APP_DIR=/opt/capcut-draft
CONFIG_DIR=$HOME/.config/capcut-draft
SERVICE_NAME=capcut-client
SERVER_URL="${1:-http://your-server:80}"
CLIENT_TOKEN="${2:-}"   # 注册时拿到的 cap_xxx

if [ ! -d $APP_DIR ]; then
  echo "!!! 服务端部署目录 $APP_DIR 不存在，请先在服务端装好代码"
  exit 1
fi

echo "==> 1. 个人 config 目录"
mkdir -p $CONFIG_DIR

if [ ! -f $CONFIG_DIR/client.yaml ]; then
  cp $APP_DIR/config/client.example.yaml $CONFIG_DIR/client.yaml
  sed -i "s|http://127.0.0.1:8000|$SERVER_URL|" $CONFIG_DIR/client.yaml
  if [ -n "$CLIENT_TOKEN" ]; then
    sed -i "s|cap_请把这里换成服务端返回的token|$CLIENT_TOKEN|" $CONFIG_DIR/client.yaml
  fi
  echo "    已生成 $CONFIG_DIR/client.yaml"
  echo "    请按需修改 assets.dirs 指向你本机的素材库"
else
  echo "    已有 $CONFIG_DIR/client.yaml，跳过"
fi

echo "==> 2. venv + 客户端依赖"
[ -d $APP_DIR/.venv ] || python3 -m venv $APP_DIR/.venv
$APP_DIR/.venv/bin/pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
$APP_DIR/.venv/bin/pip install -q httpx pyyaml fastapi uvicorn pydantic -i https://pypi.tuna.tsinghua.edu.cn/simple
# 客户端也共用服务端的 deps（pyJianYingDraft / funasr 等）
$APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "==> 3. systemd --user service"
mkdir -p $HOME/.config/systemd/user
cat > $HOME/.config/systemd/user/$SERVICE_NAME.service <<EOF
[Unit]
Description=capcut-draft client (本地 ASR + 草稿生成)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment="PYTHONPATH=$APP_DIR/src"
Environment="PYTHONIOENCODING=utf-8"
Environment="PYTHONUNBUFFERED=1"
ExecStart=$APP_DIR/.venv/bin/python -m capcut_draft.client -c $CONFIG_DIR/client.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable $SERVICE_NAME
systemctl --user restart $SERVICE_NAME
echo "    服务已启动：systemctl --user status $SERVICE_NAME"

echo
echo "=========================================="
echo "  客户端安装完成"
echo "  本地 UI: http://127.0.0.1:8001/"
echo "  服务端:  $SERVER_URL"
echo "  配置:    $CONFIG_DIR/client.yaml"
echo "  日志:    journalctl --user -u $SERVICE_NAME -f"
echo
echo "  ⚠️  客户端 UI 默认只绑 127.0.0.1，不暴露公网"
echo "  ⚠️  所有素材/草稿永远留在本机，云端不缓存文件"
echo "=========================================="
