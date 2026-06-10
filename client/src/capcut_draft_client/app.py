"""本地 FastAPI（只绑 127.0.0.1，绝不暴露公网）。

端点：
- GET   /                       极简 dashboard UI（含已上传草稿 tab）
- GET   /api/status             worker 当前状态
- GET   /api/config             客户端配置（脱敏，不含 token）
- POST  /api/scan               立刻触发一次扫盘
- POST  /api/heartbeat          立刻触发一次心跳
- GET   /api/drafts             列云端草稿（透传到服务端）
- DELETE /api/drafts/{id}       删云端草稿
- GET   /api/drafts/quota       查云端 quota
- GET   /api/drafts/pending     列出本地待重传的 .zip
- POST  /api/drafts/retry-pending   立刻重传一次 pending_uploads/
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
PENDING_DIR = Path.home() / ".capcut-draft" / "pending_uploads"

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

    # -------- 草稿云端存储（透传到服务端） --------

    @app.get("/api/drafts")
    def list_drafts(
        q: Optional[str] = None,
        sort: str = "created_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """列云端草稿（带分页/搜索），普通用户只看自己。"""
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        return _worker_ref.api.list_drafts(
            q=q, sort=sort, page=page, page_size=page_size,
        )

    @app.delete("/api/drafts/{draft_id}")
    def delete_draft(draft_id: int) -> dict:
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        return _worker_ref.api.delete_draft(draft_id)

    @app.get("/api/drafts/quota")
    def draft_quota() -> dict:
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        return _worker_ref.api.get_draft_quota()

    @app.get("/api/drafts/pending")
    def list_pending_uploads() -> dict:
        """本地待重传的 .zip 列表（上传失败被搬到 PENDING_DIR 里的）。"""
        items: list[dict] = []
        if PENDING_DIR.exists():
            for z in sorted(PENDING_DIR.glob("*.zip")):
                meta_path = z.with_suffix(z.suffix + ".meta.json")
                meta: dict = {}
                if meta_path.exists():
                    try:
                        import json
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                items.append({
                    "filename": z.name,
                    "size": z.stat().st_size,
                    "mtime": z.stat().st_mtime,
                    "meta": meta,
                })
        return {"items": items, "dir": str(PENDING_DIR), "count": len(items)}

    @app.post("/api/drafts/retry-pending")
    def retry_pending_uploads() -> dict:
        """重传 PENDING_DIR 里的 .zip。同步执行，给前端一个明确的成功/失败结果。"""
        if _worker_ref is None:
            raise HTTPException(503, "worker 未启动")
        if not PENDING_DIR.exists():
            return {"ok": True, "msg": "无待重传文件", "uploaded": 0, "failed": 0}

        uploaded, failed = 0, 0
        errors: list[str] = []
        for z in sorted(PENDING_DIR.glob("*.zip")):
            meta_path = z.with_suffix(z.suffix + ".meta.json")
            task_id = None
            try:
                import json
                if meta_path.exists():
                    task_id = (json.loads(meta_path.read_text(encoding="utf-8")) or {}).get("task_id")
            except Exception:
                pass

            def _cb(sent: int, total: int) -> None:
                # 占位：UI 拉列表时各自打日志
                pass

            r = _worker_ref.api.upload_draft(
                z, task_id=task_id, task_name=f"retry-{z.stem}", progress_callback=_cb,
            )
            st = r.get("_status", 0)
            if st == 200:
                uploaded += 1
                try:
                    z.unlink()
                    if meta_path.exists():
                        meta_path.unlink()
                except OSError:
                    pass
            else:
                failed += 1
                errors.append(f"{z.name}: {r.get('_error') or r.get('detail') or st}")

        return {
            "ok": failed == 0,
            "uploaded": uploaded,
            "failed": failed,
            "errors": errors,
        }

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
