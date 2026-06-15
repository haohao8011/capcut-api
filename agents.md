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
  - 重视效率：素材通过 Web 直传服务器，草稿自动上传云端，随时可管理
- **🔴 不备份**：本项目**不做任何备份**（DB / 草稿 / 素材都不备份）。服务器 30G 小盘扛不住双重存储；用户自己删除的草稿/素材 = 真删，服务不兜底。**AI 不要再建议加备份 / 恢复演练 / restore 脚本**——以后真要备份我会说。

## 项目目标

把 **即创生成的数字人口播视频** + **B-roll 素材** + **ASR 字幕** 自动合成 **剪映草稿**（.draft 文件夹），人只需在剪映里做最后微调。

### C/S 架构（当前设计，2025-06 之后）

```
                  公网（HTTPS）                       员工机器（多台）
        ┌───────────────────────┐        ┌─────────────────────────┐
        │  Nginx + gunicorn     │        │  start-client.bat       │
        │  FastAPI:             │  ←──   │  capcut_draft_client    │
        │  - /api/auth/*        │  JWT   │  - worker (heartbeat/   │
        │  - /api/clients/*     │  +     │    scan/poll)           │
        │  - /api/assets/*      │  cap_  │  - local FastAPI 8001   │
        │  - /api/tasks/*       │  token │  - cli._process_one     │
        │  - /api/drafts/*      │        │    (本地 ASR + 草稿)     │
        │  - cleanup_loop       │        │  - 草稿 .zip → 云端     │
        │  - SQLite / Postgres  │        │                         │
        └───────────────────────┘        └─────────────────────────┘
```

**素材云端存储 + 内容审核**（2026-06 重构）：
- **素材上传**：Web 前端直传视频文件到服务器（`/var/lib/capcut-draft/uploads/{user_id}/`）
  - 素材配额 3GB（`CAPCUT_ASSET_QUOTA_MB`）+ 草稿配额 2GB（`CAPCUT_DRAFT_QUOTA_MB`）= 5GB 总计
  - 单文件 ≤ 1GB，白名单 .mp4/.mov/.avi/.mkv/.webm
  - 流式写盘（1MB 分块 + SHA256）
- **内容审核**：默认自动通过（review_status="approved"），admin 事后可审（flag/approve/reject/delete）
- **双来源素材**：Task 同时支持 `main_asset_id`（客户端扫盘）和 `main_upload_id`（Web 上传），向后兼容
- **客户端 Worker 适配**：从服务器下载素材到本地缓存目录（`~/.capcut-draft/cache/`），LRU 淘汰
- **草稿云端存储**（沿用）：生成的草稿 .zip 存到云端（`data/drafts/{owner_id}/`）
  - quota 2GB/人，超限不自动删，让用户自己去 Web 后台清理
  - 草稿永久保留，cleanup_loop 不动草稿表

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
| Web 服务端（Ubuntu） | `fastapi` + `gunicorn` + `nginx` | `python -m capcut_draft_server.web` |
| Web UI（客户端） | `fastapi` + `uvicorn`（**只绑 127.0.0.1:8001**） | `python -m capcut_draft_client` |
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
B-roll 素材 ────┘                                                ├─→ pyJianYingDraft → 本地 .draft/AI合成/
                                                                │
                                              切点策略筛选 ─────┘
```

CLI 和 Web 客户端（C/S 模式）共用底层 `builder.py` / `asr.py` / `cutter.py`，只差入口与文件来源。
**C/S 模式服务端的角色**：仅做鉴权、任务调度、DB 存储；不接触视频文件本身。

## 数据库表（SQLAlchemy 2.x）

| 表 | 来源 | 说明 |
| --- | --- | --- |
| `users` | `auth.py` | 鉴权、role（admin/user） |
| `clients` | `db_models.py` | 员工机器注册：name/hostname/owner_id/token_hash |
| `assets` | `db_models.py` | 客户端扫盘资产元数据：path/name/kind/size/duration/mtime（**不含文件内容**） |
| `uploaded_assets` | `db_models.py` | ★ Web 上传素材：owner_id/filename/storage_path/kind/size/review_status/reviewed_by |
| `folders` | `db_models.py` | ★ 素材文件夹：owner_id/name/parent_id（支持多层嵌套） |
| `tasks` | `db_models.py` | 任务派发：main_asset_id/main_upload_id/broll_asset_ids/broll_upload_ids/options/status/progress/result_path |
| `task_logs` | `db_models.py` | 任务日志：ts/level/message（30 天后自动清） |
| `setup_codes` | `db_models.py` | 一次性 6 位安装码（wizard 用）：code/name_hint/expires_at/redeemed_at |
| `drafts` | `db_models.py` | ★ 草稿云端存储：task_id/owner_id/filename/storage_path/size/sha256/download_count |
| `draft_shares` | `db_models.py` | ★ 草稿分享 token：draft_id/token/expires_at/used/used_ip（7 天 + 一次性） |

## 目录约定

> **monorepo 三子包结构**（2026-06 重构）：`common/` + `server/` + `client/`，`pip install -e ./common -e ./server -e ./client` 独立可装。

```
capcut-api/
├── config/                       # ★ 工作流持久化（用户部分 git 忽略）
│   ├── workflows.builtin.json    # 6 个内置工作流（git 跟踪）
│   └── workflows.user.json       # 用户保存的工作流（git 忽略）
├── data/                         # ★ 运行时数据（git 忽略）
│   ├── capcut.db                 # SQLite（默认）
│   ├── drafts/                   # ★ 草稿云端存储（按 owner_id 子目录）
│   └── uploads/                  # ★ Web 上传素材存储（按 user_id 子目录）
├── deploy/                       # ★ 阿里云部署
│   ├── aliyun-server.sh          # ★ 一键部署服务端（Ubuntu + nginx + HTTPS）
│   ├── capcut-draft.service      # ★ systemd unit（参考模板）
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
├── admin/                        # ★ 管理后台 UI（前后端分离，纯静态 HTML）
│   ├── index.html                #   管理后台（数据概览/素材审核/任务监控/草稿/客户端/用户）
│   └── login.html                #   SVG 卡通小熊登录页（仅管理员可进）
│
├── frontend/                     # ★ 用户工作台 UI（前后端分离，纯静态 HTML）
│   ├── index.html                #   工作台（上传素材/素材库/我的任务/我的草稿/个人设置）
│   └── login.html                #   用户登录页（统一入口，admin 也导向 /app/）
│
├── server/                       # ★ 服务端包（capcut-draft-server，纯 REST API）
│   ├── pyproject.toml            #   包名 = capcut-draft-server
│   └── src/capcut_draft_server/
│       ├── auth.py               #   鉴权（bcrypt + JWT + SQLAlchemy；读 CAPCUT_DB_URL）
│       ├── db_models.py          #   Client/Asset/UploadedAsset/Task/TaskLog/Draft/DraftShare/SetupCode
│       ├── web.py                #   FastAPI 入口（含 cleanup_loop）+ StaticFiles 挂载 + 重定向
│       ├── web_clients.py        #   /api/clients/* （注册/心跳/列表/删/重置 token/wizard）
│       ├── web_assets.py         #   /api/assets/* （合并查询 UploadedAsset + Asset，带 source 字段）
│       ├── web_uploads.py        #   /api/uploads/* （用户端上传/列表/配额/下载/删除/move）
│       ├── web_admin.py          #   /api/admin/review/* + /api/admin/stats（admin 后台：审核 + 统计）
│       ├── web_folders.py        #   ★ /api/folders/* （CRUD + 树形结构）
│       ├── web_tasks.py          #   /api/tasks/* （CRUD + 领取/进度/完成/失败/取消/重试 + 双来源素材）
│       ├── web_drafts.py         #   ★ /api/drafts/* + /share/* （上传/列表/下载/删/share/公开页）
│       └── static/
│           └── 404.html          #   自定义 404 页（渐变 404 + 光球背景 + 粒子动画）
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
├── scripts/                      # ★ 部署脚本
│   └── deploy.ps1                #   一键部署到阿里云 ECS
└── agents.md                     # 本文件
```

## 便利脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/deploy.ps1` | **一键部署到服务器**（tar + scp + 解压 + pip + 重启） |
| `deploy/aliyun-server.sh` | 首次部署服务端到阿里云 ECS（systemd + 环境配置） |

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
| `/api/assets` | GET | user | 合并查询（UploadedAsset + Asset），带 source 字段 |
| `/api/assets/batch` | POST | cap_token | 批量 upsert 资产元数据（**不传文件**） |
| `/api/assets/{id}` | GET/DELETE | user | 详情/删 |

### 素材上传
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/uploads` | POST | user | multipart 上传视频（白名单格式，≤1GB，检查配额，支持 folder_id） |
| `/api/uploads` | GET | user | 列当前用户上传的素材（支持 folder_id 筛选） |
| `/api/uploads/quota` | GET | user | 素材配额：used/quota/warning(>80%) |
| `/api/uploads/{id}` | GET/DELETE | user | 详情/删除（DB+磁盘） |
| `/api/uploads/{id}/file` | GET | user | 下载/流式获取文件 |
| `/api/uploads/{id}/download` | GET | cap_token | Worker 从服务器下载素材 |
| `/api/uploads/{id}/move` | PATCH | user | 移动素材到指定文件夹（folder_id=null 移到根目录） |

### 文件夹
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/folders` | POST | user | 创建文件夹（name + parent_id） |
| `/api/folders` | GET | user | 列当前用户所有文件夹（平铺） |
| `/api/folders/tree` | GET | user | 文件夹树形结构 |
| `/api/folders/{id}` | GET | user | 文件夹详情 + 子文件夹 + 内容 |
| `/api/folders/{id}` | PUT | user | 重命名文件夹 |
| `/api/folders/{id}` | DELETE | user | 删除文件夹（子文件夹级联删，素材 folder_id 置空） |

### Admin 审核 & 统计
| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/admin/review` | GET | admin | 全部上传素材（含 flagged/rejected），分页筛选 |
| `/api/admin/review/{id}/flag` | POST | admin | 标记 flagged |
| `/api/admin/review/{id}/approve` | POST | admin | 标记 approved |
| `/api/admin/review/{id}/reject` | POST | admin | 标记 rejected |
| `/api/admin/review/{id}` | DELETE | admin | 管理员硬删 |
| `/api/admin/stats` | GET | admin | 系统统计：用户数/任务数/素材占用/草稿占用/磁盘剩余 |

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
- 素材默认 3GB/人（`CAPCUT_ASSET_QUOTA_MB`）
- 草稿默认 2GB/人（`CAPCUT_DRAFT_QUOTA_MB`）
- 用户表 `users.quota_mb` / `users.asset_quota_mb` 字段可单独覆盖；0 = 不限
- 上传前 `used + file_size > quota` → 413，提示"请删除历史草稿"
- 不自动删、不定期清（草稿是用户资产）

**分享规则**：
- 64 位 url-safe token（`secrets.token_urlsafe(48)`）
- 默认 7 天过期；可指定 1~90 天
- 一次性：用 `?confirm=1` 真正下载后 `used=True`，第二次访问 → 410

## UI 设计

### 前后端完全分离架构（2026-06-12 重构）

- **server/ 纯 REST API**：不再包含任何 HTML 渲染逻辑
- **admin/ 管理后台 UI**：`StaticFiles(html=True)` 挂载到 `/admin`
- **frontend/ 用户工作台 UI**：`StaticFiles(html=True)` 挂载到 `/app`
- **History 路由**：前端使用 History API（pushState），URL 如 `/app/upload`、`/app/assets`、`/app/tasks`、`/app/drafts`、`/app/settings`
- **SPA catch-all**：服务端 `/app/{path}` 返回 index.html（真实文件优先），支持刷新页面不丢失路由
- **重定向保持向后兼容**：`/` → `/app/`、`/console` → `/admin/`、`/login` → `/app/login.html`、`/console/login` → `/admin/login.html`

### 侧栏布局（admin + frontend 统一）

```
┌──────────────┬────────────────────────────┐
│   Sidebar    │      Main Content          │
│   (240px)    │  Header (标题 + 快捷链接)    │
│  Logo+品牌   │  ┌──────────────────────┐  │
│  📋 任务  3  │  │   工作区内容          │  │
│  📦 草稿  5  │  │   （表格/表单/卡片）   │  │
│  💻 客户端    │  └──────────────────────┘  │
│  👥 用户      │                            │
│  ──────────  │                            │
│  用户名      │                            │
│  ☀️ 🌙 🚪    │                            │
└──────────────┴────────────────────────────┘
```

- **Admin 侧栏**：📊 数据概览 / 🔍 素材审核 / 📋 任务监控 / 📦 草稿 / 💻 客户端 / 👥 用户
- **User 侧栏**：📤 上传素材 / 📂 素材库 / 📋 我的任务 / 📦 我的草稿 / ⚙️ 个人设置
- CSS flexbox：`.layout { display: flex }` + sticky sidebar + flex-1 主内容区
- 移动端 ≤768px 自动折叠为图标模式（60px）

### SVG 卡通人物登录页（admin/login.html）

- 内联 SVG kawaii 小熊角色（~60 行 SVG）
- 5 种交互状态：空闲呼吸 / 瞳孔跟踪鼠标 / 密码捂眼 / 悬停偷看 / 靠近脸红
- CSS class 切换（hands-up / peeking / shy）+ JS 瞳孔 transform
- 深色/浅色主题（CSS 变量控制熊脸填充色）

### 全局主题切换

- 所有页面均支持深色/浅色主题
- CSS 变量 `data-theme` + localStorage `capcut_theme` + 系统偏好检测
- `<head>` 内联 script 避免 FOUC 闪烁

### 权限 & 访问隔离

- **访问隔离**：前后端无交叉跳转链接，Admin 只能直接输入网址访问
- **Frontend Login**：所有用户（含 admin）统一导向 `/app/`，无“管理后台”链接
- **Admin Login**：非 admin 显示“此入口仅限管理员”，清除 token
- **Admin 权限检查**：admin/index.html JS 调 `/api/auth/me`，非 admin 显示无权限
- **数据隔离**：`GET /api/clients` 非 admin 强制 `WHERE owner_id = user.id`

### 客户端 UI（`http://127.0.0.1:8001/`）

- **风格**：深色，简洁
- **4 大块**：
  1. **隐私 banner**：明示"素材不出本机、草稿 .zip 存到云端 5GB/人"
  2. **Worker 状态**：运行中/当前任务/最近心跳/最近扫盘/统计（done/failed/uptime + ★ 已上传/失败）
  3. **客户端配置**（脱敏）：服务端 URL / 客户端名 / hostname / token 前缀 / 素材目录
  4. **★ 已上传草稿（云端）卡片**：配额用量 + 草稿数量 + 待重传数 + 列表（删除/重传按钮）

## 协作规则

- **🔴 不要把代码挤在一个文件写**：按职责分文件、分文件夹。一个文件超过 ~300 行就该考虑拆。Router 拆 `web_xxx.py`、Helper 抽 `xxx_helpers.py`、同类放 `xxx/` 子包——别堆在一个 `web.py` / `utils.py` 里。AI 写代码默认按这个走，**新功能起新文件**，不要 append 到已有的"大杂烩"文件。
- 修改代码前先 `Read` 现有文件，不要凭印象改
- 写完代码 + 自测通过再 commit，commit 前看 `git diff` 确认改动范围
- 沙盒环境限制：Playwright Chromium 装不了（`__dirlock` 权限），截图统一用 Edge headless（`tests/shoot.py`）
- PowerShell 写 `.bat` 记得用 `[System.IO.File]::WriteAllText(..., [System.Text.UTF8Encoding]::new($true))`，否则中文会被写成 `?`
- `Remove-Item -Recurse -Force` 在 IDE 沙盒下会被通配符路径拦，做大范围删除用 Python 脚本
- **手机远程控制场景**：发图给用户是看不到的（localhost 不通），描述界面用纯文字 + ASCII 草图
- **每次修改代码后必须同步更新 agents.md**：在「已完成」加条目（带日期），改了新文件/目录也要更新目录树

## 服务器路径

| 内容 | 路径 |
|---|---|
| 代码部署 | `/opt/capcut-draft` |
| 数据目录 | `/var/lib/capcut-draft/` |
| 数据库 | `/var/lib/capcut-draft/capcut.db` |
| 上传素材 | `/var/lib/capcut-draft/uploads/{user_id}/` |
| 草稿存储 | `/var/lib/capcut-draft/drafts/{user_id}/` |
| 环境配置 | `/opt/capcut-draft/.env` |
| systemd | `/etc/systemd/system/capcut-server.service` |

## MiMoCode 目录

| 内容 | 路径 |
|---|---|
| 技能目录 | `C:\Users\Administrator\.agents\skills\` |
| 记忆目录 | `C:\Users\Administrator\.local\share\mimocode\memory\` |
| 数据库 | `C:\Users\Administrator\.local\share\mimocode\mimocode.db` |
| 项目记忆 | `C:\Users\Administrator\.local\share\mimocode\memory\projects\<project_id>\MEMORY.md` |
| 全局记忆 | `C:\Users\Administrator\.local\share\mimocode\memory\global\MEMORY.md` |

## 部署命令

### 本地 SSH 密钥
- 密钥路径：`D:\Offices\三鼎.pem`

### 服务端升级（**用 deploy.ps1 一键部署**）
项目根目录下跑：

```powershell
# 默认：tar + scp + 解压 + pip install + 重启
.\scripts\deploy.ps1

# 只传代码不重启（手动重启用）
.\scripts\deploy.ps1 -SkipRestart

# 代码没改依赖时（更快，跳过 pip）
.\scripts\deploy.ps1 -SkipPip
```

**首次部署**（裸服务器）需先跑 `.\deploy\aliyun-server.sh` 完成系统装 + systemd 注册，之后就一直用 `deploy.ps1` 增量更新。

### 手动部署（绕过脚本，仅做应急参考）
```bash
# 1. 本地打包代码（排除 .venv, .git, __pycache__, data/, *.db, .env）
tar -czf deploy.tar.gz --exclude='.venv' --exclude='.venv-client' --exclude='.git' --exclude='.env' --exclude='__pycache__' --exclude='data' --exclude='*.db' .

# 2. 上传到服务器
scp -i "D:\Offices\三鼎.pem" deploy.tar.gz root@8.129.83.166:/opt/capcut-draft/

# 3. SSH 到服务器解压并重启
ssh -i "D:\Offices\三鼎.pem" root@8.129.83.166 "cd /opt/capcut-draft && tar -xzf deploy.tar.gz && sudo -u capcut .venv/bin/pip install -e ./common -e ./server -i https://pypi.tuna.tsinghua.edu.cn/simple && sudo systemctl restart capcut-server"
```

### 检查日志
```bash
ssh -i "D:\Offices\三鼎.pem" root@8.129.83.166 "journalctl -u capcut-server --since '1 hour ago' -f"
```

### 安全约束
- **永远不要覆盖** `/var/lib/capcut-draft/capcut.db`（用户数据/审计日志）
- **永远不要覆盖** `/opt/capcut-draft/.env`（JWT secret、DB URL 都在这）
- deploy.ps1 默认排除 .env / *.db / data/，不要随便改 exclude 规则
- 服务器权限：`/opt/capcut-draft/` 跑完是 `capcut:capcut`（gunicorn 跑在 capcut 用户下）

## 已知问题 / 进度

### 已完成
- [x] C/S 重设计：服务端（鉴权+任务调度） + 客户端（本地 ASR+草稿）
- [x] db_models.py：Client/Asset/Task/TaskLog/SetupCode + ★Draft + ★DraftShare 七张表
- [x] web_clients.py / web_assets.py / web_tasks.py + ★web_drafts.py 四套路由
- [x] 客户端子包：config / api / storage / worker / app / credentials / static
- [x] queue_pending 附 main_asset.path + broll_assets[].path（worker 拿本地路径）
- [x] cli._process_one 加 progress_cb（worker 上报细粒度进度）
- [x] 云端 cleanup_loop（task_logs / offline client）— **不动草稿表**
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
- [x] ★ **阿里云 ECS 部署完成**（2026-06-11）：Ubuntu 22.04 + Python 3.11.15（deadsnakes PPA）+ gunicorn 直绑 0.0.0.0:8000（无 nginx）+ systemd + SQLite
  - ECS IP: `8.129.83.166`，访问地址 `http://8.129.83.166:8000`
  - 数据目录 `/var/lib/capcut-draft/`（capcut.db + drafts/ + logs/）
  - `.env` 含随机 JWT secret，迁移服务器时带走即可
  - nginx 已停用（gunicorn 直连），后续加域名 HTTPS 再启用
- [x] ★ **自定义 favicon**（2026-06-11）：用用户头像（`static/favicon.jpg`）替换 PIL 生成的青色 "C"，`web.py` 改为读文件 + 内存缓存
- [x] ★ **Dashboard tab badge 预加载**（2026-06-11）：页面启动时自动 fetch 任务/草稿/客户端/用户数，不再显示 0 直到点击才更新
- [x] ★ **web.py `Response` import 修复**（2026-06-11）：favicon 端点用了 `Response` 但没 import，导致 500
- [x] ★ **双界面架构重构**（2026-06-11）：拆分单一 dashboard 为用户工作台（`/` → app.html）+ 管理后台（`/console` → console.html）
  - app.html：我的任务 / 我的草稿 / 个人设置（改密码），admin 可见「管理后台」入口
  - console.html：全局任务/草稿/客户端/用户管理，非 admin 自动跳转 `/`
  - login.html：登录后按 `is_admin` 分流到 `/console` 或 `/`
  - web_clients.py：`GET /api/clients` 非 admin 强制 owner 隔离
  - 旧 index.html 已删除
- [x] ★ **登录页现代化重设计**（2026-06-11）：
  - `login.html`（用户）：深色 glassmorphism + 渐变动画背景 + 青色主色调 + pulse 光效 + 加载动画
  - `console-login.html`（管理后台）：同上风格但橙红色调 + 「仅限管理员」badge + 非 admin 登录拒绝
  - `/console/login` 独立路由，console.html 的 401 跳转指向此而非 `/login`
- [x] ★ **自定义 404 页面**（2026-06-11）：
  - 巨型渐变 404 文字（青→紫→橙）+ 呼吸光效
  - 同样的光球动画背景 + 网格 + 浮动粒子
  - Glitch 风格分割线 + 快捷链接（工作台/管理后台/API 文档）
  - API 路径（`/api/*`）仍返回 JSON 404，不受影响
- [x] ★ **全局深色/浅色主题切换**（2026-06-11）：
  - 所有 5 个页面（login / console-login / app / console / 404）均支持
  - `<head>` 内联 `<script>` 在渲染前设 `data-theme`，避免 FOUC 闪烁
  - localStorage key: `capcut_theme`（dark/light），默认跟随系统 `prefers-color-scheme`
  - 右上角/顶栏 ☀️/🌙 切换按钮，点击即时切换 + 持久化
  - 浅色主题配色：bg `#f0f2f5` / card `#ffffff` / text `#1a1d26` / accent 深色化 / 光球低透明度
- [x] ★ **前后端完全分离 + 侧栏布局 + SVG 卡通登录页**（2026-06-12）：
  - server/ 纯 REST API，不再包含 HTML 渲染（删掉 4 个 HTML 路由）
  - `admin/` 管理后台 UI：左侧导航栏 + 右侧工作区（240px sticky sidebar + flex-1 主内容区）
  - `frontend/` 用户工作台 UI：同样侧栏布局（我的任务/我的草稿/个人设置）
  - `admin/login.html`：SVG kawaii 小熊互动登录页（捂眼/偷看/瞳孔跟踪/脸红 5 种状态）
  - 重定向保持向后兼容：`/` → `/app/`、`/console` → `/admin/`、`/login` → `/app/login.html`
  - 旧 HTML 文件（app.html / console.html / login.html / console-login.html）已从 server/static/ 删除
  - 移动端响应式折叠（≤768px 侧栏变图标模式 60px）

- [x] ★ **系统架构重构：素材上传 + 内容审核 + 前后端职责分离**（2026-06）：
  - 新增 `uploaded_assets` 表 + `web_uploads.py`（用户端上传/列表/删除/配额 + Admin 审核/统计 + 客户端下载）
  - Task 支持双来源素材：`main_asset_id`（客户端扫盘）+ `main_upload_id`（Web 上传）
  - `web_assets.py` 合并查询两张表，每条带 `source` 字段区分
  - Frontend 工作台：新增上传素材 + 素材库 section，配额双进度条，拖拽上传
  - Admin 管理后台：新增数据概览 + 素材审核 section，删除"新建任务"按钮
  - 访问隔离：删除前后端交叉跳转链接，Admin 只能直接输入网址访问
  - 客户端 Worker 适配：从服务器下载素材到本地缓存目录
  - 配额分离：素材 3GB + 草稿 2GB = 5GB 总计

- [x] ★ **History 路由 + 文件夹系统**（2026-06-12）：
  - 前端改为 History API 路由：`/app/upload`、`/app/assets`、`/app/tasks`、`/app/drafts`、`/app/settings`
  - 服务端 SPA catch-all：`/app/{path}` 返回 index.html（真实文件优先）
  - 新增 `folders` 表（支持多层嵌套，`parent_id` 指向父文件夹）
  - 新增 `web_folders.py`：CRUD + 树形结构 API
  - `uploaded_assets` 表新增 `folder_id` 列（外键关联 folders）
  - 素材库：显示文件夹列表，点击进入文件夹，支持新建/删除文件夹
  - 上传素材：可选择目标文件夹，支持文件夹上传
  - 素材移动：可将素材移动到指定文件夹
- [x] ★ **拆分 web_admin.py**（2026-06-12）：把 `web_uploads.py` 里的 admin 路由（5 review + 1 stats）拆到独立文件，`web_uploads.py` 648 → 447 行；`_resolve_upload_path` 改公开（admin 复用）；新加协作规则"不要把代码挤在一个文件写"
- [x] ★ **一键部署脚本 deploy.ps1**（2026-06-12）：项目根目录 `.\scripts\deploy.ps1` 一键 tar+scp+解压+pip+重启；6 步 pipeline，含 health check；`Clean-Bash` 函数去掉 PowerShell here-string 的 `\r`（首次跑没这个会全报错）；服务器已升级到 `c31ae62`（拆 web_admin 那个）

### 未做 / 待办
- [ ] 真实即创视频端到端测试（需要用户提供素材）
- [ ] B-roll 智能匹配（关键词 → 素材），目前是顺序轮询
- [ ] 字幕样式（字体/位置/动画）目前是默认白字
- [ ] 客户端侧进度上报的"阶段文案"目前是写死的，可以加更细粒度
- [ ] dashboard 截图（用户截图用 Edge headless）
- [ ] 公网域名 + 备案 + HTTPS（nginx + certbot 脚本已写好，待域名就绪）
- [ ] 看 `client_token` 是否需要在服务端 admin 页"重置/重发"时同时显示明文（当前是 admin 自己看，不算泄露）
- [ ] 服务器迁移文档（当前数据全在文件，scp /var/lib/capcut-draft/ + .env 即可）
