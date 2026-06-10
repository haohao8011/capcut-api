"""客户端（员工机器）管理路由。

调用方：
- 管理员/用户：注册客户端、列客户端、删客户端（带 user JWT）
- 客户端自身：心跳（带 client token cap_xxx）

两种注册方式：
A. 管理员调 `/api/clients/register` → 拿明文 token → 手工告诉员工
B. **推荐**：管理员生成 setup_code（`/api/clients/wizard/setup`）
   → 员工双击 install-client.bat → 自动调 `/api/clients/wizard/redeem` 换 token
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc as _desc, select

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


class SetupCodeReq(BaseModel):
    name_hint: str                       # 客户端的默认名（如"小马的剪辑机"）
    ttl_minutes: int = 60                # 多少分钟后过期
    owner_id: Optional[int] = None       # 绑定的员工，None = 当前用户（admin）


class RedeemSetupReq(BaseModel):
    code: str
    name: str                            # 最终客户端名（可改 hint）
    hostname: str
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


# -------- wizard：管理员生成 setup_code，员工兑换 token --------

@router.post("/wizard/setup",
             dependencies=[Depends(auth_mod.get_current_user)])
def wizard_setup(
    req: SetupCodeReq,
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """生成一个 6 位安装码。员工把这个码 + 服务端 URL 输到 install-client.bat 里。

    有效期默认 60 分钟，过期/已用都不可再兑。
    """
    if not req.name_hint.strip():
        raise HTTPException(400, "name_hint 不能为空")
    if req.ttl_minutes < 5 or req.ttl_minutes > 7 * 24 * 60:
        raise HTTPException(400, "ttl_minutes 必须在 5 .. 10080 之间")

    owner_id = req.owner_id if req.owner_id is not None else user.id
    if not user.is_admin and owner_id != user.id:
        raise HTTPException(403, "只能给自己生成安装码")

    # 冲突重试：极端概率，3 次就够
    code = models.generate_setup_code()
    for _ in range(3):
        if not db.scalar(select(models.SetupCode).where(models.SetupCode.code == code)):
            break
        code = models.generate_setup_code()
    else:
        raise HTTPException(500, "生成码冲突，重试")

    sc = models.SetupCode(
        code=code,
        name_hint=req.name_hint.strip(),
        created_by=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=req.ttl_minutes),
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)
    return {
        **sc.to_dict(),
        "owner_id": owner_id,
        "hint": f"把这个 6 位码告诉员工。员工双击 install-client.bat，"
                f"输 {req.server_url if hasattr(req, 'server_url') else '服务端URL'} 和这个码即可。",
    }


@router.get("/wizard/codes", dependencies=[Depends(auth_mod.get_current_user)])
def list_setup_codes(
    user: Annotated[auth_mod.User, Depends(auth_mod.get_current_user)],
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """列出所有安装码。admin 看全部，普通用户看自己的。"""
    q = select(models.SetupCode).order_by(_desc(models.SetupCode.created_at)).limit(100)
    if not user.is_admin:
        q = q.where(models.SetupCode.created_by == user.id)
    codes = db.scalars(q).all()
    return {"codes": [c.to_dict() for c in codes]}


@router.post("/wizard/redeem")  # 公共端点：不需要鉴权
def wizard_redeem(
    req: RedeemSetupReq,
    db: Annotated[auth_mod.Session, Depends(auth_mod.get_db)],
) -> dict:
    """员工用安装码换 token。**明文 token 仅此一次返回**，客户端要立刻存 credentials.json。"""
    if not req.code or len(req.code) < 4:
        raise HTTPException(400, "code 格式错")
    if not req.name.strip() or not req.hostname.strip():
        raise HTTPException(400, "name/hostname 不能为空")

    code = req.code.upper().strip()
    sc = db.scalar(select(models.SetupCode).where(models.SetupCode.code == code))
    if not sc:
        raise HTTPException(404, "安装码不存在")
    if sc.redeemed_at is not None:
        raise HTTPException(410, "安装码已被使用")
    # SQLite 存的是 naive datetime，比较前先 normalize
    expires = sc.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(410, "安装码已过期")

    # 决定 owner：找创建 setup_code 的那个用户
    owner_id = sc.created_by

    # 创建 client
    plain, hashed = models.generate_client_token()
    c = models.Client(
        name=req.name.strip(),
        hostname=req.hostname.strip()[:128],
        owner_id=owner_id,
        token_hash=hashed,
        version=req.version,
        is_online=False,
    )
    db.add(c)
    db.flush()  # 拿到 c.id

    sc.redeemed_at = datetime.now(timezone.utc)
    sc.redeemed_client_id = c.id
    db.commit()
    db.refresh(c)

    return {
        "client": c.to_dict(),
        "token": plain,  # ★ 明文只此一次
        "token_warning": "请把此 token 写入客户端本地 credentials.json，关闭后无法再查",
        "server_url_hint": "这就是你的服务端 URL，不需要再确认",
    }
