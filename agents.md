# Agents

> 项目级 AI 协作说明：谁在用这个项目、目标是什么、AI 助手应该怎么配合。

## 用户

- **姓名**：小马
- **方向**：短视频自动化（剪映 / 即创 / 数字人）
- **常用环境**：Windows + Python 3.11.9（项目根：`d:\Offices\Program\Python\capcut-api`）
- **偏好**：
  - 中文交流
  - 喜欢 CLI / 脚本化，避免手敲 GUI
  - 倾向"先跑通最小闭环，再迭代"的工作方式

## 项目目标

把 **即创生成的数字人口播视频** + **B-roll 素材** + **ASR 字幕** 自动合成 **剪映草稿**（.draft 文件夹），人只需在剪映里做最后微调。

## 技术栈

| 角色 | 选型 | 备注 |
| --- | --- | --- |
| Python | 3.11.9 | 唯一解释器，venv 在 `.venv` |
| ASR / VAD | `funasr`（paraformer-zh + fsmn-vad + ct-punc） | 首次运行从 ModelScope 下载模型 |
| 音频/视频处理 | `imageio-ffmpeg`（无系统 ffmpeg 也能跑） | |
| 音频读取 | `soundfile` + `numpy` | |
| 剪映草稿 | `pyJianYingDraft` | 直接生成 .draft 文件夹 |
| CLI 入口 | `python -m capcut_draft.cli` | 见 `README.md` |

## 流水线

```
数字人主视频 ──┐
               ├─→ funasr (ASR + VAD) ─→ 字幕分段 + 停顿切点 ─┐
B-roll 素材 ──┘                                                ├─→ pyJianYingDraft → outputs/AI合成/
                                                               │
                                              切点策略筛选 ────┘
```

## 目录约定

```
capcut-api/
├── inputs/                 # 用户放素材（git 忽略具体文件）
│   ├── README.md           # 放素材说明
│   ├── .gitkeep
│   └── broll/
├── outputs/                # 生成的剪映草稿（git 忽略）
├── src/capcut_draft/       # 源码包
├── .venv/                  # 虚拟环境
├── .gitignore
├── pyproject.toml
├── requirements.txt
├── run.ps1 / run.bat       # 一键运行脚本
├── README.md
└── agents.md               # 本文件
```

## 便利脚本

- **PowerShell**：`.\run.ps1`（默认跳过 ASR） / `.\run.ps1 -WithAsr`（跑 ASR） / `.\run.ps1 -MainPath other.mp4`
- **CMD**：`run.bat [主视频] [broll 目录] [输出目录] [草稿名]`

## 已知问题 / 进度

- [x] 跳过 ASR 路径跑通
- [x] 真 ASR 路径跑通（paraformer-zh + fsmn-vad + ct-punc 模型已下载）
- [ ] 真实即创视频端到端测试（需要用户提供素材）
- [ ] B-roll 智能匹配（关键词 → 素材），目前是顺序轮询
- [ ] 字幕样式（字体/位置/动画）目前是默认白字

