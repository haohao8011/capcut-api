# inputs/

把你的素材放这里：

```
inputs/
├── digital_human.mp4        ← 即创生成的数字人 / 口播主视频
├── my_digital_human_02.mp4  ← 也可以有更多
└── broll/
    ├── 01.mp4               ← 任意顺序，脚本会按文件名轮询
    ├── 02.mp4
    ├── 03.mp4
    └── ...
```

`.gitignore` 已经忽略 `*.mp4 / *.mov / *.wav`，所以你随便放不会被 git 跟踪。
