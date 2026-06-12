"""文件夹 API：CRUD + 树形结构。

端点：
- POST   /api/folders              创建文件夹
- GET    /api/folders              列出当前用户的文件夹（平铺）
- GET    /api/folders/tree         文件夹树形结构
- PUT    /api/folders/{id}         重命名
- DELETE /api/folders/{id}         删除（级联删子文件夹，文件夹内素材 folder_id 置空）
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth as auth_mod
from .db_models import Folder, UploadedAsset

log = logging.getLogger(__name__)
router = APIRouter(tags=["folders"])


# -------- 数据模型 --------

class CreateFolderReq(BaseModel):
    name: str
    parent_id: Optional[int] = None


class RenameFolderReq(BaseModel):
    name: str


# -------- 工具函数 --------

def _build_tree(folders: list[Folder], parent_id: Optional[int] = None) -> list[dict]:
    """递归构建文件夹树。"""
    tree = []
    for f in folders:
        if f.parent_id == parent_id:
            node = f.to_dict()
            node["children"] = _build_tree(folders, f.id)
            tree.append(node)
    return tree


# -------- 端点 --------

@router.post("/api/folders")
def create_folder(
    req: CreateFolderReq,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "文件夹名不能为空")
    if len(name) > 200:
        raise HTTPException(400, "文件夹名太长")

    # 检查父文件夹
    if req.parent_id is not None:
        parent = db.get(Folder, req.parent_id)
        if not parent or parent.owner_id != user.id:
            raise HTTPException(404, "父文件夹不存在")

    # 同级重名检查
    q = select(Folder).where(
        Folder.owner_id == user.id,
        Folder.name == name,
        Folder.parent_id == req.parent_id,
    )
    if db.scalar(q):
        raise HTTPException(409, "同级下已有同名文件夹")

    folder = Folder(owner_id=user.id, name=name, parent_id=req.parent_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    log.info("[folders] user=%s created folder id=%s name=%s", user.username, folder.id, name)
    return {"ok": True, "folder": folder.to_dict()}


@router.get("/api/folders")
def list_folders(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    folders = db.scalars(
        select(Folder).where(Folder.owner_id == user.id).order_by(Folder.name)
    ).all()
    return {"folders": [f.to_dict() for f in folders]}


@router.get("/api/folders/tree")
def folder_tree(
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    folders = db.scalars(
        select(Folder).where(Folder.owner_id == user.id)
    ).all()
    return {"tree": _build_tree(folders)}


@router.get("/api/folders/{fid}")
def get_folder(
    fid: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    folder = db.get(Folder, fid)
    if not folder or folder.owner_id != user.id:
        raise HTTPException(404, "文件夹不存在")

    # 获取子文件夹
    children = db.scalars(
        select(Folder).where(Folder.parent_id == fid).order_by(Folder.name)
    ).all()

    # 获取文件夹内素材
    assets = db.scalars(
        select(UploadedAsset).where(UploadedAsset.folder_id == fid).order_by(UploadedAsset.id.desc())
    ).all()

    return {
        "folder": folder.to_dict(),
        "children": [c.to_dict() for c in children],
        "assets": [a.to_dict() for a in assets],
    }


@router.put("/api/folders/{fid}")
def rename_folder(
    fid: int,
    req: RenameFolderReq,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    folder = db.get(Folder, fid)
    if not folder or folder.owner_id != user.id:
        raise HTTPException(404, "文件夹不存在")

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "文件夹名不能为空")

    # 同级重名检查
    q = select(Folder).where(
        Folder.owner_id == user.id,
        Folder.name == name,
        Folder.parent_id == folder.parent_id,
        Folder.id != fid,
    )
    if db.scalar(q):
        raise HTTPException(409, "同级下已有同名文件夹")

    folder.name = name
    db.commit()
    return {"ok": True, "folder": folder.to_dict()}


@router.delete("/api/folders/{fid}")
def delete_folder(
    fid: int,
    user: auth_mod.User = Depends(auth_mod.get_current_user),
    db: Session = Depends(auth_mod.get_db),
) -> dict:
    folder = db.get(Folder, fid)
    if not folder or folder.owner_id != user.id:
        raise HTTPException(404, "文件夹不存在")

    # 递归收集所有子文件夹 ID
    all_ids = [fid]
    stack = [fid]
    while stack:
        pid = stack.pop()
        children = db.scalars(select(Folder.id).where(Folder.parent_id == pid)).all()
        for cid in children:
            all_ids.append(cid)
            stack.append(cid)

    # 文件夹内素材 folder_id 置空
    from sqlalchemy import update as _upd
    db.execute(
        _upd(UploadedAsset).where(UploadedAsset.folder_id.in_(all_ids)).values(folder_id=None)
    )

    # 删除所有文件夹
    for did in all_ids:
        f = db.get(Folder, did)
        if f:
            db.delete(f)

    db.commit()
    log.info("[folders] user=%s deleted folder id=%s (+ %d children)", user.username, fid, len(all_ids) - 1)
    return {"ok": True, "deleted": len(all_ids)}
