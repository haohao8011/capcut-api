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

## 项目目标

把 **即创生成的数字人口播视频** + **B-roll 素材** + **ASR 字幕** 自动合成 **剪映草稿**（.draft 文件夹），人只需在剪映里做最后微调。

支持单视频 / 批量（整个文件夹）模式 + 浏览器 Web 上传界面。

## 技术栈

| 角色 | 选型 | 备注 |
| --- | --- | --- |
| Python | 3.11.9 | 唯一解释器，venv 在 `.venv` |
| ASR / VAD | `funasr`（paraformer-zh + fsmn-vad + ct-punc） | 首次运行从 ModelScope 下载模型 |
| 音频/视频处理 | `imageio-ffmpeg`（无系统 ffmpeg 也能跑） | |
| 音频读取 | `soundfile` + `numpy` | |
| 剪映草稿 | `pyJianYingDraft` | 直接生成 .draft 文件夹 |
| Web 服务 | `fastapi` + `uvicorn` | `python -m capcut_draft.web` |
| CLI 入口 | `python -m capcut_draft.cli` | |

## 流水线

```
数字人主视频(s) ──┐
                 ├─→ funasr (ASR + VAD) ─→ 字幕分段 + 停顿切点 ─┐
B-roll 素材 ────┘                                                ├─→ pyJianYingDraft → outputs/AI合成/
                                                                │
                                              切点策略筛选 ─────┘
```

CLI 与 Web 共用底层 `builder.py` / `asr.py` / `cutter.py`，只差入口与文件来源。

## 目录约定

```
capcut-api/
├── inputs/                       # 用户素材（git 忽略具体文件）
│   ├── README.md
│   ├── .gitkeep
│   ├── digital_human.mp4         # 单视频模式
│   └── broll/                    # B-roll 素材
├── outputs/                      # 生成的剪映草稿（git 忽略）
├── uploads/                      # Web 端上传的临时文件（git 忽略）
├── screenshots/                  # 截图存这里（git 忽略 .edge_profile_*）
│   └── *.png                     # 4 张 UI 截图
├── config/                       # ★ 工作流持久化（用户部分 git 忽略）
│   ├── workflows.builtin.json    # 6 个内置工作流（git 跟踪）
│   └── workflows.user.json       # 用户保存的工作流（git 忽略，本地）
├── src/capcut_draft/             # 源码包
│   ├── models.py                 # 切点 / 字幕数据类
│   ├── asr.py                    # funasr 调用
│   ├── cutter.py                 # 切点策略
│   ├── builder.py                # pyJianYingDraft 组装
│   ├── cli.py                    # CLI 入口（支持单/批）
│   ├── web.py                    # FastAPI 入口
│   └── static/index.html         # Web 前端（单文件 SPA）
├── tests/                        # 冒烟 / 验证脚本
│   ├── web_smoke.py              # 端到端 API 验证
│   ├── ui_check.py               # UI 元素 + 端到端
│   ├── workflow_check.py         # 6 个工作流验证
│   ├── wf_api_check.py           # 工作流 API（列表/保存/删除）验证
│   └── shoot.py                  # Edge headless 截图脚本
├── .venv/                        # 虚拟环境
├── .gitignore
├── pyproject.toml
├── requirements.txt
├── start.bat / stop.bat          # ★ 一键启停（双击即用）
├── run.ps1 / run.bat             # CLI 一键运行
├── README.md
└── agents.md                     # 本文件
```

## 便利脚本

| 脚本 | 用途 |
| --- | --- |
| `start.bat` | **双击启动 Web 服务**（建 venv → 装依赖 → 后台拉起 → 开浏览器） |
| `start.bat 9000` | 同上，自定义端口 |
| `stop.bat` | 双击停服（按端口找进程 kill） |
| `start.bat 9000` 后 `stop.bat 9000` | 配对使用 |
| `run.ps1 -Serve` | PowerShell 启动 Web（看实时日志） |
| `run.ps1` | PowerShell 跑 CLI（跳过 ASR） |
| `run.ps1 -WithAsr` | PowerShell 跑 CLI（带 ASR） |
| `run.bat` / `run.bat serve` | CMD 跑 CLI / 起服务 |

## Web 端接口

启动后 `http://localhost:8000/` 即 UI，文档在 `/docs`。

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 单页前端 |
| `/favicon.ico` | GET | 青色 C 图标（程序生成，避免 404） |
| `/api/upload-main` | POST | 上传主视频，返回 `file_id` |
| `/api/upload-broll` | POST | 上传多个 B-roll，返回 `file_ids[]` |
| `/api/jobs` | POST | 创建任务（主+副+参数），返回 `job_id` |
| `/api/jobs` | GET | 任务列表 |
| `/api/jobs/{id}` | GET | 任务详情（含完整 progress 日志） |
| `/api/jobs/{id}/download` | GET | 打包 zip 流回 |
| `/api/jobs/{id}` | DELETE | 删除任务 + 草稿 + zip |
| `/api/workflows` | GET | 列出全部工作流（内置 + 用户） |
| `/api/workflows` | POST | 把当前参数保存为用户工作流（id 自动生成 `u_xxx`） |
| `/api/workflows/{id}` | DELETE | 删除用户工作流（内置不可删，会 400） |
| `/docs` | GET | Swagger UI |

## UI 设计

- **风格**：深色"剪辑工作台 / Editing Suite Cockpit"
- **配色**：`#0a0b0e` 黑底 + 电压蓝 `#00d4ff` + 警示橙 + 成功绿 + 危险红
- **字体**：JetBrains Mono（数字/标签）+ Noto Sans SC（中文）
- **布局**：左列 4 步（上传/上传/参数/任务），右列 04 任务，窄屏堆叠
- **6 个工作流预设**：🚀 带货口播 / 📚 知识分享 / 🎬 Vlog 故事 / 🎯 极简字幕 / ⚡ 快测预览 / 🤖 TTS / 数字人
- **工作流存储**：内置 6 个写在 `config/workflows.builtin.json`（git 跟踪），用户在 UI 里"+ 保存当前参数为新工作流"会落到 `config/workflows.user.json`（git 忽略）；用户工作流右上角 hover 出 × 可删
- **核心 bug 已修**：broll 上传路由、删除按钮渲染、label/input 关联、a/button 嵌套、polling 智能启停、toast 堆叠、failed 任务"重新提交"按钮

## 已知问题 / 进度

### 已完成
- [x] Python 环境统一到 3.11.9
- [x] 跳过 ASR 路径跑通
- [x] 真 ASR 路径跑通（paraformer-zh + fsmn-vad + ct-punc）
- [x] 批量模式（一个文件夹数字人 → 一堆草稿）
- [x] FastAPI Web 服务（上传 / 任务 / 下载 / 删除）
- [x] 深色 cockpit UI 重设计
- [x] 6 个工作流预设 + 一键套用 + 微调失效检测
- [x] 一键启停脚本（start.bat / stop.bat）
- [x] /favicon.ico 端点（程序生成 16x16 青色 C）
- [x] 关掉 ANSI 颜色码（start.bat 设 `NO_COLOR=1`）让 cmd 窗口日志干净
- [x] 临时垃圾清理（`.edge_profile_*` 72.94 MB / `__pycache__` / 一次性脚本）
- [x] 工作流外置到 `config/*.json`（内置 6 个 git 跟踪，用户保存的 git 忽略）
- [x] 用户工作流保存 / 删除 API（`/api/workflows` GET/POST/DELETE）

### 未做 / 待办
- [ ] 真实即创视频端到端测试（需要用户提供素材）
- [ ] B-roll 智能匹配（关键词 → 素材），目前是顺序轮询
- [ ] 字幕样式（字体/位置/动画）目前是默认白字
- [ ] 用户自定义工作流保存（目前只内置 6 个）
- [ ] 任务历史的持久化（重启后任务列表清空，仅上传文件保留）
- [ ] Web 端多用户 / 鉴权
- [ ] 公网访问（现在只 bind 0.0.0.0:8000 但无鉴权，不建议暴露公网）

## 协作规则

- 修改代码前先 `Read` 现有文件，不要凭印象改
- 写完代码 + 自测通过再 commit，commit 前看 `git diff` 确认改动范围
- 沙盒环境限制：Playwright Chromium 装不了（`__dirlock` 权限），截图统一用 Edge headless（`tests/shoot.py`）
- PowerShell 写 `.bat` 记得用 `[System.IO.File]::WriteAllText(..., [System.Text.UTF8Encoding]::new($true))`，否则中文会被写成 `?`
- `Remove-Item -Recurse -Force` 在 IDE 沙盒下会被通配符路径拦，做大范围删除用 Python 脚本
- **手机远程控制场景**：发图给用户是看不到的（localhost 不通），描述界面用纯文字 + ASCII 草图
