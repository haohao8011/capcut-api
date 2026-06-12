"""素材上传 API + Admin 审核 + 系统统计。

用户端（user JWT）：
- POST   /api/uploads                    上传视频（multipart）
- GET    /api/uploads                    列当前用户上传的素材
- GET    /api/uploads/quota              素材配额查询
- GET    /api/uploads/{id}               详情
- GET    /api/uploads/{id}/file          下载/流式获取文件
- DELETE /api/uploads/{id}               删除（DB+磁盘）

Admin 审核（admin JWT）：
- GET    /api/admin/review               全部上传素材（含审核状态筛选）
- POST   /api/admin/review/{id}/flag     标记 flagged
- POST   /api/admin/review/{id}/approve  标记 approved
- POST   /api/admin/review/{id}/reject   标记 rejected
- DELETE /api/admin/review/{id}          管理员硬删
- GET    /api/admin/stats                系统统计概览

客户端下载（client token cap_xxx）：
- GET    /api/uploads/{id}/download      Worker 从服务器下载素材
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth as auth_mod
from . import db_models as models
from .db_models import UploadedAsset

log = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"])

# -------- 配置 --------

DATA_DIR = auth_mod.DATA_DIR
UPLOADS_DIR = Path(os.environ.get("CAPCUT_UPLOADS_DIR", DATA_DIR / "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# 素材配额默认值（MB）— 用户表 asset_quota_mb 字段可覆盖
DEFAULT_ASSET_QUOTA_MB = int(os.environ.get("CAPCUT_ASSET_QUOTA_MB", "3072"))  # 3GB

# 单文件上限（字节），默认 1GB
MAX_UPLOAD_BYTES = int(os.environ.get("CAPCUT_UPLOAD_MAX_BYTES", str(1024 * 1024 * 1024)))

# 允许的文件扩展名
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# MIME 类型映射
MIME_MAP = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}


# -------- 工具函数 --------

def _safe_filename(name: str) -> str:
    """防 path traversal：只保留 basename + 把危险字符替换成下划线。"""
    name = os.path.basename(name or "video.mp4")
    bad = '<>:"/\\|?*\0'
    return "".join("_" if c in bad else c for c in name)[:200] or "video.mp4"


def _upload_path(owner_id: int, filename: str) -> Path:
    """每个用户独立子目录：uploads/{owner_id}/{uuid[:8]}_{filename}"""
    owner_dir = UPLOADS_DIR / str(owner_id)
    owner_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex[:8]
    return owner_dir / f"{uid}_{filename}"


def _user_asset_used_bytes(db: Session, owner_id: int) -> int:
    """当前用户素材已用配额（字节）。"""
    total = db.scalar(
        select(func.coalesce(func.sum(UploadedAsset.size), 0))
        .where(UploadedAsset.owner_id == owner_id)
    )
    return int(total or 0)


def _user_asset_quota_bytes(db: Session, user: auth_mod.User) -> int:
    """当前用户素材 quota（字节）。0 = 不限。"""
    if user.asset_quota_mb is None:
        return DEFAULT_ASSET_QUOTA_MB * 1024 * 1024
    return user.asset_quota_mb * 1024 * 1024


def _resolve_upload_path(ua: UploadedAsset) -> Path:
    """把 DB 里的 storage_path 还原成绝对路径。"""
    sp = ua.storage_path
    p = Path(sp)
    if not p.is_absolute():
        p = (DATA_DIR / sp).resolve()
    # 安全检查：必须在 UPLOADS_DIR 子树下
    uploads_norm = os.path.normcase(str(UPLOADS_DIR))
    p_norm = os.path.normcase(str(p))
    if not p_norm.startswith(uploads_norm + os.sep) and p_norm != uploads_norm:
        log.error("[uploads] storage_path 越界: %s", p)
        p = UPLOADS_DIR / str(ua.owner_id) / ua.filename
    return p


def _fmt_mb(n: int) -> str:
    """字节数格式化成 MB/GB 字符串。"""
    if n is None:
        return "?"
    mb = n / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"


# -------- 用户端：上传 --------

@router.post("/api/uploads")
async def upload_file(
    file: Annotated[UploadFile, File(...)],
    kind: Annotated[str, Form()] = "main",
    name: Annotated[Optional[str], Form()] = None,
    duration: Annotated[Optional[float], Form()] = None,
    folder_id: Annotated[Optional[int], Form()] = None,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    """上传视频素材到服务器。

    - 白名单：.mp4/.mov/.avi/.mkv/.webm
    - 单文件 ≤ 1GB（CAPCUT_UPLOAD_MAX_BYTES）
    - 配额检查：已用 + 本次 > quota → 413
    - folder_id：可选，指定素材放到哪个文件夹
    """
    if not file.filename:
        raise HTTPException(400, "缺少文件名")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"不支持的文件格式 {ext}，允许：{', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    if kind not in ("main", "broll"):
        raise HTTPException(400, "kind 必须是 main 或 broll")

    # 验证 folder_id
    if folder_id is not None:
        from .db_models import Folder
        folder = db.get(Folder, folder_id)
        if not folder or folder.owner_id != user.id:
            raise HTTPException(404, "文件夹不存在")

    safe_name = _safe_filename(name or file.filename)
    dest = _upload_path(user.id, safe_name)

    quota_bytes = _user_asset_quota_bytes(db, user)
    used = _user_asset_used_bytes(db, user.id)

    # 提前检查 Content-Length
    if file.size is not None and file.size > 0:
        if file.size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"文件超过单文件上限 {_fmt_mb(MAX_UPLOAD_BYTES)}，拒绝接收。",
            )
        if quota_bytes > 0 and (used + file.size) > quota_bytes:
            raise HTTPException(
                413,
                f"素材配额超限：已用 {_fmt_mb(used)} / quota "
                f"{_fmt_mb(quota_bytes)}，本次文件 {_fmt_mb(file.size)}。"
                f"请删除旧素材腾出空间。",
            )

    # 流式写盘
    bytes_written = 0
    sha256 = None
    try:
        import hashlib
        h = hashlib.sha256()
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                # 二次兜底
                if quota_bytes > 0 and (used + bytes_written) > quota_bytes:
                    out.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"素材配额超限：已用 {_fmt_mb(used)} / quota "
                        f"{_fmt_mb(quota_bytes)}。请删除旧素材腾出空间。",
                    )
                if bytes_written > MAX_UPLOAD_BYTES:
                    out.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"文件超过单文件上限 {_fmt_mb(MAX_UPLOAD_BYTES)}，拒绝接收。",
                    )
                h.update(chunk)
                out.write(chunk)
            out.flush()
        sha256 = h.hexdigest()
    except HTTPException:
        raise
    except Exception as e:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        log.exception("[uploads] upload failed: %s", e)
        raise HTTPException(500, f"上传失败：{e!r}")

    # 写 DB
    storage_path = str(dest.relative_to(DATA_DIR)) if DATA_DIR in dest.parents else str(dest)
    mime_type = MIME_MAP.get(ext)

    ua = UploadedAsset(
        owner_id=user.id,
        folder_id=folder_id,
        filename=safe_name,
        storage_path=storage_path,
        kind=kind,
        size=bytes_written,
        duration=duration or 0.0,
        mime_type=mime_type,
        review_status="approved",  # 自动通过
        sha256=sha256,
    )
    db.add(ua)
    db.commit()
    db.refresh(ua)

    log.info(
        "[uploads] owner=%s uploaded asset id=%s name=%s size=%s",
        user.username, ua.id, safe_name, bytes_written,
    )

    new_used = _user_asset_used_bytes(db, user.id)
    warning = quota_bytes > 0 and new_used > quota_bytes * 0.8

    return {
        "ok": True,
        "asset": ua.to_dict(),
        "quota": {
            "used_bytes": new_used,
            "quota_bytes": quota_bytes,
            "warning": warning,
        },
    }


# -------- 用户端：列表 --------

@router.get("/api/uploads")
def list_uploads(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
    kind: Optional[str] = Query(None, description="按类型筛选: main/broll"),
    review_status: Optional[str] = Query(None, description="审核状态筛选"),
    folder_id: Optional[int] = Query(None, description="按文件夹筛选"),
) -> dict:
    """列出当前用户上传的素材。"""
    q = select(UploadedAsset).where(UploadedAsset.owner_id == user.id)
    if kind:
        q = q.where(UploadedAsset.kind == kind)
    if review_status:
        q = q.where(UploadedAsset.review_status == review_status)
    if folder_id is not None:
        q = q.where(UploadedAsset.folder_id == folder_id)
    q = q.order_by(UploadedAsset.id.desc())
    items = db.scalars(q).all()
    return {"assets": [a.to_dict() for a in items]}


@router.patch("/api/uploads/{aid}/move")
def move_asset_to_folder(
    aid: int,
    folder_id: Optional[int] = None,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    """移动素材到指定文件夹（folder_id=null 表示移到根目录）。"""
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    if ua.owner_id != user.id:
        raise HTTPException(403, "只能移动自己的素材")

    if folder_id is not None:
        from .db_models import Folder
        folder = db.get(Folder, folder_id)
        if not folder or folder.owner_id != user.id:
            raise HTTPException(404, "目标文件夹不存在")

    ua.folder_id = folder_id
    db.commit()
    return {"ok": True, "asset": ua.to_dict()}


# -------- 用户端：配额 --------

@router.get("/api/uploads/quota")
def get_asset_quota(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    used = _user_asset_used_bytes(db, user.id)
    quota = _user_asset_quota_bytes(db, user)
    warning = quota > 0 and used > quota * 0.8
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "used_mb": round(used / 1024 / 1024, 2),
        "quota_mb": round(quota / 1024 / 1024, 2) if quota else 0,
        "unlimited": quota == 0,
        "asset_count": db.scalar(
            select(func.count(UploadedAsset.id)).where(UploadedAsset.owner_id == user.id)
        ) or 0,
        "warning": warning,
    }


# -------- 用户端：详情 --------

@router.get("/api/uploads/{aid}")
def get_upload(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, f"素材不存在: {aid}")
    if ua.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "无权访问此素材")
    return ua.to_dict()


# -------- 用户端：下载文件 --------

@router.get("/api/uploads/{aid}/file")
def download_upload_file(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
):
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    if ua.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "无权下载此素材")

    path = _resolve_upload_path(ua)
    if not path.is_file():
        raise HTTPException(410, f"素材文件已丢失：{ua.filename}")

    return FileResponse(
        path=str(path),
        filename=ua.filename,
        media_type=ua.mime_type or "application/octet-stream",
    )


# -------- 用户端：删除 --------

@router.delete("/api/uploads/{aid}")
def delete_upload(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    if ua.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "只能删除自己的素材")

    # 删文件
    path = _resolve_upload_path(ua)
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:
        log.warning("[uploads] unlink 失败 %s: %s", path, e)

    db.delete(ua)
    db.commit()

    log.info("[uploads] owner=%s 删除素材 id=%s name=%s", user.username, ua.id, ua.filename)
    return {
        "ok": True,
        "asset_id": ua.id,
        "freed_bytes": ua.size,
        "quota": {
            "used_bytes": _user_asset_used_bytes(db, user.id),
            "quota_bytes": _user_asset_quota_bytes(db, user),
        },
    }


# -------- 客户端下载（cap_xxx token） --------

@router.get("/api/uploads/{aid}/download")
def client_download_upload(
    aid: int,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Session = Depends(auth_mod.get_db),
):
    """客户端 Worker 从服务器下载素材文件。"""
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")

    path = _resolve_upload_path(ua)
    if not path.is_file():
        raise HTTPException(410, f"素材文件已丢失：{ua.filename}")

    return FileResponse(
        path=str(path),
        filename=ua.filename,
        media_type=ua.mime_type or "application/octet-stream",
    )


# -------- Admin：素材审核 --------

@router.get("/api/admin/review", dependencies=[Depends(auth_mod.require_admin)])
def admin_list_review(
    db: Session = Depends(auth_mod.get_db),
    review_status: Optional[str] = Query(None, description="审核状态筛选"),
    owner_id: Optional[int] = Query(None, description="按上传者筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict:
    """Admin：列出所有上传素材（含审核状态）。"""
    q = select(UploadedAsset)
    cnt = select(func.count(UploadedAsset.id))

    if review_status:
        q = q.where(UploadedAsset.review_status == review_status)
        cnt = cnt.where(UploadedAsset.review_status == review_status)
    if owner_id is not None:
        q = q.where(UploadedAsset.owner_id == owner_id)
        cnt = cnt.where(UploadedAsset.owner_id == owner_id)

    q = q.order_by(UploadedAsset.id.desc())
    offset = (page - 1) * page_size
    items = db.scalars(q.offset(offset).limit(page_size)).all()
    total = db.scalar(cnt) or 0

    # 附带上传者用户名
    result = []
    for ua in items:
        d = ua.to_dict()
        owner = db.get(auth_mod.User, ua.owner_id)
        d["owner_username"] = owner.username if owner else None
        result.append(d)

    return {"items": result, "page": page, "page_size": page_size, "total": total}


@router.post("/api/admin/review/{aid}/flag", dependencies=[Depends(auth_mod.require_admin)])
def admin_flag_asset(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    ua.review_status = "flagged"
    ua.reviewed_by = user.id
    ua.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "asset_id": ua.id, "review_status": "flagged"}


@router.post("/api/admin/review/{aid}/approve", dependencies=[Depends(auth_mod.require_admin)])
def admin_approve_asset(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    ua.review_status = "approved"
    ua.reviewed_by = user.id
    ua.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "asset_id": ua.id, "review_status": "approved"}


@router.post("/api/admin/review/{aid}/reject", dependencies=[Depends(auth_mod.require_admin)])
def admin_reject_asset(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")
    ua.review_status = "rejected"
    ua.reviewed_by = user.id
    ua.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "asset_id": ua.id, "review_status": "rejected"}


@router.delete("/api/admin/review/{aid}", dependencies=[Depends(auth_mod.require_admin)])
def admin_delete_asset(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")

    path = _resolve_upload_path(ua)
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:
        log.warning("[uploads] admin unlink 失败 %s: %s", path, e)

    db.delete(ua)
    db.commit()
    log.info("[uploads] admin %s 删除素材 id=%s owner=%s", user.username, ua.id, ua.owner_id)
    return {"ok": True, "asset_id": ua.id, "freed_bytes": ua.size}


# -------- Admin：系统统计 --------

@router.get("/api/admin/stats", dependencies=[Depends(auth_mod.require_admin)])
def admin_stats(db: Session = Depends(auth_mod.get_db)) -> dict:
    """系统统计概览：用户数/任务数/素材占用/草稿占用/磁盘剩余。"""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    # 用户数
    user_count = db.scalar(select(func.count(auth_mod.User.id))) or 0

    # 任务统计
    total_tasks = db.scalar(select(func.count(models.Task.id))) or 0
    today_tasks = db.scalar(
        select(func.count(models.Task.id)).where(models.Task.created_at >= today_start)
    ) or 0
    week_tasks = db.scalar(
        select(func.count(models.Task.id)).where(models.Task.created_at >= week_start)
    ) or 0

    # 素材占用
    asset_total_bytes = db.scalar(
        select(func.coalesce(func.sum(UploadedAsset.size), 0))
    ) or 0

    # 草稿占用
    draft_total_bytes = db.scalar(
        select(func.coalesce(func.sum(models.Draft.size), 0))
    ) or 0
    draft_count = db.scalar(select(func.count(models.Draft.id))) or 0

    # 客户端在线数
    online_clients = db.scalar(
        select(func.count(models.Client.id)).where(models.Client.is_online == True)  # noqa: E712
    ) or 0

    # 磁盘信息
    import shutil
    disk_usage = shutil.disk_usage(str(DATA_DIR))

    # 最近 10 条完成任务
    recent_tasks = db.scalars(
        select(models.Task)
        .where(models.Task.status.in_(["done", "failed"]))
        .order_by(models.Task.finished_at.desc())
        .limit(10)
    ).all()

    return {
        "users": user_count,
        "tasks": {
            "total": total_tasks,
            "today": today_tasks,
            "week": week_tasks,
        },
        "assets": {
            "total_bytes": asset_total_bytes,
            "total_display": _fmt_mb(asset_total_bytes),
            "count": db.scalar(select(func.count(UploadedAsset.id))) or 0,
        },
        "drafts": {
            "total_bytes": draft_total_bytes,
            "total_display": _fmt_mb(draft_total_bytes),
            "count": draft_count,
        },
        "clients_online": online_clients,
        "disk": {
            "total": disk_usage.total,
            "used": disk_usage.used,
            "free": disk_usage.free,
            "free_display": _fmt_mb(disk_usage.free),
        },
        "recent_tasks": [
            {
                "id": t.id,
                "status": t.status,
                "workflow_name": t.workflow_name,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            }
            for t in recent_tasks
        ],
    }
