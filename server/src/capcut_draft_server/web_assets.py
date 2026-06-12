"""资产（素材元数据）路由。

调用方：
- 客户端：用 cap_xxx token 上报本机扫到的素材（批量 upsert）
- 用户：用 user JWT 列自己的素材（选主视频 / B-roll 时用）
- 管理员 / owner：删除素材
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth as auth_mod
from . import db_models as models

router = APIRouter(prefix="/api/assets", tags=["assets"])


class AssetItem(BaseModel):
    path: str
    name: str
    kind: str  # "main" | "broll"
    size: int = 0
    duration: float = 0.0
    mtime: Optional[datetime] = None


class BatchUpsertReq(BaseModel):
    """客户端批量上报。"""
    items: list[AssetItem]


# -------- 客户端侧（client token） --------

@router.post("/batch", status_code=200)
def batch_upsert(
    req: BatchUpsertReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """客户端上报本机素材（upsert：同 client+path 覆盖更新）。

    - 服务端不下载文件，只存引用
    - owner_id 跟随 client.owner_id（NULL = 公共池，任何登录用户可见）
    """
    inserted = 0
    updated = 0
    for item in req.items:
        if item.kind not in ("main", "broll"):
            continue
        existing = db.scalar(
            models.select(models.Asset).where(
                models.Asset.client_id == client.id,
                models.Asset.path == item.path,
            )
        )
        if existing:
            existing.name = item.name
            existing.size = item.size
            existing.duration = item.duration
            existing.mtime = item.mtime
            existing.kind = item.kind
            updated += 1
        else:
            db.add(models.Asset(
                owner_id=client.owner_id,
                client_id=client.id,
                path=item.path,
                name=item.name,
                kind=item.kind,
                size=item.size,
                duration=item.duration,
                mtime=item.mtime,
            ))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}


# -------- 用户侧（user JWT） --------

@router.get("", dependencies=[Depends(auth_mod.get_current_user)])
def list_assets(
    kind: Optional[str] = None,
    client_id: Optional[int] = None,
    source: Optional[str] = None,
    *,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """列出当前用户可用的素材（客户端扫盘 + Web 上传合并）。

    每条 asset 带 source 字段："client_scan" 或 "web_upload"。
    """
    from sqlalchemy import or_, select as _sel

    result = []

    # 1. 客户端扫盘素材（Asset 表）
    if not source or source == "client_scan":
        q = models.select(models.Asset)
        if not user.is_admin:
            public_client_ids = _sel(models.Client.id).where(models.Client.owner_id.is_(None))
            q = q.where(or_(
                models.Asset.owner_id == user.id,
                models.Asset.client_id.in_(public_client_ids),
            ))
        if kind:
            q = q.where(models.Asset.kind == kind)
        if client_id is not None:
            q = q.where(models.Asset.client_id == client_id)
        q = q.order_by(models.Asset.id.desc())
        for a in db.scalars(q).all():
            result.append(a.to_dict())

    # 2. Web 上传素材（UploadedAsset 表，只返回 approved + 自己的）
    if not source or source == "web_upload":
        uq = _sel(models.UploadedAsset).where(
            models.UploadedAsset.owner_id == user.id,
            models.UploadedAsset.review_status == "approved",
        )
        if kind:
            uq = uq.where(models.UploadedAsset.kind == kind)
        uq = uq.order_by(models.UploadedAsset.id.desc())
        for ua in db.scalars(uq).all():
            result.append(ua.to_dict())

    return {"assets": result}


@router.get("/{aid}", dependencies=[Depends(auth_mod.get_current_user)])
def get_asset(
    aid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    a = db.get(models.Asset, aid)
    if not a:
        raise HTTPException(404, f"资产不存在: {aid}")
    if a.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "无权查看此资产")
    return a.to_dict()


@router.delete("/{aid}",
               dependencies=[Depends(auth_mod.get_current_user)])
def delete_asset(
    aid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    a = db.get(models.Asset, aid)
    if not a:
        raise HTTPException(404, f"资产不存在: {aid}")
    if a.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "只能删自己的资产")
    db.delete(a)
    db.commit()
    return {"deleted": aid}
