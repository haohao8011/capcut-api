# Agents

> 项目级 AI 协作说明：谁在用这个项目、目标是什么、AI 助手应该怎么配合。
> **本文件由 AI 助手维护，每次大改动后同步。**

## 用户

- **姓名**：小马
- **方向**：短视频自动化（剪映 / 即创 / 数字人）
- **常用环境**：Windows + Python 3.11.9（项目根：`d:\Offices\Program\Python\capcut-api`），**常用手机远程控制桌面**
- **偏好**：
  - 中文交流
  - 喜欢 CLI / 脚本化 / Web UI，避免手敲 GUI
  - 倾向"先跑通最小闭环，再迭代"的工作方式
  - 想要"开箱即用"（双击启动、所见即所得）
  - 重视隐私：所有数字人/素材/草稿留在本机，云端只做调度

## 项目目标

把 **即创生成的数字人口播视频** + **B-roll 素材** + **ASR 字幕** 自动合成 **剪映草稿**（.draft 文件夹），人只需在剪映里做最后微调。

### C/S 架构（当前设计，2025-06 之后）

```
                  公网（HTTPS）                       员工机器（多台）
        ┌───────────────────────┐        ┌─────────────────────────┐
        │  Nginx + gunicorn     │        │  start-client.bat       │
        │  FastAPI:             │  ←──   │  capcut_draft.client    │
        │  - /api/auth/*        │  JWT   │  - worker (heartbeat/   │
        │  - /api/clients/*     │  token │    scan/poll)           │
        │  - /api/assets/*      │  +     │  - local FastAPI 8001   │
        │  - /api/tasks/*       │  cap_  │  - cli._process_one     │
        │  - cleanup_loop       │  token │    (本地 ASR + 草稿)     │
        │  - SQLite / Postgres  │        │  - output_dir (本地)    │
        └───────────────────────┘        └─────────────────────────┘
```

**零素材缓存 + 草稿存云端**（用户多次明确要求，2026-06）：
- **数字人视频 / B-roll 素材 / 临时上传** 永远不出员工本机 — 云端只存路径引用 + 元数据
- **生成的草稿 .zip 默认存到云端**（`data/drafts/{owner_id}/`）— 员工可下载/删除/分享
  - quota 默认 5GB/人（`CAPCUT_DRAFT_QUOTA_MB` 可改；用户表 `quota_mb` 字段可单独覆盖）
  - 超限**不自动删**，让用户自己去 Web 后台清理
  - 草稿**永久保留**，cleanup_loop 不动草稿表（用户资产，非临时缓存）
- 客户端 worker 调 `_process_one` **完全在本地**：读本地文件 → 本地 ASR → 本地建草稿
- 后台 `cleanup_loop` 每小时清一次：7 天前的旧上传、30 天前的 task_logs、30 天没心跳的 client

**"难活的都在服务端"原则**（用户原话）：
- 所有"需要每次都弄得"的活（鉴权、DB、清理、token 颁发、安装码生成）放服务端
- 员工机器**永远只做 2 件事**：① 双击 `install-client.bat`（一次） ② 双击 `start-client.bat`（每次）
- 接触不到：yaml / token / 数据库 / 命令行 / Python

### 客户端注册两种方式
A. 高级注册：admin 调 `/api/clients/register` 拿明文 token → 手动告诉员工
B. **推荐（wizard）**：admin 生成 6 位 setup_code（`/api/clients/wizard/setup`）→ 员工双击 install-client.bat → 自动调 `/api/clients/wizard/redeem` 换 token → 存 `~/.capcut-draft/credentials.json`（权限 600）

## 技术栈

| 角色 | 选型 | 备注 |
| --- | --- | --- |
| Python | 3.11.9 | 唯一解释器，venv 在 `.venv` |
| ASR / VAD | `funasr`（paraformer-zh + fsmn-vad + ct-punc） | 首次运行从 ModelScope 下载模型 |
| 音频/视频处理 | `imageio-ffmpeg`（无系统 ffmpeg 也能跑） | |
| 音频读取 | `soundfile` + `numpy` | |
| 剪映草稿 | `pyJianYingDraft` | 直接生成 .draft 文件夹 |
| Web 服务（服务端） | `fastapi` + `gunicorn` + `nginx` | `python -m capcut_draft.web` |
| Web UI（客户端） | `fastapi` + `uvicorn`（**只绑 127.0.0.1:8001**） | `python -m capcut_draft.client` |
| 鉴权 | `bcrypt` 4.x + `pyjwt` (HS256) | 用户 access 2h + refresh 30d |
| 客户端鉴权 | `bcrypt` 哈希的 `cap_xxx` opaque token | 客户端调 `/api/clients/*` 用 |
| 数据库 | SQLite（默认）/ PostgreSQL | `CAPCUT_DB_URL` 环境变量切；自动建表 |
| 部署 | gunicorn + systemd + nginx + Let's Encrypt | 详见 `deploy/README.md` |
| 客户端部署 | `start-client.bat`（Win） / systemd --user（Linux） | |
| 内部异步通信 | `httpx` | 客户端用，服务端不用 |

## 流水线

```
数字人主视频(s) ──┐
                 ├─→ funasr (ASR + VAD) ─→ 字幕分段 + 停顿切点 ─┐
B-roll 素材 ────┘                                                ├─→ pyJianYingDraft → outputs/AI合成/
                                                                │
                                              切点策略筛选 ─────┘
```

CLI、Web 服务端（旧"上传到云端"模式）、Web 客户端（新 C/S 模式）共用底层 `builder.py` / `asr.py` / `cutter.py`，只差入口与文件来源。

## 数据库表（SQLAlchemy 2.x）

| 表 | 来源 | 说明 |
| --- | --- | --- |
| `users` | `auth.py` | 鉴权、role（admin/user） |
| `clients` | `db_models.py` | 员工机器注册：name/hostname/owner_id/token_hash |
| `assets` | `db_models.py` | 资产元数据：path/name/kind/size/duration/mtime（**不含文件内容**） |
| `tasks` | `db_models.py` | 任务派发：main_asset_id/broll_asset_ids/options/status/progress/result_path |
| `task_logs` | `db_models.py` | 任务日志：ts/level/message（30 天后自动清） |
| `setup_codes` | `db_models.py` | 一次性 6 位安装码（wizard 用）：code/name_hint/expires_at/redeemed_at |
| `drafts` | `db_models.py` | ★ 草稿云端存储：task_id/owner_id/filename/storage_path/size/sha256/download_count |
| `draft_shares` | `db_models.py` | ★ 草稿分享 token：draft_id/token/expires_at/used/used_ip（7 天 + 一次性） |

## 目录约定

> **monorepo 三子包结构**（2026-06 重构）：`common/` + `server/` + `client/`，`pip install -e ./common -e ./server -e ./client` 独立可装。

```
capcut-api/
├── inputs/                       # 用户素材（git 忽略具体文件）
├── outputs/                      # 生成的剪映草稿（git 忽略）
├── uploads/                      # 旧"上传到云端"模式的临时文件（git 忽略）
├── screenshots/                  # 截图存这里（git 忽略 .edge_profile_*）
├── config/                       # ★ 工作流持久化（用户部分 git 忽略）
│   ├── workflows.builtin.json    # 6 个内置工作流（git 跟踪）
│   ├── workflows.user.json       # 用户保存的工作流（git 忽略）
│   └── client.example.yaml       # ★ 客户端配置示例（git 跟踪）
├── data/                         # ★ 运行时数据（git 忽略）
│   ├── capcut.db                 # SQLite（默认）
│   └── drafts/                   # ★ 草稿云端存储（按 owner_id 子目录）
├── deploy/                       # ★ 阿里云部署
│   ├── aliyun-server.sh          # ★ 一键部署服务端（Ubuntu + nginx + HTTPS）
│   ├── aliyun-client.sh          # ★ 一键部署客户端（systemd --user）
│   └── README.md                 # 部署说明
├── common/                       # ★ 共享核心包（capcut-draft-core）
│   ├── pyproject.toml            #   包名 = capcut-draft-core
│   └── src/capcut_draft_core/
│       ├── models.py             #   切点/字幕数据类
│       ├── asr.py                #   funasr 调用
│       ├── cutter.py             #   切点策略
│       ├── builder.py            #   pyJianYingDraft 组装
│       └── cli.py                #   CLI 入口 + _process_one（带 progress_cb）
│
├── server/                       # ★ 服务端包（capcut-draft-server）
│   ├── pyproject.toml            #   包名 = capcut-draft-server
│   └── src/capcut_draft_server/
│       ├── auth.py               #   鉴权（bcrypt + JWT + SQLAlchemy；读 CAPCUT_DB_URL）
│       ├── db_models.py          #   Client/Asset/Task/TaskLog/Draft/DraftShare/SetupCode
│       ├── web.py                #   FastAPI 入口（含 cleanup_loop）+ 注册所有子路由
│       ├── web_clients.py        #   /api/clients/* （注册/心跳/列表/删/重置 token/wizard）
│       ├── web_assets.py         #   /api/assets/* （批量上报/列表/详情/删）
│       ├── web_tasks.py          #   /api/tasks/* （CRUD + 领取/进度/完成/失败/取消/重试）
│       ├── web_drafts.py         #   ★ /api/drafts/* + /share/* （上传/列表/下载/删/share/公开页）
│       └── static/
│           ├── index.html        #   dashboard（任务/草稿/客户端/用户 4 tab）
│           └── login.html
│
├── client/                       # ★ 客户端包（capcut-draft-client）
│   ├── pyproject.toml            #   包名 = capcut-draft-client
│   └── src/capcut_draft_client/
│       ├── __main__.py           #   启动入口（支持 --wizard / --reset / --no-worker / --no-ui）
│       ├── credentials.py        #   存 token 到 ~/.capcut-draft/credentials.json（权限 600）
│       ├── config.py             #   读 config/client.yaml 或 credentials.json
│       ├── api.py                #   ServerAPI 封装（httpx，cap_token bearer）+ upload_draft(进度回调)
│       ├── storage.py            #   本地扫盘 + 元数据上报
│       ├── worker.py             #   ★ 3 循环（心跳/扫盘/轮询）+ 任务执行 + 草稿打包上传
│       ├── app.py                #   本地 FastAPI（127.0.0.1:8001）+ 草稿管理透传端点
│       └── static/index.html     #   客户端 dashboard（含"已上传草稿"卡片）
├── tests/                        # 冒烟 / 验证脚本
│   ├── _auth_test.py             # 鉴权 + 路由保护 单元测试（32/32）
│   ├── test_clients.py           # 客户端 API 端到端（10 步）
│   ├── test_tasks.py             # 任务系统 端到端（12 步）
│   ├── test_wizard.py            # ★ wizard 流程端到端（9 步）
│   └── ...
├── .venv/                        # 虚拟环境（服务端）
├── .venv-client/                 # ★ 客户端专用 venv（员工机器）
├── start.bat / stop.bat          # 服务端启停
├── install-client.bat            # ★ 客户端首次安装（点一次就行）
├── start-client.bat / stop-client.bat   # 客户端启停
└── agents.md                     # 本文件
```

## 便利脚本

| 脚本 | 用途 |
| --- | --- |
| `start.bat` | **双击启动 Web 服务端**（建 venv → 装依赖 → 后台拉起 → 开浏览器） |
| `start.bat 9000` | 同上，自定义端口 |
| `stop.bat` | 双击停服务端（按端口找进程 kill） |
| `start-client.bat` | **双击启动客户端**（worker + 本地 UI 8001） |
| `stop-client.bat` | 停客户端 |
| `deploy/aliyun-server.sh` | 一键部署服务端到阿里云 ECS（nginx + systemd + Let's Encrypt） |
| `deploy/aliyun-client.sh` | 一键部署客户端到员工机器（systemd --user） |

## Web 服务端 API 端点

启动后 `https://your-domain/` 即 UI，文档在 `/docs`。

### 鉴权
| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/auth/login` | POST | 用户名密码 → access + refresh token |
| `/api/auth/refresh` | POST | refresh token → 新 access |
| `/api/auth/me` | GET | 当前用户信息 |
| `/api/auth/users` | GET/POST | 管理员列/建用户 |
| `/api/auth/users/{id}` | DELETE | 管理员删用户 |
| `/api/auth/users/{id}/reset-password` | POST | 管理员重置密码 |

### 客户端
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/clients` | GET | user | 列客户端（owner 隔离） |
| `/api/clients/register` | POST | user | **高级注册**（拿明文 token 写到 yaml） |
| `/api/clients/wizard/setup` | POST | user | **生成 6 位安装码**（wizard 推荐） |
| `/api/clients/wizard/codes` | GET | user | 列安装码 |
| `/api/clients/wizard/redeem` | POST | **公开** | 客户端用安装码换 token |
| `/api/clients/{id}` | DELETE | user | 删客户端 |
| `/api/clients/{id}/rotate-token` | POST | admin | 重置 token |
| `/api/clients/heartbeat` | POST | cap_token | 客户端心跳 |

### 资产
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/assets` | GET | user | 列资产（owner 隔离） |
| `/api/assets/batch` | POST | cap_token | 批量 upsert 资产元数据（**不传文件**） |
| `/api/assets/{id}` | GET/DELETE | user | 详情/删 |

### 任务
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/tasks` | POST/GET | user | 创建/列任务 |
| `/api/tasks/{id}` | GET/DELETE | user | 详情/删 |
| `/api/tasks/{id}/cancel` | POST | user | 取消 |
| `/api/tasks/{id}/retry` | POST | user | 重置为 pending |
| `/api/tasks/queue/pending` | GET | cap_token | 客户端轮询（**带 main_asset.path / broll_assets[].path**） |
| `/api/tasks/{id}/claim` | POST | cap_token | 领取 |
| `/api/tasks/{id}/start` | POST | cap_token | 标记开始 |
| `/api/tasks/{id}/progress` | POST | cap_token | 上报进度 |
| `/api/tasks/{id}/log` | POST | cap_token | 写日志 |
| `/api/tasks/{id}/complete` | POST | cap_token | 完成 + result_path |
| `/api/tasks/{id}/fail` | POST | cap_token | 失败 + error |

### 草稿（云端存储 + 分享）
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/drafts/upload` | POST | user | multipart 上传草稿 .zip（含 task_id/task_name/workflow_name），流式接收 + Content-Length 预检 quota |
| `/api/drafts` | GET | user | 列表（搜索 q / 筛选 min_size-max_size-date_from-date_to-uploader_id / 排序 / 分页）|
| `/api/drafts/quota` | GET | user | 已用 / quota / 草稿数 / 是否不限 |
| `/api/drafts/{id}/download` | GET | user | FileResponse 流式下载（计数 +1）|
| `/api/drafts/{id}` | DELETE | user | 硬删（DB + 磁盘文件 + 关联分享 token）|
| `/api/drafts/{id}/share` | POST | user | 生成分享 token（默认 7 天，可指定 1~90 天）|
| `/share/{token}` | GET | **公开** | 分享页（HTML，点确认下载）|
| `/share/{token}/download?confirm=1` | GET | **公开** | 一次性下载（用后即焚）|

**Quota 规则**：
- 默认 5GB/人（`CAPCUT_DRAFT_QUOTA_MB`）
- 用户表 `users.quota_mb` 字段可单独覆盖；0 = 不限
- 上传前 `used + file_size > quota` → 413，提示"请删除历史草稿"
- 不自动删、不定期清（草稿是用户资产）

**分享规则**：
- 64 位 url-safe token（`secrets.token_urlsafe(48)`）
- 默认 7 天过期；可指定 1~90 天
- 一次性：用 `?confirm=1` 真正下载后 `used=True`，第二次访问 → 410

## UI 设计

### 服务端 dashboard（`/`）

- **风格**：深色 cockpit（同前）
- **4 个 tab**：📋 任务（默认）/ 📦 **草稿**（新）/ 💻 客户端 / 👥 用户（仅 admin）
- **顶部隐私 banner**："零素材缓存 + 草稿存云端（5GB/人）" 提示
- **极简 modal**：注册客户端后弹 token（红字 + 复制按钮）；分享草稿后弹完整 URL（复制按钮）
- **任务行**：状态彩色 pill + 进度条 + result_path basename（**不显示完整路径**）
- **草稿 tab**：搜索框（任务名/文件名）+ 大小/日期筛选 + 排序 + 列表（大小/上传时间/下载次数）+ 配额进度条 + 下载/分享/删除按钮

### 客户端 UI（`http://127.0.0.1:8001/`）

- **风格**：深色，简洁
- **4 大块**：
  1. **隐私 banner**：明示"素材不出本机、草稿 .zip 存到云端 5GB/人"
  2. **Worker 状态**：运行中/当前任务/最近心跳/最近扫盘/统计（done/failed/uptime + ★ 已上传/失败）
  3. **客户端配置**（脱敏）：服务端 URL / 客户端名 / hostname / token 前缀 / 素材目录
  4. **★ 已上传草稿（云端）卡片**：配额用量 + 草稿数量 + 待重传数 + 列表（删除/重传按钮）

## 协作规则

- 修改代码前先 `Read` 现有文件，不要凭印象改
- 写完代码 + 自测通过再 commit，commit 前看 `git diff` 确认改动范围
- 沙盒环境限制：Playwright Chromium 装不了（`__dirlock` 权限），截图统一用 Edge headless（`tests/shoot.py`）
- PowerShell 写 `.bat` 记得用 `[System.IO.File]::WriteAllText(..., [System.Text.UTF8Encoding]::new($true))`，否则中文会被写成 `?`
- `Remove-Item -Recurse -Force` 在 IDE 沙盒下会被通配符路径拦，做大范围删除用 Python 脚本
- **手机远程控制场景**：发图给用户是看不到的（localhost 不通），描述界面用纯文字 + ASCII 草图

## 已知问题 / 进度

### 已完成
- [x] C/S 重设计：服务端（鉴权+任务调度） + 客户端（本地 ASR+草稿）
- [x] db_models.py：Client/Asset/Task/TaskLog/SetupCode + ★Draft + ★DraftShare 七张表
- [x] web_clients.py / web_assets.py / web_tasks.py + ★web_drafts.py 四套路由
- [x] 客户端子包：config / api / storage / worker / app / credentials / static
- [x] queue_pending 附 main_asset.path + broll_assets[].path（worker 拿本地路径）
- [x] cli._process_one 加 progress_cb（worker 上报细粒度进度）
- [x] 云端 cleanup_loop（uploads / outputs/*.zip / task_logs / offline client）— **不动草稿表**
- [x] 服务端 dashboard 重设计（任务/**草稿**/客户端/用户 4 tab + 安装码列表）
- [x] **wizard 流程**：6 位 setup_code + 公开 redeem 端点 + 客户端 `--wizard` 模式
- [x] install-client.bat（员工只点一次）+ start-client.bat（每次启动）
- [x] start-client.bat 加 `--wizard` / `--reset` 透传
- [x] pyproject extras_require 分组 [server] [client] [dev]
- [x] **monorepo 拆 3 个子包**：common/（capcut-draft-core）+ server/（capcut-draft-server）+ client/（capcut-draft-client），`pip install -e ./common -e ./server -e ./client` 独立可装
- [x] deploy/aliyun-server.sh + aliyun-client.sh + deploy/README.md 重写
- [x] test_clients.py（10 步）+ test_tasks.py（12 步）+ **test_wizard.py（9 步）** + **★test_drafts.py（63 步）** 端到端测试通过
- [x] 既有 32/32 鉴权测试未破坏
- [x] 用户 xiaoma / niubi666 默认管理员 + 服务端无密码重置流程
- [x] ★ **草稿云端存储**：客户端 worker 任务完成 → zip .draft 目录 → 流式上传到 /api/drafts/upload（3 次重试，失败搬到 pending_uploads/）+ quota 5GB/人
- [x] ★ **草稿管理 Web UI**：搜索/筛选/排序/分页 + 配额进度条 + 下载（计数+1）/硬删/分享按钮
- [x] ★ **草稿分享**：64 位 token，7 天过期，一次性下载（?confirm=1）
- [x] ★ **客户端 UI** 加"已上传草稿（云端）"卡片：配额/草稿数/待重传数 + 列表 + 手动重传按钮
- [x] ★ **users.quota_mb 字段**（轻量级 migration 自动加列）— admin 可单独给某用户覆盖默认 quota

### 未做 / 待办
- [ ] 真实即创视频端到端测试（需要用户提供素材）
- [ ] B-roll 智能匹配（关键词 → 素材），目前是顺序轮询
- [ ] 字幕样式（字体/位置/动画）目前是默认白字
- [ ] 客户端侧进度上报的"阶段文案"目前是写死的，可以加更细粒度
- [ ] dashboard 截图（用户截图用 Edge headless）
- [ ] 公网域名 + 备案后用真实 HTTPS 跑一遍
- [ ] 看 `client_token` 是否需要在服务端 admin 页"重置/重发"时同时显示明文（当前是 admin 自己看，不算泄露）
