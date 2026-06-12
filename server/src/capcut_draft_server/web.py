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
import threading
import time
import uuid
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth as auth_mod
from . import db_models
from . import web_admin, web_assets, web_clients, web_drafts, web_folders, web_tasks, web_uploads

# -------- 限流器（防暴力破解） --------
# 默认 5 次/分钟/IP；CAPCUT_LOGIN_RATE_LIMIT=0 可关闭（测试用）
_LOGIN_RATE_LIMIT = os.environ.get("CAPCUT_LOGIN_RATE_LIMIT", "5/minute")
_limiter = None
if _LOGIN_RATE_LIMIT and _LOGIN_RATE_LIMIT != "0":
    try:
        from slowapi import Limiter
        from slowapi.errors import RateLimitExceeded
        from slowapi.util import get_remote_address
        _limiter = Limiter(key_func=get_remote_address)
    except ImportError:  # 慢api 没装就降级为不限流
        log.warning("slowapi 未安装，登录限流已禁用")

log = logging.getLogger(__name__)

# 路径常量
ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"          # 内部静态（404、favicon）
ADMIN_DIR = ROOT.parent / "admin"                                 # 管理后台 UI
FRONTEND_DIR = ROOT.parent / "frontend"                           # 用户工作台 UI
# 优先用 monorepo 顶层 config/（monorepo 重构后的位置），fallback 到 server/config/
_TOP_CONFIG = ROOT.parent / "config" if (ROOT.parent / "config").exists() else None
CONFIG_DIR = _TOP_CONFIG if _TOP_CONFIG else (ROOT / "config")
BUILTIN_WF_FILE = CONFIG_DIR / "workflows.builtin.json"
USER_WF_FILE = CONFIG_DIR / "workflows.user.json"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
# 用户工作流文件不存在时建空数组
if not USER_WF_FILE.exists():
    USER_WF_FILE.write_text("[]\n", encoding="utf-8")


# -------- 数据模型 --------


# -------- FastAPI app --------

app = FastAPI(
    title="capcut-draft API",
    description="数字人视频 + B-roll + ASR 字幕 → 剪映草稿",
    version="0.1.0",
)

# 慢api 限流器注册（如果启用了）
if _limiter is not None:
    app.state.limiter = _limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        # 防止弱密码被暴力破解：超限后 429
        log.warning("登录限流触发: %s %s", request.client.host if request.client else "?", exc.detail)
        return JSONResponse(
            status_code=429,
            content={"detail": f"请求过于频繁：{exc.detail}。请稍后再试。"},
        )


@app.exception_handler(StarletteHTTPException)
async def custom_404_handler(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    """自定义 404 页面：现代深色风格。"""
    if exc.status_code == 404:
        # API 路径仍然返回 JSON
        if str(request.url.path).startswith("/api/"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        html = (STATIC_DIR / "404.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html, status_code=404)
    # 其他 HTTP 错误保持默认
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """把未处理的异常以 JSON 返回给前端，方便调试。"""
    log.exception("Unhandled error on %s %s: %s", request.method, request.url, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )


def _setup_logging() -> None:
    """日志初始化：默认纯文本；CAPCUT_LOG_JSON=1 切 JSON 结构化（企业 ELK/Loki 友好）。"""
    from .log_json import setup_json_logging
    if os.environ.get("CAPCUT_LOG_JSON", "0") == "1":
        setup_json_logging()
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


# -------- 云端定期清理（草稿是用户资产不删；只清 task_logs + 离线 client） --------

# 默认阈值（秒），可通过环境变量覆盖
CLEANUP_INTERVAL_SEC = int(os.environ.get("CAPCUT_CLEANUP_INTERVAL", "3600"))   # 1h
CLEANUP_LOG_MAX_AGE = int(os.environ.get("CAPCUT_CLEANUP_LOG_AGE", "2592000"))  # 30d
CLEANUP_OFFLINE_CLIENT_DAYS = int(os.environ.get("CAPCUT_CLEANUP_OFFLINE_DAYS", "30"))  # 30d 未心跳
CLEANUP_AUDIT_RETENTION_DAYS = int(os.environ.get("CAPCUT_AUDIT_RETENTION_DAYS", "180"))  # 审计保留 6 个月（合规）


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


def _cleanup_audit_once(db) -> int:
    """清超过保留期的审计日志。"""
    from .audit import cleanup_old_audit_logs
    return cleanup_old_audit_logs(db, CLEANUP_AUDIT_RETENTION_DAYS)


def _cleanup_loop() -> None:
    """后台线程：每 CLEANUP_INTERVAL_SEC 跑一次清理。"""
    log.info("[cleanup] 启动定期清理线程（间隔 %ds）", CLEANUP_INTERVAL_SEC)
    while True:
        try:
            time.sleep(CLEANUP_INTERVAL_SEC)
            from . import auth as _a
            with _a.SessionLocal() as db:
                r2 = _cleanup_db_once(db)
            if r2.get("logs_deleted") or r2.get("clients_marked_offline"):
                log.info("[cleanup] 本轮: 日志 %d 条 / client %d 个",
                         r2.get("logs_deleted", 0),
                         r2.get("clients_marked_offline", 0))
            # 审计日志清理
            n_audit = _cleanup_audit_once(_a.SessionLocal())
            if n_audit:
                log.info("[cleanup] 本轮: 审计日志 %d 条", n_audit)
        except Exception as e:
            log.exception("[cleanup] 出错（继续下一轮）: %s", e)


@app.on_event("startup")
def _startup() -> None:
    _setup_logging()
    # 建所有表（users / clients / assets / tasks / task_logs / setup_codes / drafts / draft_shares）
    db_models.init_all_tables()
    if auth_mod.seed_admin():
        log.warning("已创建默认管理员 xiaoma（密码 niubi666），首次登录后请改密！")
    # 起后台清理线程（守护线程，主进程退出它自动退）
    t = threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True)
    t.start()


# -------- 静态文件挂载 --------

# 内部静态（404.html、favicon.jpg）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# 管理后台 UI 和用户工作台 UI 用 SPA catch-all 路由处理（见下方）

# -------- C/S 路由（拆分文件） --------
app.include_router(web_clients.router)
app.include_router(web_assets.router)
app.include_router(web_tasks.router)
app.include_router(web_drafts.router)
app.include_router(web_uploads.router)
app.include_router(web_admin.router)
app.include_router(web_folders.router)


# -------- 重定向路由（保持向后兼容） --------

@app.get("/", include_in_schema=False)
def root_redirect():
    """根路径 → 用户工作台。"""
    return RedirectResponse(url="/app/")


@app.get("/console", include_in_schema=False)
def console_redirect():
    """管理后台 → /admin/。"""
    return RedirectResponse(url="/admin/")


@app.get("/console/login", include_in_schema=False)
def console_login_redirect():
    """管理后台登录 → /admin/login.html。"""
    return RedirectResponse(url="/admin/login.html")


@app.get("/login", include_in_schema=False)
def login_redirect():
    """用户登录 → /app/login.html。"""
    return RedirectResponse(url="/app/login.html")


# -------- SPA History 路由（catch-all） --------
# 前端用 History API 路由，刷新页面时需要服务端返回 index.html
# 不使用 StaticFiles(html=True) mount，因为 mount 会拦截所有请求导致 catch-all 失效

from fastapi.responses import FileResponse


def _serve_spa(base_dir: Path, path: str):
    """SPA 路由：优先返回真实文件，否则返回 index.html。"""
    if path and path != "/":
        real_file = base_dir / path
        if real_file.is_file():
            return FileResponse(str(real_file))
    index = base_dir / "index.html"
    if index.is_file():
        return HTMLResponse(content=index.read_text(encoding="utf-8"))
    raise HTTPException(404, "index.html not found")


@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
def spa_frontend_root():
    return _serve_spa(FRONTEND_DIR, "/")


@app.get("/app/{path:path}", include_in_schema=False)
def spa_frontend(path: str):
    return _serve_spa(FRONTEND_DIR, path)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def spa_admin_root():
    return _serve_spa(ADMIN_DIR, "/")


@app.get("/admin/{path:path}", include_in_schema=False)
def spa_admin(path: str):
    return _serve_spa(ADMIN_DIR, path)


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


# 登录限流装饰器（仅在启用了 slowapi 时生效；测试时设 CAPCUT_LOGIN_RATE_LIMIT=0 关闭）
def _login_rate_limit(func):
    if _limiter is not None and _LOGIN_RATE_LIMIT and _LOGIN_RATE_LIMIT != "0":
        return _limiter.limit(_LOGIN_RATE_LIMIT)(func)
    return func


@app.post("/api/auth/login")
@_login_rate_limit
def auth_login(req: LoginReq, request: Request, db=Depends(auth_mod.get_db)) -> dict:
    """公开：用户名+密码登录，返回 access + refresh token。

    限流（防暴力破解）：默认 5 次/分钟/IP，通过 CAPCUT_LOGIN_RATE_LIMIT 调。
    测试时设 CAPCUT_LOGIN_RATE_LIMIT=0 关闭。
    """
    from datetime import datetime, timezone
    from .audit import log_audit
    u = auth_mod.authenticate_user(db, req.username, req.password)
    if not u:
        # 失败登录也要记审计（合规：异常登录检测）
        log_audit(db, request=request, actor_id=None, actor_type="anonymous",
                  action="user.login", status="failure",
                  extra={"attempted_username": req.username})
        raise HTTPException(401, "用户名或密码错误")
    u.last_login_at = datetime.now(timezone.utc)
    db.commit()
    log_audit(db, request=request, actor_id=u.id, actor_type="user",
              action="user.login", status="success",
              extra={"is_admin": u.is_admin})
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
    request: Request,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db=Depends(auth_mod.get_db),
) -> dict:
    """需登录：改自己的密码。"""
    from .audit import log_audit
    auth_mod.change_password(db, user, req.old_password, req.new_password)
    log_audit(db, request=request, actor_id=user.id, actor_type="user",
              action="user.password_change", status="success")
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
def admin_create_user(
    req: CreateUserReq,
    request: Request,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db=Depends(auth_mod.get_db),
) -> dict:
    """管理员：新建用户。"""
    from .audit import log_audit
    u = auth_mod.create_user(db, req.username, req.password,
                              email=req.email, is_admin=req.is_admin)
    log_audit(db, request=request, actor_id=user.id, actor_type="user",
              action="user.create", resource="user", resource_id=u.id,
              extra={"username": u.username, "is_admin": u.is_admin})
    return {"id": u.id, "username": u.username, "is_admin": u.is_admin}


@app.delete("/api/auth/users/{uid}",
            dependencies=[Depends(auth_mod.require_admin)])
def admin_delete_user(
    uid: int,
    request: Request,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db=Depends(auth_mod.get_db),
) -> dict:
    """管理员：删除用户（不能删自己）。"""
    from .audit import log_audit
    if uid == user.id:
        raise HTTPException(400, "不能删除自己")
    target = db.get(auth_mod.User, uid)
    if not target:
        raise HTTPException(404, f"用户不存在: {uid}")
    if target.username == "xiaoma":
        raise HTTPException(400, "不能删除内置默认管理员 xiaoma")
    target_username = target.username
    db.delete(target)
    db.commit()
    log_audit(db, request=request, actor_id=user.id, actor_type="user",
              action="user.delete", resource="user", resource_id=uid,
              extra={"deleted_username": target_username})
    return {"deleted": uid}


@app.post("/api/auth/users/{uid}/reset-password",
          dependencies=[Depends(auth_mod.require_admin)])
def admin_reset_password(
    uid: int, req: ResetPwdReq, request: Request,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db=Depends(auth_mod.get_db),
) -> dict:
    """管理员：重置某用户密码。"""
    from .audit import log_audit
    target = db.get(auth_mod.User, uid)
    if not target:
        raise HTTPException(404, f"用户不存在: {uid}")
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密码太短（至少 6 位）")
    target.password_hash = auth_mod.hash_pwd(req.new_password)
    db.commit()
    log_audit(db, request=request, actor_id=user.id, actor_type="user",
              action="user.password_reset", resource="user", resource_id=uid,
              extra={"target_username": target.username})
    return {"ok": True, "uid": uid}


# -------- favicon（用户自定义头像） --------

_FAVICON_PATH = STATIC_DIR / "favicon.jpg"
_FAVICON_BYTES: bytes | None = None
_FAVICON_MIME = "image/jpeg"

def _load_favicon() -> bytes:
    """读取 favicon 文件，缓存到内存。"""
    global _FAVICON_BYTES
    if _FAVICON_BYTES is None:
        if _FAVICON_PATH.exists():
            _FAVICON_BYTES = _FAVICON_PATH.read_bytes()
        else:
            # fallback: 1x1 透明 PNG
            import base64 as _b64
            _FAVICON_BYTES = _b64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
                "2mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
            )
            global _FAVICON_MIME
            _FAVICON_MIME = "image/png"
    return _FAVICON_BYTES


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """返回自定义头像作为网站图标。"""
    return Response(content=_load_favicon(), media_type=_FAVICON_MIME)


def main() -> None:
    """命令行入口：`python -m capcut_draft_server.web`"""
    import uvicorn
    _setup_logging()
    uvicorn.run(
        "capcut_draft_server.web:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
