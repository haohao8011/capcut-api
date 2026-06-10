"""数据库模型（任务系统）：Client / Asset / Task / TaskLog。

User 在 `auth.py`（鉴权强耦合）。`models.py` 保留原来的切点数据类（CutPoint/Segment/Word/Subtitle）。
这里只放任务系统相关的表 + 客户端鉴权依赖。

设计要点：
- 客户端用 opaque token（不是 JWT），存 hash、调 API 带明文
- 任务状态用 String + 应用层校验（跨 DB 兼容：SQLite / PG / MySQL 都能跑）
- 主视频 / B-roll 资产只存"路径引用"，文件始终在客户端本地
- **云端不存任何文件本体**：临时缓存（如果有）由定时任务清理（见 deploy/aliyun-server.sh）
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from . import auth as auth_mod  # 复用 Base / engine / SessionLocal

Base = auth_mod.Base  # 共享同一个 declarative base


# -------- 客户端（每台员工机一个） --------

class Client(Base):
    """客户端：装在员工机器上，跑实际 ASR + 草稿生成。

    - token 明文只在客户端本地（config/client.yaml），服务端只存 hash
    - owner_id = 该客户端"绑定"的员工；NULL = 公共池（任何登录用户可派任务）
    - is_online 由心跳维持（30s 内有心跳 = 在线）
    """
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    hostname: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255))
    version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    owner: Mapped[Optional["auth_mod.User"]] = relationship(lazy="joined")  # type: ignore[name-defined]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "hostname": self.hostname,
            "owner_id": self.owner_id,
            "owner_username": self.owner.username if self.owner else None,
            "version": self.version,
            "is_online": self.is_online,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# -------- 资产（素材元数据，路径引用） --------

class Asset(Base):
    """素材元数据：客户端扫盘后上报。**只存路径引用，不下载文件**。"""
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("client_id", "path", name="uq_client_path"),
        Index("ix_asset_owner_kind", "owner_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # "main" / "broll"
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    mtime: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "client_id": self.client_id,
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "duration": round(self.duration, 2),
            "mtime": self.mtime.isoformat() if self.mtime else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# -------- 任务 --------

TASK_STATUS = ("pending", "claimed", "running", "done", "failed", "canceled")


class Task(Base):
    """任务：从"用户提交"到"客户端跑完"的全流程。

    状态机：pending → claimed → running → done / failed；任何状态可被 cancel。
    """
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_task_owner_status", "owner_id", "status"),
        Index("ix_task_client_status", "client_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(64))
    workflow_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    main_asset_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    broll_asset_ids: Mapped[list[int]] = mapped_column(JSON, default=list)

    options: Mapped[dict] = mapped_column(JSON, default=dict)

    output_dir: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    result_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self, *, include_options: bool = False) -> dict:
        d = {
            "id": self.id,
            "owner_id": self.owner_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "main_asset_id": self.main_asset_id,
            "broll_asset_ids": self.broll_asset_ids or [],
            "output_dir": self.output_dir,
            "result_path": self.result_path,
            "error": self.error,
            "client_id": self.client_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if include_options:
            d["options"] = self.options or {}
        return d


class TaskLog(Base):
    """任务进度日志。"""
    __tablename__ = "task_logs"
    __table_args__ = (Index("ix_tasklog_task_ts", "task_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "ts": self.ts.isoformat() if self.ts else None,
            "level": self.level,
            "message": self.message,
        }


# -------- 工具函数 --------

def generate_client_token() -> tuple[str, str]:
    """生成 (明文 token, bcrypt hash)。明文给客户端保存，hash 存 DB。"""
    plain = "cap_" + secrets.token_urlsafe(32)
    return plain, auth_mod.hash_pwd(plain)


def generate_setup_code() -> str:
    """生成 6 位人类可读安装码（去掉 0/O/1/I/L），员工手敲不易错。"""
    import random
    # 大写字母去掉 0/O/1/I/L，数字去掉 0/1（人眼混淆）
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(alphabet, k=6))


def verify_client_token(plain: str, hashed: str) -> bool:
    return auth_mod.verify_pwd(plain, hashed)


# -------- 一次性安装码（管理员生成，员工兑换） --------

class SetupCode(Base):
    """客户端安装码。流程：

    1. 管理员 dashboard 点"生成安装码" → 创建 SetupCode（含 name hint / 过期）
    2. 员工双击 install-client.bat → 装好客户端 → 弹窗输 setup_code + 服务端 URL
    3. 客户端调 `/api/clients/wizard/redeem` 换 token → 写本地 credentials.json

    一个码只能用一次（redeemed_at 标记）。
    """
    __tablename__ = "setup_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    # 提示名（默认生成的 client.name，不强约束，redeem 时可改）
    name_hint: Mapped[str] = mapped_column(String(64))
    # 谁生成的（admin）
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 兑换后绑定的 client
    redeemed_client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )

    def to_dict(self) -> dict:
        # SQLite 存的 datetime 是 naive，与 aware 的 now() 比要先 normalize
        now_aware = datetime.now(timezone.utc)
        expires = self.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "code": self.code,
            "name_hint": self.name_hint,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "redeemed_at": self.redeemed_at.isoformat() if self.redeemed_at else None,
            "redeemed_client_id": self.redeemed_client_id,
            "is_active": self.redeemed_at is None and (
                self.expires_at is None or expires > now_aware
            ),
        }


def init_all_tables() -> None:
    """建所有表（users / clients / assets / tasks / task_logs / setup_codes）。"""
    Base.metadata.create_all(bind=auth_mod.engine)


# -------- FastAPI 依赖：客户端鉴权 --------

oauth2_client_scheme = OAuth2PasswordBearer(tokenUrl="/api/clients/login", auto_error=False)


async def get_current_client(
    token: Annotated[str | None, Depends(oauth2_client_scheme)],
    db: Annotated[Session, Depends(auth_mod.get_db)],
) -> "Client":
    """客户端鉴权：从 `Authorization: Bearer cap_xxx` 解析出 Client 对象。"""
    if not token or not token.startswith("cap_"):
        raise HTTPException(401, "需要客户端 token（cap_xxx 开头）")
    candidates = db.scalars(select(Client)).all()
    for c in candidates:
        if verify_client_token(token, c.token_hash):
            return c
    raise HTTPException(401, "客户端 token 无效或已删除")
