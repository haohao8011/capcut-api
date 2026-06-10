"""本地 FastAPI（只绑 127.0.0.1，绝不暴露公网）。

端点：
- GET  /                       极简 dashboard UI
- GET  /api/status             worker 当前状态
- GET  /api/config             客户端配置（脱敏，不含 token）
- POST /api/scan               立刻触发一次扫盘
- POST /api/heartbeat          立刻触发一次心跳
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ClientConfig
from .worker import Worker

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_worker_ref: Optional[Worker] = None  # 由 __main__ 注入


def set_worker(w: Worker) -> None:
    global _worker_ref
    _worker_ref = w


def create_app(cfg: ClientConfig) -> FastAPI:
    app = FastAPI(
        title="capcut-draft client",
        version=cfg.version,
        docs_url="/docs",
    )

    @app.get("/api/status")
    def status() -> dict:
        if _worker_ref is None:
            return {"running": False, "msg": "worker 未启动（加了 --no-worker？）"}
        return _worker_ref.status

    @app.get("/api/config")
    def config_redacted() -> dict:
        return {
            "server_url": cfg.server.url,
            "client_name": cfg.client_name,
            "hostname": cfg.hostname,
            "client_id": cfg.client_id,
            "token_set": bool(cfg.client_token),
            "token_prefix": (cfg.client_token[:12] + "...") if cfg.client_token else None,
            "assets_dirs": [str(p) for p in cfg.assets.dirs],
            "worker": {
                "poll_interval_sec": cfg.worker.poll_interval_sec,
                "heartbeat_interval_sec": cfg.worker.heartbeat_interval_sec,
                "output_dir": str(cfg.worker.output_dir),
                "one_at_a_time": cfg.worker.one_at_a_time,
            },
            "ui": {
                "host": cfg.ui.host,
                "port": cfg.ui.port,
            },
            "privacy": {
                "server_has_files": False,  # ← 关键：云端不缓存文件内容
                "uploaded_metadata_only": True,
            },
        }

    @app.post("/api/scan")
    def trigger_scan() -> dict:
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        _worker_ref._do_scan()  # noqa: SLF001
        return {"ok": True, "msg": "扫盘已触发"}

    @app.post("/api/heartbeat")
    def trigger_heartbeat() -> dict:
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        _worker_ref._do_heartbeat()  # noqa: SLF001
        return {"ok": True, "msg": "心跳已发送"}

    @app.get("/")
    def index() -> FileResponse:
        idx = STATIC_DIR / "index.html"
        if not idx.exists():
            raise HTTPException(404, "UI 暂未提供")
        return FileResponse(idx, media_type="text/html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def run_app(cfg: ClientConfig, *, port: int = 8001, host: str = "127.0.0.1") -> None:
    """起本地 FastAPI（uvicorn）。只绑 127.0.0.1，公网不可达。"""
    import uvicorn
    app = create_app(cfg)
    log.info("本地 UI 启动: http://%s:%d/", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
