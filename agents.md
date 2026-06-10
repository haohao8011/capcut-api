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

**零缓存原则**（用户明确要求）：
- 所有 **数字人视频 / B-roll 素材 / 生成的草稿** 永远不出员工本机
- 云端服务端只存：**路径引用**（字符串） + 文件元数据（size/duration/mtime） + 任务状态 + 错误日志
- 客户端 worker 调 `_process_one` **完全在本地**：读本地文件 → 本地 ASR → 本地建草稿
- 后台 `cleanup_loop` 每小时清一次：7 天前的旧上传/zip、30 天前的 task_logs、30 天没心跳的 client

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

## 目录约定

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
│   └── capcut.db                 # SQLite（默认）
├── deploy/                       # ★ 阿里云部署
│   ├── aliyun-server.sh          # ★ 一键部署服务端（Ubuntu + nginx + HTTPS）
│   ├── aliyun-client.sh          # ★ 一键部署客户端（systemd --user）
│   └── README.md                 # 部署说明
├── src/capcut_draft/             # 源码包
│   ├── models.py                 # 切点 / 字幕数据类（CutPoint / Segment / Word / Subtitle）
│   ├── db_models.py              # ★ Client / Asset / Task / TaskLog（SQLAlchemy 2.x）
│   ├── asr.py                    # funasr 调用
│   ├── cutter.py                 # 切点策略
│   ├── builder.py                # pyJianYingDraft 组装
│   ├── cli.py                    # CLI 入口 + _process_one（带 progress_cb）
│   ├── auth.py                   # 鉴权（bcrypt + JWT + SQLAlchemy；读 CAPCUT_DB_URL）
│   ├── web.py                    # FastAPI 入口（含 cleanup_loop）
│   ├── web_clients.py            # ★ /api/clients/* （注册/心跳/列表/删/重置 token）
│   ├── web_assets.py             # ★ /api/assets/* （批量上报/列表/详情/删）
│   ├── web_tasks.py              # ★ /api/tasks/* （CRUD + 领取/进度/完成/失败/取消/重试）
│   ├── static/
│   │   ├── index.html            # ★ dashboard 风格（任务/客户端/用户 3 tab）
│   │   └── login.html
│   └── client/                   # ★ 客户端子包
│       ├── __init__.py
│       ├── __main__.py           # 启动入口
│       ├── config.py             # 读 config/client.yaml
│       ├── api.py                # ServerAPI 封装（httpx）
│       ├── storage.py            # 本地扫盘 + 元数据上报
│       ├── worker.py             # ★ 后台 3 循环（心跳/扫盘/轮询）+ 任务执行
│       ├── app.py                # 本地 FastAPI（127.0.0.1:8001）
│       └── static/index.html     # 客户端极简 dashboard
├── tests/                        # 冒烟 / 验证脚本
│   ├── _auth_test.py             # 鉴权 + 路由保护 单元测试（32/32）
│   ├── test_clients.py           # ★ 客户端 API 端到端（10 步）
│   ├── test_tasks.py             # ★ 任务系统 端到端（12 步）
│   └── ...
├── .venv/                        # 虚拟环境
├── start.bat / stop.bat          # 服务端启停
├── start-client.bat / stop-client.bat   # ★ 客户端启停
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
| `/api/clients/register` | POST | user | 注册新客户端（**返回明文 token，仅此一次**） |
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

## UI 设计

### 服务端 dashboard（`/`）

- **风格**：深色 cockpit（同前）
- **3 个 tab**：📋 任务（默认） / 💻 客户端 / 👥 用户（仅 admin）
- **顶部隐私 banner**："零缓存原则"提示
- **极简 modal**：注册客户端后弹 token（红字 + 复制按钮）
- **任务行**：状态彩色 pill + 进度条 + result_path basename（**不显示完整路径**）

### 客户端 UI（`http://127.0.0.1:8001/`）

- **风格**：深色，简洁
- **三大块**：
  1. **隐私 banner**：明示"所有素材/草稿不出本机"
  2. **Worker 状态**：运行中/当前任务/最近心跳/最近扫盘/统计（done/failed/uptime）
  3. **客户端配置**（脱敏）：服务端 URL / 客户端名 / hostname / token 前缀 / 素材目录

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
- [x] db_models.py：Client/Asset/Task/TaskLog 四张表
- [x] web_clients.py / web_assets.py / web_tasks.py 三套路由
- [x] 客户端子包：config / api / storage / worker / app / static
- [x] queue_pending 附 main_asset.path + broll_assets[].path（worker 拿本地路径）
- [x] cli._process_one 加 progress_cb（worker 上报细粒度进度）
- [x] 云端 cleanup_loop（uploads / outputs/*.zip / task_logs / offline client）
- [x] 服务端 dashboard 重设计（任务/客户端/用户 3 tab）
- [x] start-client.bat + stop-client.bat + config/client.example.yaml
- [x] deploy/aliyun-server.sh + aliyun-client.sh + deploy/README.md
- [x] test_clients.py（10 步）+ test_tasks.py（12 步）端到端测试通过
- [x] 既有 32/32 鉴权测试未破坏
- [x] 用户 xiaoma / niubi666 默认管理员 + 服务端无密码重置流程

### 未做 / 待办
- [ ] 真实即创视频端到端测试（需要用户提供素材）
- [ ] B-roll 智能匹配（关键词 → 素材），目前是顺序轮询
- [ ] 字幕样式（字体/位置/动画）目前是默认白字
- [ ] 客户端侧进度上报的"阶段文案"目前是写死的，可以加更细粒度
- [ ] dashboard 截图（用户截图用 Edge headless）
- [ ] 公网域名 + 备案后用真实 HTTPS 跑一遍
- [ ] 看 `client_token` 是否需要在服务端 admin 页"重置/重发"时同时显示明文（当前是 admin 自己看，不算泄露）
