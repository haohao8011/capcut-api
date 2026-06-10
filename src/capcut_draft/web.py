"""FastAPI Web 服务：上传数字人视频 + B-roll，浏览器点点点就能生成剪映草稿。

启动：
    python -m capcut_draft.web
    # 或
    uvicorn capcut_draft.web:app --host 0.0.0.0 --port 8000 --reload

浏览器打开 http://localhost:8000/
"""
from __future__ import annotations

import json
import logging
import os
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
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth as auth_mod
from . import db_models
from . import web_assets, web_clients, web_tasks
from .cli import _process_one  # 复用 cli 的处理函数

log = logging.getLogger(__name__)

# 路径常量
ROOT = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = ROOT / "uploads"
MAIN_DIR = UPLOAD_DIR / "main"
BROLL_DIR = UPLOAD_DIR / "broll"
OUTPUT_DIR = ROOT / "outputs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_DIR = ROOT / "config"
BUILTIN_WF_FILE = CONFIG_DIR / "workflows.builtin.json"
USER_WF_FILE = CONFIG_DIR / "workflows.user.json"

for d in (MAIN_DIR, BROLL_DIR, OUTPUT_DIR, CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)
# 用户工作流文件不存在时建空数组
if not USER_WF_FILE.exists():
    USER_WF_FILE.write_text("[]\n", encoding="utf-8")


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


# -------- 云端定期清理（用户需求：草稿/数字人/素材全在本地，云端不缓存） --------

# 默认阈值（秒），可通过环境变量覆盖
CLEANUP_INTERVAL_SEC = int(os.environ.get("CAPCUT_CLEANUP_INTERVAL", "3600"))   # 1h
CLEANUP_UPLOAD_MAX_AGE = int(os.environ.get("CAPCUT_CLEANUP_UPLOAD_AGE", "604800"))  # 7d
CLEANUP_ZIP_MAX_AGE = int(os.environ.get("CAPCUT_CLEANUP_ZIP_AGE", "604800"))  # 7d
CLEANUP_LOG_MAX_AGE = int(os.environ.get("CAPCUT_CLEANUP_LOG_AGE", "2592000"))  # 30d
CLEANUP_OFFLINE_CLIENT_DAYS = int(os.environ.get("CAPCUT_CLEANUP_OFFLINE_DAYS", "30"))  # 30d 未心跳


def _cleanup_uploads_once() -> dict:
    """清 uploads/main + uploads/broll 里超过阈值的旧文件（**这些是旧的"上传到云端处理"模式
    留下的；C/S 模式不经过这里**）。"""
    now = time.time()
    removed = 0
    bytes_freed = 0
    for d in (MAIN_DIR, BROLL_DIR):
        if not d.exists():
            continue
        for p in d.iterdir():
            try:
                if not p.is_file():
                    continue
                age = now - p.stat().st_mtime
                if age > CLEANUP_UPLOAD_MAX_AGE:
                    size = p.stat().st_size
                    p.unlink()
                    removed += 1
                    bytes_freed += size
                    log.info("[cleanup] 旧上传文件: %s (%.1f MB, %d 天前)",
                             p.name, size / 1024 / 1024, int(age / 86400))
            except OSError as e:
                log.debug("[cleanup] 跳过 %s: %s", p, e)
    # 清 outputs/*.zip 里超过阈值的（draft 文件夹本身如果用户没下载也会留下，按 7d 一起清）
    if OUTPUT_DIR.exists():
        for p in OUTPUT_DIR.glob("*.zip"):
            try:
                age = now - p.stat().st_mtime
                if age > CLEANUP_ZIP_MAX_AGE:
                    size = p.stat().st_size
                    p.unlink()
                    removed += 1
                    bytes_freed += size
                    log.info("[cleanup] 旧 zip: %s (%.1f MB, %d 天前)",
                             p.name, size / 1024 / 1024, int(age / 86400))
            except OSError:
                pass
    return {"removed": removed, "bytes_freed": bytes_freed}


def _cleanup_db_once(db) -> dict:
    """清 task_logs 里超过阈值的旧日志；标记 30 天没心跳的 client 为离线。"""
    from datetime import datetime, timedelta, timezone
    cutoff_log = datetime.now(timezone.utc) - timedelta(seconds=CLEANUP_LOG_MAX_AGE)
    cutoff_offline = datetime.now(timezone.utc) - timedelta(days=CLEANUP_OFFLINE_CLIENT_DAYS)

    from sqlalchemy import delete as _del, update as _upd
    n_logs = db.execute(
        _del(db_models.TaskLog).where(db_models.TaskLog.ts < cutoff_log)
    ).rowcount

    n_clients = db.execute(
        _upd(db_models.Client)
        .where(db_models.Client.last_seen_at < cutoff_offline, db_models.Client.is_online == True)  # noqa: E712
        .values(is_online=False)
    ).rowcount

    db.commit()
    if n_logs or n_clients:
        log.info("[cleanup] DB: 删 %d 条旧日志, 标记 %d 个 client 离线", n_logs, n_clients)
    return {"logs_deleted": n_logs, "clients_marked_offline": n_clients}


def _cleanup_loop() -> None:
    """后台线程：每 CLEANUP_INTERVAL_SEC 跑一次清理。"""
    log.info("[cleanup] 启动定期清理线程（间隔 %ds）", CLEANUP_INTERVAL_SEC)
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL_SEC)
            r1 = _cleanup_uploads_once()
            from . import auth as _a
            with _a.SessionLocal() as db:
                r2 = _cleanup_db_once(db)
            if r1.get("removed") or r2.get("logs_deleted") or r2.get("clients_marked_offline"):
                log.info("[cleanup] 本轮: 文件 %d 项 / 日志 %d 条 / client %d 个",
                         r1.get("removed", 0),
                         r2.get("logs_deleted", 0),
                         r2.get("clients_marked_offline", 0))
        except Exception as e:
            log.exception("[cleanup] 出错（继续下一轮）: %s", e)


@app.on_event("startup")
def _startup() -> None:
    _setup_logging()
    # 建所有表（users / clients / assets / tasks / task_logs）
    db_models.init_all_tables()
    if auth_mod.seed_admin():
        log.warning("已创建默认管理员 xiaoma（密码 niubi666），首次登录后请改密！")
    # 起后台清理线程（守护线程，主进程退出它自动退）
    t = threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True)
    t.start()
    # 启动后跑一次
    try:
        r = _cleanup_uploads_once()
        log.info("[cleanup] 启动时清扫: %d 项", r.get("removed", 0))
    except Exception as e:
        log.warning("[cleanup] 启动时清扫失败（忽略）: %s", e)


# -------- 静态文件 / 首页 --------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# -------- C/S 路由（拆分文件） --------
app.include_router(web_clients.router)
app.include_router(web_assets.router)
app.include_router(web_tasks.router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page() -> HTMLResponse:
    """独立登录页：未登录时跳到这里。"""
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# -------- 工作流管理 --------

def _load_json(path: Path, default):
    """读 JSON，文件不存在/损坏时返回 default 并打 log。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("workflow load failed: %s (%s)", path, e)
        return default


def _save_json(path: Path, data) -> None:
    """原子写 JSON（先写 .tmp 再 rename，防中途崩溃损坏）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _list_workflows() -> list[dict]:
    builtin = _load_json(BUILTIN_WF_FILE, [])
    user = _load_json(USER_WF_FILE, [])
    # 防止用户在 user 里手贱加 builtin=True / id 冲突
    for w in builtin:
        w.setdefault("builtin", True)
    for w in user:
        w["builtin"] = False
    return builtin + user


def _validate_wf_options(options: dict) -> dict:
    """裁掉无效 key，转换类型。"""
    out: dict = {}
    schema = {
        "pause_threshold": (float, 0.6),
        "min_cut_interval": (float, 2.5),
        "max_cuts": (int, None),
        "broll_duration": (float, 2.5),
        "add_subtitles": (bool, True),
        "skip_asr": (bool, False),
    }
    for k, (typ, default) in schema.items():
        if k not in options:
            out[k] = default
            continue
        v = options[k]
        if v is None:
            out[k] = None
            continue
        if typ is bool:
            out[k] = bool(v)
        elif typ is int:
            out[k] = int(v)
        else:
            out[k] = float(v)
    return out


@app.get("/api/workflows", dependencies=[Depends(auth_mod.get_current_user)])
def list_workflows() -> dict:
    """返回全部工作流：内置 + 用户。"""
    return {"workflows": _list_workflows()}


class SaveWorkflowReq(BaseModel):
    name: str
    icon: str = "📌"
    description: str = ""
    tags: list[str] = []
    options: dict


@app.post("/api/workflows", status_code=201)
def save_user_workflow(req: SaveWorkflowReq) -> dict:
    """保存当前参数为用户工作流。id 自动生成。"""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    if len(name) > 32:
        raise HTTPException(400, "name 太长（>32）")
    wf = {
        "id": "u_" + uuid.uuid4().hex[:10],
        "name": name,
        "icon": req.icon or "📌",
        "description": req.description or "",
        "tags": [str(t)[:16] for t in (req.tags or [])][:6],
        "builtin": False,
        "options": _validate_wf_options(req.options),
    }
    user = _load_json(USER_WF_FILE, [])
    user.append(wf)
    _save_json(USER_WF_FILE, user)
    return wf


@app.delete("/api/workflows/{wf_id}")
def delete_user_workflow(wf_id: str) -> dict:
    """删除用户工作流。内置的不能删。"""
    if not wf_id.startswith("u_"):
        raise HTTPException(400, "只能删除用户工作流（id 必须以 u_ 开头）")
    user = _load_json(USER_WF_FILE, [])
    kept = [w for w in user if w["id"] != wf_id]
    if len(kept) == len(user):
        raise HTTPException(404, f"工作流不存在: {wf_id}")
    _save_json(USER_WF_FILE, kept)
    return {"deleted": wf_id, "remaining": len(kept)}


# -------- 鉴权路由 --------

class LoginReq(BaseModel):
    username: str
    password: str


class RefreshReq(BaseModel):
    refresh_token: str


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


class CreateUserReq(BaseModel):
    username: str
    password: str
    email: str | None = None
    is_admin: bool = False


class ResetPwdReq(BaseModel):
    new_password: str


@app.post("/api/auth/login")
def auth_login(req: LoginReq, db=Depends(auth_mod.get_db)) -> dict:
    """公开：用户名+密码登录，返回 access + refresh token。"""
    from datetime import datetime, timezone
    u = auth_mod.authenticate_user(db, req.username, req.password)
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    u.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "access_token": auth_mod.make_token(u.id, u.username, u.is_admin,
                                             kind="access", ttl=auth_mod.ACCESS_TTL_SEC),
        "refresh_token": auth_mod.make_token(u.id, u.username, u.is_admin,
                                              kind="refresh", ttl=auth_mod.REFRESH_TTL_SEC),
        "token_type": "bearer",
        "user": {
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
        },
    }


@app.post("/api/auth/refresh")
def auth_refresh(req: RefreshReq) -> dict:
    """公开：用 refresh token 换新的 access token。"""
    import jwt as _jwt
    try:
        p = auth_mod.decode_token(req.refresh_token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(401, "refresh token 已过期，请重新登录")
    except _jwt.InvalidTokenError:
        raise HTTPException(401, "refresh token 无效")
    if p.get("typ") != "refresh":
        raise HTTPException(401, "需要 refresh token")
    return {
        "access_token": auth_mod.make_token(int(p["sub"]), p["uname"], p.get("adm", False),
                                             kind="access", ttl=auth_mod.ACCESS_TTL_SEC),
        "token_type": "bearer",
    }


@app.get("/api/auth/me", dependencies=[Depends(auth_mod.get_current_user)])
def auth_me(user: auth_mod.User = Depends(auth_mod.get_current_user)) -> dict:
    """需登录：当前用户信息。"""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


@app.post("/api/auth/change-password",
          dependencies=[Depends(auth_mod.get_current_user)])
def auth_change_password(
    req: ChangePwdReq,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db=Depends(auth_mod.get_db),
) -> dict:
    """需登录：改自己的密码。"""
    auth_mod.change_password(db, user, req.old_password, req.new_password)
    return {"ok": True}


@app.get("/api/auth/users", dependencies=[Depends(auth_mod.require_admin)])
def list_users(db=Depends(auth_mod.get_db)) -> dict:
    """管理员：列出所有用户。"""
    from sqlalchemy import select as _sel
    users = db.scalars(_sel(auth_mod.User).order_by(auth_mod.User.id)).all()
    return {
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "is_admin": u.is_admin,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            }
            for u in users
        ]
    }


@app.post("/api/auth/users",
          dependencies=[Depends(auth_mod.require_admin)],
          status_code=201)
def admin_create_user(req: CreateUserReq, db=Depends(auth_mod.get_db)) -> dict:
    """管理员：新建用户。"""
    u = auth_mod.create_user(db, req.username, req.password,
                              email=req.email, is_admin=req.is_admin)
    return {"id": u.id, "username": u.username, "is_admin": u.is_admin}


@app.delete("/api/auth/users/{uid}",
            dependencies=[Depends(auth_mod.require_admin)])
def admin_delete_user(
    uid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db=Depends(auth_mod.get_db),
) -> dict:
    """管理员：删除用户（不能删自己）。"""
    if uid == user.id:
        raise HTTPException(400, "不能删除自己")
    target = db.get(auth_mod.User, uid)
    if not target:
        raise HTTPException(404, f"用户不存在: {uid}")
    if target.username == "xiaoma":
        raise HTTPException(400, "不能删除内置默认管理员 xiaoma")
    db.delete(target)
    db.commit()
    return {"deleted": uid}


@app.post("/api/auth/users/{uid}/reset-password",
          dependencies=[Depends(auth_mod.require_admin)])
def admin_reset_password(uid: int, req: ResetPwdReq, db=Depends(auth_mod.get_db)) -> dict:
    """管理员：重置某用户密码。"""
    target = db.get(auth_mod.User, uid)
    if not target:
        raise HTTPException(404, f"用户不存在: {uid}")
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密码太短（至少 6 位）")
    target.password_hash = auth_mod.hash_pwd(req.new_password)
    db.commit()
    return {"ok": True, "uid": uid}


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


@app.post("/api/upload-main",
             dependencies=[Depends(auth_mod.get_current_user)])
async def upload_main(file: UploadFile = File(...)) -> dict:
    """上传主视频（数字人/口播），返回 file_id，后续用这个 id 启动任务。"""
    file_id, path = _save_upload(file, MAIN_DIR)
    with _FILES_LOCK:
        _MAIN_FILES[file_id] = path
    return {"file_id": file_id, "filename": file.filename, "path": path}


@app.post("/api/upload-broll",
             dependencies=[Depends(auth_mod.get_current_user)])
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


@app.post("/api/jobs", status_code=201,
             dependencies=[Depends(auth_mod.get_current_user)])
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


@app.get("/api/jobs", dependencies=[Depends(auth_mod.get_current_user)])
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


@app.get("/api/jobs/{job_id}",
            dependencies=[Depends(auth_mod.get_current_user)])
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


@app.delete("/api/jobs/{job_id}",
               dependencies=[Depends(auth_mod.get_current_user)])
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
