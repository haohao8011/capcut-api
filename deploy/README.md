# capcut-draft 部署与使用

> **核心原则（用户原话）**：不好弄的、需要每次都弄得 → 全部放服务端。本地只点两下就出结果。

---

## 一、架构 & 零缓存

```
                  公网（HTTPS）                       员工机器（多台）
        ┌───────────────────────┐        ┌─────────────────────────┐
        │  Nginx + gunicorn     │        │  install-client.bat     │
        │  FastAPI:             │  ←──   │  start-client.bat       │
        │  - /api/auth/*        │  JWT   │  capcut_draft.client    │
        │  - /api/clients/*     │  +     │  - worker (heartbeat/   │
        │  - /api/assets/*      │  cap_  │    scan/poll)           │
        │  - /api/tasks/*       │  token │  - local FastAPI 8001   │
        │  - /api/clients/      │        │  - cli._process_one     │
        │    wizard/*           │        │    (本地 ASR + 草稿)     │
        │  - cleanup_loop       │        │  - output_dir (本地)    │
        │  - SQLite / PG        │        │                         │
        └───────────────────────┘        └─────────────────────────┘
```

**零缓存**：所有数字人视频 / B-roll / 草稿 永远在员工机器。云端只存：
- 路径引用（字符串） + 文件大小/时长/修改时间
- 任务状态 + 错误日志
- 用户 / 客户端元数据

---

## 二、服务端部署（管理员 / 运维做一次）

> 适合：Ubuntu 22.04 阿里云 ECS（1C2G 够 3-4 人用）

### 一键脚本
```bash
sudo apt update && sudo apt install -y git
sudo mkdir -p /opt/capcut-draft
sudo chown $USER:$USER /opt/capcut-draft
cd /opt/capcut-draft
git clone <repo-url> .

# 装 venv + deps + nginx + systemd + HTTPS 证书
chmod +x deploy/aliyun-server.sh
sudo ./deploy/aliyun-server.sh capcut.your-domain.com
```

脚本会：
1. 建 `capcut` 系统用户
2. 建 `.venv` 装 `[server]` 套件（gunicorn + psycopg2-binary）
3. 写 `/etc/systemd/system/capcut-server.service`（gunicorn 2 worker + uvicorn worker class）
4. 写 nginx 反代（`client_max_body_size 2048m`，兼容老上传路径）
5. 跑 certbot 拿 Let's Encrypt 证书
6. 写 `/opt/capcut-draft/.env`（含随机 JWT secret）

### 检查
```bash
systemctl status capcut-server
journalctl -u capcut-server -f
curl https://capcut.your-domain.com/api/auth/me
```

### 首次登录
- 浏览器开 `https://capcut.your-domain.com/login`
- 默认 `xiaoma` / `niubi666`
- **立即去"用户"tab 改密码**

### 切到 PostgreSQL（可选，3-4 人用 SQLite 够）
```bash
# 在 ECS 上
sudo apt install -y postgresql
sudo -u postgres createuser -P capcut       # 输入密码
sudo -u postgres createdb -O capcut capcut

# /opt/capcut-draft/.env
CAPCUT_DB_URL=postgresql+psycopg2://capcut:yourpass@127.0.0.1:5432/capcut

sudo systemctl restart capcut-server
# 自动建表（init_all_tables）
```

---

## 三、客户端安装（员工机器 · 一辈子就点一次）

### 3.1 Windows
1. **复制一份代码到员工机器**（任选一种）：
   - `git clone <repo-url> D:\capcut-draft`（如果员工有 git 权限）
   - 或者管理员把整个 `capcut-draft` 目录 zip 发给员工，解压
2. **管理员**先在服务端 dashboard 做两件事：
   - 登录 → "客户端" tab → "🪄 生成安装码" → 6 位码告诉员工
3. **员工**双击 `install-client.bat`：
   - 自动检测 Python（≥3.11）
   - 自动建 `.venv-client\` 装客户端依赖
   - 弹窗输"服务端 URL + 6 位码 + 机器名" → 自动换 token → 存到 `~/.capcut-draft/credentials.json`
4. 之后每次启动：双击 `start-client.bat`

**员工这一辈子要做的事**：点 install-client.bat 一次 + 之后双击 start-client.bat 启动。
**接触不到**：yaml / token / 数据库 / 命令行 / Python。

### 3.2 Linux / macOS / WSL2
```bash
cd /opt/capcut-draft
chmod +x deploy/aliyun-client.sh
./deploy/aliyun-client.sh https://capcut.your-domain.com ABCD23
```
脚本自动写 `~/.config/systemd/user/capcut-client.service`，登录即自启。

### 3.3 想恢复"手改 yaml"模式（高级用户 / 老客户端）
直接编辑 `~/.config/capcut-draft/client.yaml` 或 `config/client.yaml`，参考 `config/client.example.yaml`。
启动时 `start-client.bat --config client.yaml` 即可（token 和 url 仍以 credentials.json 为准，避免漂移）。

---

## 四、员工操作速查

| 场景 | 怎么操作 |
| --- | --- |
| 启动 | 双击 `start-client.bat`（Linux: `systemctl --user start capcut-client`） |
| 关闭 | 关掉黑色窗口（Linux: `systemctl --user stop capcut-client`） |
| 看状态 | 浏览器开 `http://127.0.0.1:8001` |
| 重连（换了服务端 / 重置 token） | 双击 `start-client.bat --reset` 跑向导重装 |
| 草稿放哪 | `~/Videos/capcut-drafts/task_<id>_<ts>/`（**本机**，不上传） |
| 修素材库路径 | 改 `~/.config/capcut-draft/client.yaml` 里的 `assets.dirs`（**自动重扫**） |

---

## 五、清理策略（云端自动）

`web.py` 的 `cleanup_loop` 后台线程每小时跑一次：

| 清理项 | 阈值（默认） | 环境变量 |
| --- | --- | --- |
| `uploads/main/*` + `uploads/broll/*` 旧文件 | 7 天 | `CAPCUT_CLEANUP_UPLOAD_AGE` |
| `outputs/*.zip` 旧压缩包 | 7 天 | `CAPCUT_CLEANUP_ZIP_AGE` |
| `task_logs` 旧记录 | 30 天 | `CAPCUT_CLEANUP_LOG_AGE` |
| 30 天没心跳的 `Client.is_online` | 30 天 | `CAPCUT_CLEANUP_OFFLINE_DAYS` |
| 清理循环间隔 | 1 小时 | `CAPCUT_CLEANUP_INTERVAL` |

**C/S 模式下 `uploads/` 几乎空着**，因为员工机器根本不上传文件。

---

## 六、备份 & 监控

### 备份（就 4 个东西）
```bash
# 服务端机器
tar -czf capcut-backup-$(date +%Y%m%d).tar.gz \
  /var/lib/capcut-draft/capcut.db \
  /opt/capcut-draft/.env \
  /etc/systemd/system/capcut-server.service \
  /etc/nginx/sites-available/capcut-server
```

**员工机器不需要备份** — 素材/草稿本来就是本地的，按用户习惯存。

### 监控
```bash
# 服务端
systemctl status capcut-server
journalctl -u capcut-server --since "1 hour ago" -f

# 客户端
journalctl --user -u capcut-client -f   # Linux
# 或直接看 client.log（Windows）
```

Dashboard：
- 服务端 `https://capcut.your-domain.com/` 看"任务/客户端"tab + 安装码列表
- 客户端 `http://127.0.0.1:8001/` 看本地 worker / 扫盘 / 心跳

---

## 七、升级

```bash
# 服务端
cd /opt/capcut-draft
git pull
.venv/bin/pip install -e .[server] -i https://pypi.tuna.tsinghua.edu.cn/simple
sudo systemctl restart capcut-server

# 客户端（Windows）
# 解压新版覆盖，启动时会自动重装缺的依赖（pip -e .[client]）
# 客户本地 credentials.json 不动
```

---

## 八、故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 员工客户端连不上服务端 | 防火墙 / 域名未解析 / 证书过期 | `curl https://your-domain/api/auth/me` 测一下 |
| 心跳 401 | token 被 admin 重置 | 员工双击 `start-client.bat --reset` 重配 |
| 任务一直 pending | 没有 worker 在线 / 所有 client 的 owner 不匹配 | dashboard 看"客户端"tab 是否有人在线；看 task.owner_id |
| ASR 跑半天没出 | funasr 模型下载慢 | 让员工把 .cache/modelscope 复用（或者预装到公网 OSS） |
| 草稿生成后剪映打不开 | 路径有中文 / pyJianYingDraft 版本不匹配 | 升级到 1.0+；草稿目录别放桌面 |
| 装了但 `py` 命令找不到 | Windows Python 没加 PATH | 重装 Python 勾 "Add Python to PATH" |
| 安装码过了 60 分钟过期 | 员工迟迟没输 | 再生成一个 |
