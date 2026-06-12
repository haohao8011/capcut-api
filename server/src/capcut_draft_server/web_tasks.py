"""任务路由（任务系统核心）。

调用方：
- 用户（user JWT）：
    POST   /api/tasks                       创建
    GET    /api/tasks                       列出（按 status / owner_id 过滤）
    GET    /api/tasks/{id}                  详情（含 logs）
    POST   /api/tasks/{id}/cancel           取消
    DELETE /api/tasks/{id}                  删
- 客户端（client token cap_xxx）：
    GET    /api/tasks?status=pending&client=me   拉"自己能领的"任务
    POST   /api/tasks/{id}/claim            领取
    POST   /api/tasks/{id}/start            开始执行
    POST   /api/tasks/{id}/progress         上报进度
    POST   /api/tasks/{id}/log              追加一条日志
    POST   /api/tasks/{id}/complete         完成（含 result_path）
    POST   /api/tasks/{id}/fail             失败
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth as auth_mod
from . import db_models as models

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# -------- Pydantic --------

class CreateTaskReq(BaseModel):
    workflow_id: str
    workflow_name: Optional[str] = None
    main_asset_id: Optional[int] = None       # 客户端扫盘的素材
    main_upload_id: Optional[int] = None      # Web 上传的素材（新增）
    broll_asset_ids: list[int] = []
    broll_upload_ids: list[int] = []          # Web 上传的 B-roll（新增）
    options: dict = {}
    output_dir: Optional[str] = None  # 客户端写入的本地目录（可由客户端默认）


class ProgressReq(BaseModel):
    progress: int  # 0-100
    message: Optional[str] = None
    level: str = "info"  # info/warn/error


class LogReq(BaseModel):
    level: str = "info"
    message: str


class CompleteReq(BaseModel):
    result_path: str
    output_dir: Optional[str] = None
    message: Optional[str] = None


class FailReq(BaseModel):
    error: str
    message: Optional[str] = None


# -------- 工具 --------

def _add_log(db, task_id: int, level: str, message: str) -> None:
    db.add(models.TaskLog(task_id=task_id, level=level, message=message))


def _visible_to_user(task: models.Task, user: auth_mod.User) -> bool:
    if user.is_admin:
        return True
    if task.owner_id == user.id:
        return True
    return False


def _task_for_user(db, task_id: int, user: auth_mod.User) -> models.Task:
    t = db.get(models.Task, task_id)
    if not t:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if not _visible_to_user(t, user):
        raise HTTPException(403, "无权访问此任务")
    return t


# -------- 用户侧 --------

@router.post("", status_code=201,
             dependencies=[Depends(auth_mod.get_current_user)])
def create_task(
    req: CreateTaskReq,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """用户创建任务：支持客户端扫盘素材和 Web 上传素材两种来源。"""
    has_main = req.main_asset_id is not None or req.main_upload_id is not None
    has_broll = bool(req.broll_asset_ids) or bool(req.broll_upload_ids)
    if not has_main and not has_broll:
        raise HTTPException(400, "至少要选 1 个主视频或 B-roll")
    t = models.Task(
        owner_id=user.id,
        workflow_id=req.workflow_id,
        workflow_name=req.workflow_name,
        main_asset_id=req.main_asset_id,
        main_upload_id=req.main_upload_id,
        broll_asset_ids=req.broll_asset_ids,
        broll_upload_ids=req.broll_upload_ids,
        options=req.options,
        output_dir=req.output_dir,
        status="pending",
        progress=0,
        message="等待客户端领取",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    _add_log(db, t.id, "info", f"用户 {user.username} 创建任务")
    db.commit()
    return t.to_dict()


@router.get("", dependencies=[Depends(auth_mod.get_current_user)])
def list_tasks(
    status: Optional[str] = None,
    client_id: Optional[int] = None,
    limit: int = 100,
    *,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    q = models.select(models.Task)
    if not user.is_admin:
        q = q.where(models.Task.owner_id == user.id)
    if status:
        q = q.where(models.Task.status == status)
    if client_id is not None:
        q = q.where(models.Task.client_id == client_id)
    q = q.order_by(models.Task.id.desc()).limit(min(limit, 500))
    items = db.scalars(q).all()
    return {"tasks": [t.to_dict() for t in items]}


@router.get("/{tid}", dependencies=[Depends(auth_mod.get_current_user)])
def get_task(
    tid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = _task_for_user(db, tid, user)
    logs = db.scalars(
        models.select(models.TaskLog)
        .where(models.TaskLog.task_id == tid)
        .order_by(models.TaskLog.id.asc())
    ).all()
    d = t.to_dict(include_options=True)
    d["logs"] = [lg.to_dict() for lg in logs]
    return d


@router.post("/{tid}/cancel",
             dependencies=[Depends(auth_mod.get_current_user)])
def cancel_task(
    tid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = _task_for_user(db, tid, user)
    if t.status in ("done", "failed", "canceled"):
        raise HTTPException(400, f"任务已 {t.status}，不能取消")
    t.status = "canceled"
    t.message = f"用户 {user.username} 取消"
    t.finished_at = datetime.now(timezone.utc)
    _add_log(db, t.id, "warn", t.message)
    db.commit()
    return t.to_dict()


@router.post("/{tid}/retry",
             dependencies=[Depends(auth_mod.get_current_user)])
def retry_task(
    tid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """失败 / 取消的任务可重置为 pending。"""
    t = _task_for_user(db, tid, user)
    if t.status not in ("failed", "canceled"):
        raise HTTPException(400, f"只能重试 {('failed','canceled')} 状态的任务")
    t.status = "pending"
    t.progress = 0
    t.error = None
    t.client_id = None
    t.claimed_at = None
    t.started_at = None
    t.finished_at = None
    t.message = "用户重置任务"
    _add_log(db, t.id, "info", t.message)
    db.commit()
    return t.to_dict()


@router.delete("/{tid}",
               dependencies=[Depends(auth_mod.get_current_user)])
def delete_task(
    tid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = _task_for_user(db, tid, user)
    db.delete(t)
    db.commit()
    return {"deleted": tid}


# -------- 客户端侧（client token） --------

@router.get("/queue/pending", dependencies=[Depends(models.get_current_client)])
def queue_pending(
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """客户端轮询"我能领的"任务。

    返回的每条 task 都会附：
    - main_asset.path / broll_assets[].path（客户端扫盘素材，本地路径）
    - main_upload / broll_uploads（Web 上传素材，需从服务器下载）
    """
    from sqlalchemy import or_
    q = models.select(models.Task).where(
        models.Task.status == "pending",
        or_(
            models.Task.owner_id == client.owner_id,  # 员工的客户端跑该员工的任务
            models.Task.owner_id == 0,  # 公共池（暂未使用）
        ),
    ).order_by(models.Task.id.asc()).limit(5)
    items = db.scalars(q).all()

    out = []
    for t in items:
        d = t.to_dict(include_options=True)
        # 拼 main_asset 路径引用（客户端扫盘）
        d["main_asset"] = None
        if t.main_asset_id:
            a = db.get(models.Asset, t.main_asset_id)
            if a:
                d["main_asset"] = {"id": a.id, "path": a.path, "name": a.name}
        # 拼 broll_assets 路径引用（客户端扫盘）
        brolls = []
        for aid in (t.broll_asset_ids or []):
            a = db.get(models.Asset, aid)
            if a:
                brolls.append({"id": a.id, "path": a.path, "name": a.name})
        d["broll_assets"] = brolls

        # 拼 main_upload（Web 上传素材）
        d["main_upload"] = None
        if t.main_upload_id:
            ua = db.get(models.UploadedAsset, t.main_upload_id)
            if ua:
                d["main_upload"] = {
                    "id": ua.id, "filename": ua.filename,
                    "size": ua.size, "kind": ua.kind,
                }
        # 拼 broll_uploads（Web 上传 B-roll）
        broll_uploads = []
        for uid in (t.broll_upload_ids or []):
            ua = db.get(models.UploadedAsset, uid)
            if ua:
                broll_uploads.append({
                    "id": ua.id, "filename": ua.filename,
                    "size": ua.size, "kind": ua.kind,
                })
        d["broll_uploads"] = broll_uploads

        out.append(d)
    return {"tasks": out}


@router.post("/{tid}/claim", dependencies=[Depends(models.get_current_client)])
def claim_task(
    tid: int,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t:
        raise HTTPException(404, f"任务不存在: {tid}")
    if t.status != "pending":
        raise HTTPException(409, f"任务已被领取 / 已开始 (status={t.status})")
    if t.owner_id != client.owner_id and not client.owner_id:
        # 公共池客户端不能领带 owner 的任务
        raise HTTPException(403, "该任务属于其他员工，公共池客户端不能领")
    t.status = "claimed"
    t.client_id = client.id
    t.claimed_at = datetime.now(timezone.utc)
    t.message = f"客户端 #{client.id} ({client.name}) 领取"
    _add_log(db, t.id, "info", t.message)
    db.commit()
    return t.to_dict(include_options=True)


@router.post("/{tid}/start", dependencies=[Depends(models.get_current_client)])
def start_task(
    tid: int,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t or t.client_id != client.id:
        raise HTTPException(404, "任务不存在或不属于你")
    if t.status != "claimed":
        raise HTTPException(400, f"任务未领取 (status={t.status})")
    t.status = "running"
    t.started_at = datetime.now(timezone.utc)
    t.message = "开始执行"
    _add_log(db, t.id, "info", t.message)
    db.commit()
    return t.to_dict()


@router.post("/{tid}/progress", dependencies=[Depends(models.get_current_client)])
def report_progress(
    tid: int,
    req: ProgressReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t or t.client_id != client.id:
        raise HTTPException(404, "任务不存在或不属于你")
    if t.status not in ("claimed", "running"):
        raise HTTPException(400, f"任务不在执行中 (status={t.status})")
    if t.status == "claimed":
        t.status = "running"
        t.started_at = datetime.now(timezone.utc)
    t.progress = max(0, min(100, req.progress))
    if req.message:
        t.message = req.message
    _add_log(db, t.id, req.level, req.message or f"进度 {t.progress}%")
    db.commit()
    return {"ok": True, "progress": t.progress, "status": t.status}


@router.post("/{tid}/log", dependencies=[Depends(models.get_current_client)])
def append_log(
    tid: int,
    req: LogReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t or t.client_id != client.id:
        raise HTTPException(404, "任务不存在或不属于你")
    _add_log(db, t.id, req.level, req.message)
    db.commit()
    return {"ok": True}


@router.post("/{tid}/complete", dependencies=[Depends(models.get_current_client)])
def complete_task(
    tid: int,
    req: CompleteReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t or t.client_id != client.id:
        raise HTTPException(404, "任务不存在或不属于你")
    t.status = "done"
    t.progress = 100
    t.finished_at = datetime.now(timezone.utc)
    t.result_path = req.result_path
    if req.output_dir:
        t.output_dir = req.output_dir
    t.message = req.message or f"完成 → {req.result_path}"
    _add_log(db, t.id, "info", t.message)
    db.commit()
    return t.to_dict()


@router.post("/{tid}/fail", dependencies=[Depends(models.get_current_client)])
def fail_task(
    tid: int,
    req: FailReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    t = db.get(models.Task, tid)
    if not t or t.client_id != client.id:
        raise HTTPException(404, "任务不存在或不属于你")
    t.status = "failed"
    t.finished_at = datetime.now(timezone.utc)
    t.error = req.error[:2000] if req.error else None
    t.message = req.message or f"失败: {req.error[:80]}"
    _add_log(db, t.id, "error", req.error or req.message or "失败")
    db.commit()
    return t.to_dict()
