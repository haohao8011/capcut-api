"""Admin 后台路由：素材审核 + 系统统计。

- GET    /api/admin/review               全部上传素材（含审核状态筛选）
- POST   /api/admin/review/{id}/flag     标记 flagged
- POST   /api/admin/review/{id}/approve  标记 approved
- POST   /api/admin/review/{id}/reject   标记 rejected
- DELETE /api/admin/review/{id}          管理员硬删
- GET    /api/admin/stats                系统统计概览

所有端点需要 admin JWT。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth as auth_mod
from . import db_models as models
from .db_models import UploadedAsset
from .web_uploads import DATA_DIR, resolve_upload_path

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _fmt_mb(n: int) -> str:
    """字节数格式化成 MB/GB 字符串（admin 统计展示用）。"""
    if n is None:
        return "?"
    mb = n / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"


# -------- 素材审核 --------

@router.get("/review", dependencies=[Depends(auth_mod.require_admin)])
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


@router.post("/review/{aid}/flag", dependencies=[Depends(auth_mod.require_admin)])
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


@router.post("/review/{aid}/approve", dependencies=[Depends(auth_mod.require_admin)])
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


@router.post("/review/{aid}/reject", dependencies=[Depends(auth_mod.require_admin)])
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


@router.delete("/review/{aid}", dependencies=[Depends(auth_mod.require_admin)])
def admin_delete_asset(
    aid: int,
    user: auth_mod.User = Depends(auth_mod.require_admin),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    """管理员硬删：DB + 磁盘文件 + 关联分享。"""
    ua = db.get(UploadedAsset, aid)
    if not ua:
        raise HTTPException(404, "素材不存在")

    path = resolve_upload_path(ua)
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:
        log.warning("[admin] unlink 失败 %s: %s", path, e)

    db.delete(ua)
    db.commit()
    log.info("[admin] %s 删除素材 id=%s owner=%s", user.username, ua.id, ua.owner_id)
    return {"ok": True, "asset_id": ua.id, "freed_bytes": ua.size}


# -------- 系统统计 --------

@router.get("/stats", dependencies=[Depends(auth_mod.require_admin)])
def admin_stats(db: Session = Depends(auth_mod.get_db)) -> dict:
    """系统统计概览：用户数/任务数/素材占用/草稿占用/磁盘剩余。"""
    import shutil

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
