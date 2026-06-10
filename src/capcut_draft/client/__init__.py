"""capcut-draft 客户端子包。

双击 `start-client.bat` 启动；启动后：
1. 读 config/client.yaml 拿到服务端 URL + 本机 token + 素材目录
2. 后台 worker 轮询服务端"我能领的任务" → 本地跑 _process_one → 上报进度/完成
3. 启动本地 FastAPI（127.0.0.1:8001）展示极简 UI（状态 + 素材库 + 任务历史）

**所有素材/草稿/数字人/数字人视频都不出本机**。
云端只存"路径引用 + 元数据 + 任务状态"。
"""
