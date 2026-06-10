"""客户端（员工机器）管理路由。

调用方：
- 管理员/用户：注册客户端、列客户端、删客户端（带 user JWT）
- 客户端自身：心跳（带 client token cap_xxx）

注册流程：
  1. 管理员用 user JWT 调 POST /api/clients/register，给出"昵称 + 绑定员工"
  2. 服务端生成 cap_xxx 明文 + bcrypt hash 存 DB，**明文只此一次返回**
  3. 管理员把明文告诉员工，员工写到本地 config/client.yaml
  4. 客户端启动时用 cap_xxx 调 heartbeat，后续轮询 /api/tasks 时也用它
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth as auth_mod
from . import db_models as models

router = APIRouter(prefix="/api/clients", tags=["clients"])


# -------- Pydantic 模型 --------

class RegisterClientReq(BaseModel):
    name: str
    hostname: str
    owner_id: Optional[int] = None  # 绑定的员工；None = 公共池
    version: Optional[str] = None


class HeartbeatReq(BaseModel):
    is_online: bool = True
    version: Optional[str] = None


# -------- 用户侧（user JWT） --------

@router.get("", dependencies=[Depends(auth_mod.get_current_user)])
def list_clients(
    owner_id: Optional[int] = None,
    only_online: bool = False,
    *,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """列出所有客户端（可按 owner_id 过滤）。"""
    q = models.select(models.Client)
    if owner_id is not None:
        q = q.where(models.Client.owner_id == owner_id)
    if only_online:
        q = q.where(models.Client.is_online == True)  # noqa: E712
    q = q.order_by(models.Client.id.desc())
    clients = db.scalars(q).all()
    return {"clients": [c.to_dict() for c in clients]}


@router.post("/register", status_code=201,
             dependencies=[Depends(auth_mod.get_current_user)])
def register_client(
    req: RegisterClientReq,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """注册新客户端。**明文 token 仅此一次返回**，管理员需保存并告知员工。

    owner_id 默认 = 当前用户（即"自己用的客户端"）。
    非 admin 想给别人注册会被拒。
    """
    if not req.name.strip():
        raise HTTPException(400, "name 不能为空")
    # 决定 owner_id：req.owner_id > user.id
    owner_id = req.owner_id if req.owner_id is not None else user.id
    if not user.is_admin and owner_id != user.id:
        raise HTTPException(403, "只能给自己注册客户端")

    plain, hashed = models.generate_client_token()
    c = models.Client(
        name=req.name.strip(),
        hostname=req.hostname.strip() or "unknown",
        owner_id=owner_id,
        token_hash=hashed,
        version=req.version,
        is_online=False,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {
        **c.to_dict(),
        "token": plain,  # ★ 明文只此一次返回，客户端要立刻保存
        "token_warning": "请把此 token 写入 config/client.yaml，刷新后无法再查",
    }


@router.get("/{cid}", dependencies=[Depends(auth_mod.get_current_user)])
def get_client(
    cid: int,
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    c = db.get(models.Client, cid)
    if not c:
        raise HTTPException(404, f"客户端不存在: {cid}")
    return c.to_dict()


@router.delete("/{cid}",
               dependencies=[Depends(auth_mod.get_current_user)])
def delete_client(
    cid: int,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    c = db.get(models.Client, cid)
    if not c:
        raise HTTPException(404, f"客户端不存在: {cid}")
    if c.owner_id and c.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "只能删自己的客户端或管理员删任意")
    db.delete(c)
    db.commit()
    return {"deleted": cid}


@router.post("/{cid}/rotate-token",
             dependencies=[Depends(auth_mod.require_admin)])
def rotate_client_token(
    cid: int,
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """管理员：重置客户端 token（旧 token 立即失效）。"""
    c = db.get(models.Client, cid)
    if not c:
        raise HTTPException(404, f"客户端不存在: {cid}")
    plain, hashed = models.generate_client_token()
    c.token_hash = hashed
    c.is_online = False
    db.commit()
    return {"id": cid, "token": plain, "warning": "旧 token 已失效"}


# -------- 客户端侧（client token cap_xxx） --------

@router.post("/heartbeat")
def heartbeat(
    req: HeartbeatReq,
    client: Annotated[models.Client, Depends(models.get_current_client)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """客户端心跳（每 20-30s 调一次）。"""
    client.is_online = req.is_online
    client.last_seen_at = datetime.now(timezone.utc)
    if req.version:
        client.version = req.version
    db.commit()
    return {"ok": True, "id": client.id, "server_time": datetime.now(timezone.utc).isoformat()}
