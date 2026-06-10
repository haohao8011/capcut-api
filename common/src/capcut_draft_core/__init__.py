"""capcut-draft-core: 服务端 / 客户端共享的 ASR + 草稿生成逻辑。

**这是纯函数包，没有任何 HTTP / FastAPI / 鉴权依赖**。
- 服务端 `capcut_draft_server` 调它（web.py 后台任务）
- 客户端 `capcut_draft_client` 调它（worker.py 本地任务）
- 独立命令行 `capcut-draft` 调它（手工跑单个视频）
"""
from __future__ import annotations

__version__ = "0.2.0"
