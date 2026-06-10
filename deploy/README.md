# capcut-draft 部署指南

> **零缓存原则**：所有数字人视频、B-roll 素材、生成的草稿都留在员工本机。
> 云端服务端只存：路径引用 + 文件元数据 + 任务状态 + 错误日志。

## 架构

```
                  互联网                        员工机器（多台）
        ┌───────────────────────┐        ┌─────────────────────────┐
        │  Nginx (HTTPS)        │        │  start-client.bat       │
        │  ↓                   │        │  ↓                      │
        │  gunicorn (FastAPI)   │  ←──   │  capcut_draft.client    │
        │  - /api/auth/*        │  JWT   │  - worker (heartbeat/   │
        │  - /api/clients/*     │  token │    scan/poll)           │
        │  - /api/assets/*      │        │  - local FastAPI 8001   │
        │  - /api/tasks/*       │        │  - cli._process_one     │
        │  - cleanup_loop       │        │    (本地 ASR + 草稿)     │
        │  - SQLite / Postgres  │        │  - output_dir (本地)    │
        └───────────────────────┘        └─────────────────────────┘
```

数据流向：
- **注册时**：管理员调 `/api/clients/register` 拿明文 token（**只此一次**），写到员工机器 `client.yaml`
- **心跳 / 扫盘**：员工机器 → 服务端，只发 path/size/mtime，**不发文件**
- **任务派发**：服务端下发 `{main_asset.path, broll_assets[].path, options}` → 员工机器**本地读** → 跑 ASR + 草稿
- **结果**：员工机器把 `result_path`（**路径引用**）报给服务端

---

## 服务端部署（阿里云 ECS / Ubuntu 22.04）

### 1. 准备
- 阿里云 ECS：1C2G 起步，3-4 人够用
- 域名解析到 ECS 公网 IP
- 安全组开 80/443

### 2. 装系统包
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx git
```

### 3. 拉代码
```bash
sudo mkdir -p /opt/capcut-draft
sudo chown $USER:$USER /opt/capcut-draft
cd /opt/capcut-draft
git clone <your-repo-url> .
```

### 4. 跑部署脚本
```bash
chmod +x deploy/aliyun-server.sh
sudo ./deploy/aliyun-server.sh capcut.your-domain.com
```

脚本自动做：
1. 建 `capcut` 系统用户
2. 建 venv 装依赖
3. 写 `.env`（含随机 JWT secret）
4. 写 `/etc/systemd/system/capcut-server.service`
5. 写 nginx 反代 + Let's Encrypt

### 5. 检查
```bash
systemctl status capcut-server
journalctl -u capcut-server -f
curl https://capcut.your-domain.com/api/auth/me
```

### 6. 首次登录
- 浏览器开 `https://capcut.your-domain.com/login`
- 默认管理员 `xiaoma` / `niubi666`
- **立即去"用户"tab 改密**

---

## 客户端安装（员工机器：Windows / Linux / macOS）

### Windows
1. 把代码 `git clone` 到本地（比如 `D:\capcut-draft`）
2. 双击 `start.bat` 装服务端（venv + 依赖）
3. 复制 `config\client.example.yaml` 为 `config\client.yaml`
4. 管理员在服务端 dashboard → "客户端" → "注册新客户端" → 拿 token
5. 把 token 写到 `config\client.yaml` 的 `client_token`，把 `assets.dirs` 改成你本机的素材库
6. 双击 `start-client.bat` 启动
7. 浏览器开 `http://127.0.0.1:8001` 看本地 dashboard

### Linux / WSL2
```bash
cd /opt/capcut-draft   # 共用同一份代码（员工机器也把 repo 拉下来）
chmod +x deploy/aliyun-client.sh
./deploy/aliyun-client.sh https://capcut.your-domain.com cap_xxx_xxx
```

脚本自动做：
1. 写 `~/.config/capcut-draft/client.yaml`
2. 装 `systemd --user` service `capcut-client`
3. 登录即自启

---

## 数据库

默认 SQLite（3-4 人够用，零运维）：
```bash
# 默认 DB 路径
/var/lib/capcut-draft/capcut.db
```

切到 PostgreSQL（人多时）：
```bash
# /opt/capcut-draft/.env
CAPCUT_DB_URL=postgresql+psycopg2://capcut:yourpass@127.0.0.1:5432/capcut
```
装 `psycopg2-binary`，重启服务即可（自动建表）。

---

## 定期清理（云端）

云端后台线程每小时跑一次清理，阈值可配：
```bash
# /opt/capcut-draft/.env
CAPCUT_CLEANUP_INTERVAL=3600        # 1 小时
CAPCUT_CLEANUP_UPLOAD_AGE=604800    # 旧上传文件保留 7 天
CAPCUT_CLEANUP_ZIP_AGE=604800       # 旧 zip 保留 7 天
CAPCUT_CLEANUP_LOG_AGE=2592000      # 旧 task_logs 保留 30 天
CAPCUT_CLEANUP_OFFLINE_DAYS=30      # 30 天没心跳的 client 标记离线
```

清理内容：
- `uploads/main/*` + `uploads/broll/*` 里 7 天前的文件（旧的"上传到云端处理"模式留下的）
- `outputs/*.zip` 里 7 天前的压缩包
- `task_logs` 表里 30 天前的记录
- 30 天没心跳的 `Client.is_online` 改为 False

**C/S 模式下不会上传文件，所以 uploads 目录基本是空的**。

---

## 备份

只需备份：
- `/var/lib/capcut-draft/capcut.db`（一个文件，任务/用户/客户端/资产元数据）
- `/etc/systemd/system/capcut-server.service`
- `/etc/nginx/sites-available/capcut-server`
- `/opt/capcut-draft/.env`（含 JWT secret）

`/opt/capcut-draft` 里的代码由 git 拉，不需额外备份。

员工机器上的素材/草稿/ASR 缓存**不需要备份到云端**（本身就是本地的）。

---

## 监控

```bash
# 服务端
systemctl status capcut-server
journalctl -u capcut-server --since "1 hour ago" -f

# 客户端
systemctl --user status capcut-client   # Linux
# 或直接看 client.log
```

dashboard 上：
- 服务端 https://capcut.your-domain.com/ 看"任务/客户端"tab
- 客户端 http://127.0.0.1:8001/ 看本地 worker 状态
