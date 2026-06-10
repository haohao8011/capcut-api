"""FastAPI Web 服务：上传数字人视频 + B-roll，浏览器点点点就能生成剪映草稿。

启动：
    python -m capcut_draft.web
    # 或
    uvicorn capcut_draft.web:app --host 0.0.0.0 --port 8000 --reload

浏览器打开 http://localhost:8000/
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cli import _process_one  # 复用 cli 的处理函数

log = logging.getLogger(__name__)

# 路径常量
ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = ROOT / "uploads"
MAIN_DIR = UPLOAD_DIR / "main"
BROLL_DIR = UPLOAD_DIR / "broll"
OUTPUT_DIR = ROOT / "outputs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

for d in (MAIN_DIR, BROLL_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)


# -------- 数据模型 --------

JobStatus = Literal["queued", "running", "success", "failed"]


@dataclass
class JobInfo:
    id: str
    name: str
    main_file: str
    broll_files: list[str]
    options: dict
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    draft_path: str | None = None
    progress_log: list[str] = field(default_factory=list)


# 内存里的"任务库"（重启会清空；正式用可换 SQLite/Redis）
_JOBS: dict[str, JobInfo] = {}
_JOBS_LOCK = threading.Lock()

# 文件 id → 文件路径 映射（uploads/main 或 uploads/broll）
_MAIN_FILES: dict[str, str] = {}
_BROLL_FILES: dict[str, str] = {}
_FILES_LOCK = threading.Lock()


# -------- FastAPI app --------

app = FastAPI(
    title="capcut-draft API",
    description="数字人视频 + B-roll + ASR 字幕 → 剪映草稿",
    version="0.1.0",
)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """把未处理的异常以 JSON 返回给前端，方便调试。"""
    log.error("Unhandled error on %s %s: %s", request.method, request.url, exc)
    log.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.on_event("startup")
def _startup() -> None:
    _setup_logging()


# -------- 静态文件 / 首页 --------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# 用 PIL 生成一张 16x16 青色 "C" 图标。装了 funasr / pyJianYingDraft 基本会顺带把
# Pillow 拉进来；如果实在没装，fallback 到 1x1 透明 PNG。
def _make_favicon_png() -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        # 1x1 透明 PNG（最少 67 字节）
        import base64 as _b64
        return _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
            "2mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
        )
    img = Image.new("RGBA", (16, 16), (10, 11, 14, 255))  # --bg-0
    d = ImageDraw.Draw(img)
    # 画一个青色环 + 中心镂空，做成 "C" 形
    cyan = (0, 212, 255, 255)
    # 圆环
    d.ellipse((1, 1, 14, 14), outline=cyan, width=3)
    # 抹掉右侧一小段，做成 C
    d.rectangle((9, 5, 15, 11), fill=(10, 11, 14, 255))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


_FAVICON_PNG = _make_favicon_png()


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """给浏览器标签一个青色 C 图标，免得日志里 404。"""
    return Response(content=_FAVICON_PNG, media_type="image/png")


# -------- 上传接口 --------

_ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _save_upload(file: UploadFile, dest_dir: Path) -> tuple[str, str]:
    """保存上传文件到 dest_dir，返回 (file_id, 绝对路径)。"""
    suffix = Path(file.filename or "").suffix.lower() or ".mp4"
    if suffix not in _ALLOWED_VIDEO_EXTS:
        raise HTTPException(400, f"不支持的视频格式: {suffix}")
    file_id = f"{uuid.uuid4().hex}{suffix}"
    dest = dest_dir / file_id
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return file_id, str(dest)


@app.post("/api/upload-main")
async def upload_main(file: UploadFile = File(...)) -> dict:
    """上传主视频（数字人/口播），返回 file_id，后续用这个 id 启动任务。"""
    file_id, path = _save_upload(file, MAIN_DIR)
    with _FILES_LOCK:
        _MAIN_FILES[file_id] = path
    return {"file_id": file_id, "filename": file.filename, "path": path}


@app.post("/api/upload-broll")
async def upload_broll(files: list[UploadFile] = File(...)) -> dict:
    """批量上传 B-roll 素材，返回 file_id 列表。"""
    ids: list[str] = []
    paths: list[str] = []
    for f in files:
        file_id, path = _save_upload(f, BROLL_DIR)
        ids.append(file_id)
        paths.append(path)
    with _FILES_LOCK:
        for fid, p in zip(ids, paths):
            _BROLL_FILES[fid] = p
    return {"file_ids": ids, "count": len(ids)}


# -------- 任务接口 --------


class JobOptions(BaseModel):
    pause_threshold: float = 0.6
    min_cut_interval: float = 2.5
    max_cuts: int | None = None
    broll_duration: float = 2.5
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    add_subtitles: bool = True
    skip_asr: bool = False
    name: str = "AI合成"


class CreateJobReq(BaseModel):
    name: str = "AI合成"
    main_file_id: str
    broll_file_ids: list[str]
    options: JobOptions = JobOptions()


def _run_job_sync(job: JobInfo, main_path: str, broll_paths: list[str], options: dict) -> None:
    """在后台线程里跑处理。"""
    job.status = "running"
    job.started_at = time.time()
    job.progress_log.append("开始处理")

    # 用 logging 把 capcut-draft 的日志也存到 progress_log
    capcut_logger = logging.getLogger("capcut-draft")
    handler_id = f"job_{job.id}"
    job_log = logging.getLogger(handler_id)
    job_log.setLevel(logging.INFO)

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            job.progress_log.append(self.format(record))

    h = _ListHandler()
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    capcut_logger.addHandler(h)

    try:
        p = _process_one(
            Path(main_path),
            [Path(p) for p in broll_paths],
            OUTPUT_DIR,
            options.get("name", job.name),
            pause_threshold=options.get("pause_threshold", 0.6),
            min_cut_interval=options.get("min_cut_interval", 2.5),
            max_cuts=options.get("max_cuts"),
            broll_duration=options.get("broll_duration", 2.5),
            width=options.get("width", 1080),
            height=options.get("height", 1920),
            fps=options.get("fps", 30.0),
            add_subtitles=options.get("add_subtitles", True),
            skip_asr=options.get("skip_asr", False),
            log=capcut_logger,
        )
        job.draft_path = p
        job.status = "success"
        job.progress_log.append(f"完成: {p}")
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.progress_log.append(f"失败: {e}")
        log.exception("job %s failed", job.id)
    finally:
        capcut_logger.removeHandler(h)
        job.finished_at = time.time()


@app.post("/api/jobs", status_code=201)
def create_job(req: CreateJobReq, bg: BackgroundTasks) -> dict:
    with _FILES_LOCK:
        main_path = _MAIN_FILES.get(req.main_file_id)
        broll_paths = [_BROLL_FILES[fid] for fid in req.broll_file_ids if fid in _BROLL_FILES]
    if not main_path or not Path(main_path).exists():
        raise HTTPException(400, f"主视频不存在或已过期: {req.main_file_id}")
    if not broll_paths:
        raise HTTPException(400, "请至少上传一个 B-roll 素材")

    job = JobInfo(
        id=uuid.uuid4().hex[:12],
        name=req.name,
        main_file=Path(main_path).name,
        broll_files=[Path(p).name for p in broll_paths],
        options=req.options.model_dump() | {"name": req.name},
    )
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    bg.add_task(_run_job_sync, job, main_path, broll_paths, job.options)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with _JOBS_LOCK:
        items = [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status,
                "main_file": j.main_file,
                "broll_count": len(j.broll_files),
                "created_at": j.created_at,
                "finished_at": j.finished_at,
                "draft_path": j.draft_path,
                "error": j.error,
            }
            for j in _JOBS.values()
        ]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"jobs": items, "count": len(items)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return {
        "id": j.id,
        "name": j.name,
        "status": j.status,
        "main_file": j.main_file,
        "broll_files": j.broll_files,
        "options": j.options,
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "error": j.error,
        "draft_path": j.draft_path,
        "progress": j.progress_log[-30:],
    }


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
    if not j or not j.draft_path:
        raise HTTPException(404, "草稿不存在或任务未完成")
    draft = Path(j.draft_path)
    if not draft.exists():
        raise HTTPException(404, "草稿文件夹已丢失")

    # 打包成 zip
    zip_path = OUTPUT_DIR / f"{job_id}.zip"
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in draft.rglob("*"):
                zf.write(p, p.relative_to(draft.parent))
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"{j.name}.zip",
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        if j.draft_path:
            shutil.rmtree(j.draft_path, ignore_errors=True)
        zip_path = OUTPUT_DIR / f"{job_id}.zip"
        zip_path.unlink(missing_ok=True)
        del _JOBS[job_id]
    return {"deleted": job_id}


def main() -> None:
    """命令行入口：`python -m capcut_draft.web`"""
    import uvicorn
    _setup_logging()
    uvicorn.run(
        "capcut_draft.web:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
