# capcut-draft

把 **即创/数字人口播视频** + **B-roll 素材** + **ASR 字幕** 自动合成一个 **剪映草稿**（.draft 文件夹）。
打开剪映 → 导入草稿 → 微调 → 导出成品。

## 流水线

```
数字人主视频 ──┐
               ├─→ funasr (ASR + VAD) ─→ 字幕分段 + 停顿切点 ─┐
B-roll 素材 ──┘                                                ├─→ pyJianYingDraft → outputs/AI合成/
                                                               │
                                              切点策略筛选 ────┘
```

## 安装

```powershell
cd d:\Offices\Program\Python\capcut-api
pip install -r requirements.txt
```

> 首次运行 ASR 时，funasr 会自动从 ModelScope 下载 `paraformer-zh` / `fsmn-vad` / `ct-punc` 模型（数 GB，需联网）。

## 准备素材

```
inputs/
  digital_human.mp4     # 你的数字人口播主视频
  broll/
    01.mp4              # 穿插素材（任意顺序）
    02.mp4
    03.mp4
    ...
```

## 运行

```powershell
$env:PYTHONPATH = "src"
python -m capcut_draft.cli `
  --main inputs\digital_human.mp4 `
  --broll inputs\broll `
  --out outputs `
  --name AI合成
```

## 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--pause-threshold` | 0.6 | 多长的静音算"语义停顿"，作为切点 |
| `--min-cut-interval` | 2.5 | 相邻切点最小间隔（秒） |
| `--max-cuts` | 不限 | 最多用几个切点（多了就均匀采样） |
| `--broll-duration` | 2.5 | 每个 B-roll 停留多少秒 |
| `--no-subtitles` | - | 不写字幕轨 |
| `--skip-asr` | - | 跳过 ASR，只按 6 秒固定间隔切 |

## 输出

会在 `outputs/AI合成/` 下生成一个剪映草稿文件夹（含 `draft_content.json`、`draft_meta_info.json`、`assets/`），在剪映中：

**媒体 → 导入 → 草稿 → 选这个文件夹** 即可加载。

## 目录结构

```
capcut-api/
├── inputs/                 # 你的素材
│   ├── digital_human.mp4
│   └── broll/
├── outputs/                # 生成的草稿
├── src/capcut_draft/
│   ├── __init__.py
│   ├── models.py           # 数据模型
│   ├── asr.py              # funasr 转写 + VAD
│   ├── cutter.py           # 切点筛选
│   ├── builder.py          # pyJianYingDraft 组装
│   └── cli.py              # CLI 入口
└── requirements.txt
```
