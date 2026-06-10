"""草稿云端存储 API + 分享链接。

API 列表（全部需 JWT 登录，除 `/share/*` 公开）：
- POST   /api/drafts/upload         上传草稿 .zip（multipart，含 task_id/filename）
- GET    /api/drafts                列表（搜索/筛选/分页）
- GET    /api/drafts/{id}/download  流式下载（计数 +1）
- DELETE /api/drafts/{id}           硬删（DB + 磁盘）
- POST   /api/drafts/{id}/share     生成分享 token（默认 7 天过期）
- GET    /api/drafts/quota          查询当前用户 quota 使用情况

公开：
- GET    /share/{token}             分享页（HTML，点确认下载）
- GET    /share/{token}/download    真正下载（?confirm=1 时立即触发）

Quota 规则：
- 默认 5GB/人（环境变量 CAPCUT_DRAFT_QUOTA_MB）
- 用户可在 `users.quota_mb` 单独覆盖；0 = 不限
- 上传前 `已用 + 本次文件大小 > quota` → 413，提示"请删除历史草稿"
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth as auth_mod
from .db_models import Draft, DraftShare

log = logging.getLogger(__name__)

router = APIRouter(tags=["drafts"])

# -------- 配置 --------

DATA_DIR = auth_mod.DATA_DIR
DRAFTS_DIR = Path(os.environ.get("CAPCUT_DRAFTS_DIR", DATA_DIR / "drafts"))
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

# 单人 quota 默认值（MB）— 用户表里有 quota_mb 字段，NULL 时用这个
DEFAULT_QUOTA_MB = int(os.environ.get("CAPCUT_DRAFT_QUOTA_MB", "5120"))  # 5GB

# 分享链接默认有效期
SHARE_TTL_DAYS = int(os.environ.get("CAPCUT_DRAFT_SHARE_TTL_DAYS", "7"))

# 单个 .zip 上限（防止 client 端 OOM 炸服务端），默认 2GB
MAX_DRAFT_BYTES = int(os.environ.get("CAPCUT_DRAFT_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))


# -------- 工具函数 --------

def _safe_filename(name: str) -> str:
    """防 path traversal：只保留 basename + 把危险字符替换成下划线。"""
    name = os.path.basename(name or "draft.zip")
    if not name.lower().endswith(".zip"):
        name = name + ".zip"
    # 防 Windows 保留字符 + 控制字符
    bad = '<>:"/\\|?*\0'
    return "".join("_" if c in bad else c for c in name)[:200] or "draft.zip"


def _draft_zip_path(owner_id: int, filename: str) -> Path:
    """每个用户独立子目录：data/drafts/{owner_id}/filename.zip"""
    owner_dir = DRAFTS_DIR / str(owner_id)
    owner_dir.mkdir(parents=True, exist_ok=True)
    return owner_dir / filename


def _user_used_bytes(db: Session, owner_id: int) -> int:
    """当前用户已用配额（字节）。"""
    total = db.scalar(
        select(func.coalesce(func.sum(Draft.size), 0)).where(Draft.owner_id == owner_id)
    )
    return int(total or 0)


def _user_quota_bytes(db: Session, user: auth_mod.User) -> int:
    """当前用户 quota（字节）。0 = 不限。"""
    if user.quota_mb is None:
        return DEFAULT_QUOTA_MB * 1024 * 1024
    return user.quota_mb * 1024 * 1024


# -------- API：上传 --------

@router.post("/api/drafts/upload")
async def upload_draft(
    file: Annotated[UploadFile, File(...)],
    task_id: Annotated[Optional[int], Form()] = None,
    task_name: Annotated[Optional[str], Form()] = None,
    workflow_name: Annotated[Optional[str], Form()] = None,
    note: Annotated[Optional[str], Form()] = None,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    """客户端 worker 在任务完成后调用，把 .zip 上传到云端。

    - 检查 quota → 超限 413
    - 写入 `data/drafts/{owner_id}/draft_{ts}_{task}.zip`
    - DB 记录 size/download_count
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "只支持 .zip 格式草稿")

    # 流式接收 + 限额检查
    safe_name = _safe_filename(file.filename)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stored_filename = f"draft_{ts}_{Path(safe_name).stem}.zip"
    dest = _draft_zip_path(user.id, stored_filename)

    quota_bytes = _user_quota_bytes(db, user)
    used = _user_used_bytes(db, user.id)

    # 提前检查：上传前 Content-Length 已知的情况下直接拒，省得写半截再删
    if file.size is not None and file.size > 0:
        if file.size > MAX_DRAFT_BYTES:
            raise HTTPException(
                413,
                f"草稿超过单文件上限 {_fmt_mb(MAX_DRAFT_BYTES)}，拒绝接收。",
            )
        if quota_bytes > 0 and (used + file.size) > quota_bytes:
            raise HTTPException(
                413,
                f"草稿云端存储超限：已用 {_fmt_mb(used)} / quota "
                f"{_fmt_mb(quota_bytes)}，本次文件 {_fmt_mb(file.size)}。"
                f"请到 Web 后台「草稿管理」删除历史草稿腾出空间。",
            )

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
                # 二次兜底：流式过程中如果 quota 翻车了（比如有人同时上传）
                if quota_bytes > 0 and (used + bytes_written) > quota_bytes:
                    out.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"草稿云端存储超限：已用 {_fmt_mb(used)} / quota "
                        f"{_fmt_mb(quota_bytes)}，本次文件 {_fmt_mb(bytes_written)}。"
                        f"请到 Web 后台「草稿管理」删除历史草稿腾出空间。",
                    )
                if bytes_written > MAX_DRAFT_BYTES:
                    out.close()
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"草稿超过单文件上限 {_fmt_mb(MAX_DRAFT_BYTES)}，拒绝接收。",
                    )
                h.update(chunk)
                out.write(chunk)
            out.flush()
        sha256 = h.hexdigest()
    except HTTPException:
        raise
    except Exception as e:
        # 写盘失败 → 清掉半截
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        log.exception("[drafts] upload failed: %s", e)
        raise HTTPException(500, f"上传失败：{e!r}")

    # 写 DB
    d = Draft(
        task_id=task_id,
        owner_id=user.id,
        task_name=task_name,
        workflow_name=workflow_name,
        filename=stored_filename,
        storage_path=str(dest.relative_to(DATA_DIR)) if DATA_DIR in dest.parents else str(dest),
        size=bytes_written,
        sha256=sha256,
        note=note,
    )
    db.add(d)
    db.commit()
    db.refresh(d)

    log.info(
        "[drafts] owner=%s uploaded draft id=%s name=%s size=%s",
        user.username, d.id, stored_filename, bytes_written,
    )
    return {
        "ok": True,
        "draft": d.to_dict(),
        "quota": {
            "used_bytes": _user_used_bytes(db, user.id),
            "quota_bytes": quota_bytes,
        },
    }


# -------- API：列表 + 搜索/筛选 --------

@router.get("/api/drafts")
def list_drafts(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
    q: Optional[str] = Query(None, description="按任务名/文件名模糊搜"),
    uploader_id: Optional[int] = Query(None, description="按上传人筛选（admin 才能查别人）"),
    min_size: Optional[int] = Query(None, description="最小字节数"),
    max_size: Optional[int] = Query(None, description="最大字节数"),
    date_from: Optional[str] = Query(None, description="ISO 时间，>="),
    date_to: Optional[str] = Query(None, description="ISO 时间，<="),
    sort: str = Query("created_desc", description="created_desc/created_asc/size_desc/size_asc/name_asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict:
    """草稿列表。普通用户只能看自己的；admin 可看全部 + 可按上传人筛选。"""
    stmt = select(Draft)
    cnt = select(func.count(Draft.id))

    if not user.is_admin:
        stmt = stmt.where(Draft.owner_id == user.id)
        cnt = cnt.where(Draft.owner_id == user.id)
    elif uploader_id is not None:
        stmt = stmt.where(Draft.owner_id == uploader_id)
        cnt = cnt.where(Draft.owner_id == uploader_id)

    if q:
        like = f"%{q}%"
        cond = (Draft.filename.ilike(like)) | (Draft.task_name.ilike(like)) | (Draft.workflow_name.ilike(like))
        stmt = stmt.where(cond)
        cnt = cnt.where(cond)

    if min_size is not None:
        stmt = stmt.where(Draft.size >= min_size)
        cnt = cnt.where(Draft.size >= min_size)
    if max_size is not None:
        stmt = stmt.where(Draft.size <= max_size)
        cnt = cnt.where(Draft.size <= max_size)

    if date_from:
        try:
            df = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
            stmt = stmt.where(Draft.created_at >= df)
            cnt = cnt.where(Draft.created_at >= df)
        except ValueError:
            raise HTTPException(400, "date_from 不是合法 ISO 时间")
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
            stmt = stmt.where(Draft.created_at <= dt)
            cnt = cnt.where(Draft.created_at <= dt)
        except ValueError:
            raise HTTPException(400, "date_to 不是合法 ISO 时间")

    # 排序
    if sort == "created_desc":
        stmt = stmt.order_by(Draft.created_at.desc())
    elif sort == "created_asc":
        stmt = stmt.order_by(Draft.created_at.asc())
    elif sort == "size_desc":
        stmt = stmt.order_by(Draft.size.desc())
    elif sort == "size_asc":
        stmt = stmt.order_by(Draft.size.asc())
    elif sort == "name_asc":
        stmt = stmt.order_by(Draft.filename.asc())
    else:
        raise HTTPException(400, f"未知 sort: {sort}")

    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)
    items = db.scalars(stmt).all()
    total = db.scalar(cnt) or 0

    return {
        "items": [d.to_dict() for d in items],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# -------- API：quota 查询 --------

@router.get("/api/drafts/quota")
def get_quota(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    used = _user_used_bytes(db, user.id)
    quota = _user_quota_bytes(db, user)
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "used_mb": round(used / 1024 / 1024, 2),
        "quota_mb": round(quota / 1024 / 1024, 2) if quota else 0,
        "unlimited": quota == 0,
        "draft_count": db.scalar(
            select(func.count(Draft.id)).where(Draft.owner_id == user.id)
        ) or 0,
    }


# -------- API：下载（带计数） --------

@router.get("/api/drafts/{draft_id}/download")
def download_draft(
    draft_id: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
):
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404, "草稿不存在")
    # 普通用户只能下自己的；admin 可下任何
    if not user.is_admin and d.owner_id != user.id:
        raise HTTPException(403, "只能下载自己的草稿")

    path = _resolve_storage_path(d)
    if not path.is_file():
        raise HTTPException(410, f"草稿文件已丢失：{d.filename}")

    d.download_count = (d.download_count or 0) + 1
    d.last_downloaded_at = datetime.now(timezone.utc)
    db.commit()

    return FileResponse(
        path=str(path),
        filename=d.filename,
        media_type="application/zip",
    )


# -------- API：硬删 --------

@router.delete("/api/drafts/{draft_id}")
def delete_draft(
    draft_id: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404, "草稿不存在")
    if not user.is_admin and d.owner_id != user.id:
        raise HTTPException(403, "只能删除自己的草稿")

    path = _resolve_storage_path(d)
    # 删文件（失败不抛，只记 log；DB 一定要删成功）
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:
        log.warning("[drafts] unlink 失败 %s: %s", path, e)

    # 删关联的分享
    db.query(DraftShare).filter(DraftShare.draft_id == d.id).delete()
    db.delete(d)
    db.commit()

    log.info("[drafts] owner=%s 删除草稿 id=%s name=%s", user.username, d.id, d.filename)
    return {
        "ok": True,
        "draft_id": d.id,
        "freed_bytes": d.size,
        "quota": {
            "used_bytes": _user_used_bytes(db, user.id),
            "quota_bytes": _user_quota_bytes(db, user),
        },
    }


# -------- API：生成分享链接 --------

@router.post("/api/drafts/{draft_id}/share")
def create_share(
    draft_id: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
    ttl_days: Optional[int] = Query(None, ge=1, le=90),
) -> dict:
    d = db.get(Draft, draft_id)
    if not d:
        raise HTTPException(404, "草稿不存在")
    if not user.is_admin and d.owner_id != user.id:
        raise HTTPException(403, "只能分享自己的草稿")

    ttl = ttl_days or SHARE_TTL_DAYS
    token = secrets.token_urlsafe(48)  # 64 base64 chars
    share = DraftShare(
        draft_id=d.id,
        created_by=user.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=ttl),
    )
    db.add(share)
    db.commit()
    db.refresh(share)

    return {
        "ok": True,
        "share": share.to_dict(),
        "draft": d.to_dict(),
    }


# -------- 公开：分享页面 + 下载 --------

_SHARE_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>草稿分享</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 520px; margin: 60px auto;
         padding: 0 20px; color: #222; }}
  .card {{ background: #f7f8fa; border-radius: 12px; padding: 24px;
          box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  h1 {{ font-size: 20px; margin-top: 0; }}
  .meta {{ color: #666; font-size: 14px; line-height: 1.7; }}
  .btn {{ display: inline-block; background: #2563eb; color: white;
         text-decoration: none; padding: 12px 24px; border-radius: 8px;
         margin-top: 16px; font-size: 15px; }}
  .btn:hover {{ background: #1d4ed8; }}
  .warn {{ color: #b91c1c; }}
  .ok   {{ color: #15803d; }}
</style>
</head>
<body>
<div class="card">
  <h1>📦 草稿分享</h1>
  <div class="meta">
    <div><b>文件名：</b>{filename}</div>
    <div><b>大小：</b>{size}</div>
    <div><b>分享人：</b>{owner}</div>
    <div><b>状态：</b><span class="{status_class}">{status}</span></div>
  </div>
  {body}
</div>
</body></html>
"""


@router.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str, db: Session = Depends(auth_mod.get_db)):
    s = db.scalar(select(DraftShare).where(DraftShare.token == token))
    if not s:
        return HTMLResponse(_SHARE_HTML.format(
            filename="-", size="-", owner="-",
            status="链接无效", status_class="warn",
            body="<p>此分享链接不存在或已删除。</p>",
        ), status_code=404)
    d = db.get(Draft, s.draft_id)
    owner = db.get(auth_mod.User, d.owner_id) if d else None

    if s.used:
        body = '<p class="warn">此链接已被使用过，<b>每个分享链接只支持下载一次</b>。<br>请向分享人索要新链接。</p>'
        status, status_class = "已使用", "warn"
    elif not s.is_active:
        body = '<p class="warn">此分享链接已过期。</p>'
        status, status_class = "已过期", "warn"
    else:
        body = f'<a class="btn" href="/share/{token}/download?confirm=1">⬇️ 确认下载</a>'
        status, status_class = f"有效（{SHARE_TTL_DAYS} 天内）", "ok"

    return HTMLResponse(_SHARE_HTML.format(
        filename=d.filename if d else "-",
        size=_fmt_mb(d.size) if d else "-",
        owner=owner.username if owner else "-",
        status=status,
        status_class=status_class,
        body=body,
    ))


@router.get("/share/{token}/download")
def share_download(
    token: str,
    request: Request,
    confirm: int = 0,
    db: Session = Depends(auth_mod.get_db),
):
    s = db.scalar(select(DraftShare).where(DraftShare.token == token))
    if not s:
        raise HTTPException(404, "分享链接无效")
    if not s.is_active:
        raise HTTPException(410, "分享链接已过期或已使用")
    if not confirm:
        # 不带 confirm=1 就当作预览请求，重定向到分享页
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/share/{token}", status_code=302)

    d = db.get(Draft, s.draft_id)
    if not d:
        raise HTTPException(404, "原草稿已删除")
    path = _resolve_storage_path(d)
    if not path.is_file():
        raise HTTPException(410, "草稿文件已丢失")

    # 标记已用
    s.used = True
    s.used_at = datetime.now(timezone.utc)
    s.used_ip = request.client.host if request.client else None
    db.commit()

    return FileResponse(
        path=str(path),
        filename=d.filename,
        media_type="application/zip",
    )


# -------- 工具 --------

def _resolve_storage_path(d: Draft) -> Path:
    """把 DB 里的 storage_path 还原成绝对路径。

    Windows 上 8.3 短路径（如 `C:\\Users\\ADMINI~1\\...`）和长路径（`C:\\Users\\Administrator\\...`）
    在 `Path.relative_to` 比较时会报"越界"，所以这里用 `os.path.normcase` 字符串前缀判断。
    """
    sp = d.storage_path
    p = Path(sp)
    if not p.is_absolute():
        p = (DATA_DIR / sp).resolve()
    # 安全检查：必须在 DRAFTS_DIR 子树下（用 normcase 避免 Windows 短路径差异）
    drafts_norm = os.path.normcase(str(DRAFTS_DIR))
    p_norm = os.path.normcase(str(p))
    if not p_norm.startswith(drafts_norm + os.sep) and p_norm != drafts_norm:
        log.error("[drafts] storage_path 越界: %s", p)
        # 强行纠正到 owner 子目录下，避免 path traversal
        p = DRAFTS_DIR / str(d.owner_id) / d.filename
    return p


def _fmt_mb(n: int) -> str:
    """字节数格式化成 MB/GB 字符串。"""
    if n is None:
        return "?"
    mb = n / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"
