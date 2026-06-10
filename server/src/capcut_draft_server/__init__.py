"""capcut-draft-server: Web 服务端（API + 鉴权 + 任务调度 + 清理）。

启动：`capcut-server`（gunicorn 形式）或 `uvicorn capcut_draft_server.web:app --reload`（开发）。
"""
from __future__ import annotations

__version__ = "0.2.0"
