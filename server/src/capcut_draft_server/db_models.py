"""数据库模型（任务系统 + 草稿云端存储 + 素材上传 + 审计日志）：Client / Asset / UploadedAsset / Task / TaskLog / Draft / DraftShare / SetupCode / AuditLog。

User 在 `auth.py`（鉴权强耦合）。`models.py` 保留原来的切点数据类（CutPoint/Segment/Word/Subtitle）。
这里只放任务系统相关的表 + 客户端鉴权依赖 + 草稿云端存储 + 素材上传 + 审计日志。

设计要点：
- 客户端用 opaque token（不是 JWT），存 hash、调 API 带明文
- 任务状态用 String + 应用层校验（跨 DB 兼容：SQLite / PG / MySQL 都能跑）
- Asset 表：客户端扫盘上报，只存路径引用，文件始终在客户端本地
- UploadedAsset 表：用户通过 Web 前端直传到服务器，文件存在 uploads/{user_id}/
- Task 同时支持两种素材来源（main_asset_id → Asset, main_upload_id → UploadedAsset）
- **草稿 .zip 存在服务端**（data/drafts/{owner_id}/），员工可下载/删除/分享
  - 素材配额默认 3GB/人（CAPCUT_ASSET_QUOTA_MB），草稿配额默认 2GB/人（CAPCUT_DRAFT_QUOTA_MB）
  - 超限上传会被拒绝（不自动删，让用户自己删历史）
  - 草稿永久保留，cleanup_loop 不动草稿表
- AuditLog 表：合规审计（企业级要求 6 个月保留）。记录敏感操作（登录、改密、增删用户/客户端/草稿/素材等）
  - 默认保留 6 个月（CAPCUT_AUDIT_RETENTION_DAYS）；cleanup_loop 定期清旧
"""
from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

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
            "source": "client_scan",
        }


# -------- 文件夹 --------

class Folder(Base):
    """素材文件夹（支持多层嵌套，parent_id 指向父文件夹）。"""
    __tablename__ = "folders"
    __table_args__ = (
        Index("ix_folder_owner_parent", "owner_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# -------- 上传素材（Web 前端直传到服务器） --------

class UploadedAsset(Base):
    """用户上传的素材：通过 Web 前端直传视频文件到服务器。

    - 文件存储在 uploads/{user_id}/{uuid}_{filename}
    - review_status 默认 approved（自动通过），admin 事后可审
    - 与 Asset 表独立，互不干扰
    """
    __tablename__ = "uploaded_assets"
    __table_args__ = (
        Index("ix_upasset_owner_kind", "owner_id", "kind"),
        Index("ix_upasset_review", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    filename: Mapped[str] = mapped_column(String(255))  # 原始文件名
    storage_path: Mapped[str] = mapped_column(String(512))  # 相对路径
    kind: Mapped[str] = mapped_column(String(16))  # "main" / "broll"
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    mime_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), default="approved")
    reviewed_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "folder_id": self.folder_id,
            "filename": self.filename,
            "storage_path": self.storage_path,
            "kind": self.kind,
            "size": self.size,
            "duration": round(self.duration, 2),
            "mime_type": self.mime_type,
            "review_status": self.review_status,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "source": "web_upload",
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

    # Web 上传的素材（与上面的客户端扫盘素材并存，向后兼容）
    main_upload_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("uploaded_assets.id", ondelete="SET NULL"), nullable=True
    )
    broll_upload_ids: Mapped[list[int]] = mapped_column(JSON, default=list)

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
            "main_upload_id": self.main_upload_id,
            "broll_upload_ids": self.broll_upload_ids or [],
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


# -------- 草稿（云端存储 + 分享） --------

class Draft(Base):
    """云端草稿：客户端 worker 把 .draft 目录打包成 .zip 上传到这里。

    - storage_path 形如 `data/drafts/{owner_id}/draft_20260510_153022_task123.zip`
    - size 是上传时的字节数（用于 quota 计算）
    - 草稿**永久保留**，不由 cleanup_loop 清理（用户资产，非临时缓存）
    - 删除是硬删（DB + 磁盘文件一起）
    - download_count / last_downloaded_at 给"下载次数统计"用
    """
    __tablename__ = "drafts"
    __table_args__ = (
        Index("ix_draft_owner_created", "owner_id", "created_at"),
        Index("ix_draft_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 原始任务名（便于 UI 展示，task 删除时也不会丢）
    task_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 客户端上传时的 workflow 名
    workflow_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 磁盘上的文件名（draft_20260510_153022_task123.zip）
    filename: Mapped[str] = mapped_column(String(255))
    # 相对 data/ 的存储路径（owner_id 子目录下）
    storage_path: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    # SHA256 用于完整性校验（可选，调试时用）
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 上传时客户端给的备注
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    download_count: Mapped[int] = mapped_column(Integer, default=0)
    last_downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "task_name": self.task_name,
            "workflow_name": self.workflow_name,
            "filename": self.filename,
            "storage_path": self.storage_path,
            "size": self.size,
            "sha256": self.sha256,
            "note": self.note,
            "download_count": self.download_count,
            "last_downloaded_at": (
                self.last_downloaded_at.isoformat() if self.last_downloaded_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DraftShare(Base):
    """草稿分享链接：临时下载 token。

    流程：
    1. owner 调 `POST /api/drafts/{id}/share` → 创建 DraftShare（含 64 位 token）
    2. 服务端返回完整 URL：`https://server/share/{token}`
    3. 同事点链接 → 第一次访问时 `GET /share/{token}?confirm=1` 真正下载
       - 同时把 used_at 标记，used=True（防爬）
    4. expires_at 后失效（默认 7 天）
    """
    __tablename__ = "draft_shares"
    __table_args__ = (Index("ix_draftshare_draft", "draft_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), index=True
    )
    # 谁分享的（通常 = draft.owner_id）
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 用了之后记录 IP/UA（审计用）
    used_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "draft_id": self.draft_id,
            "created_by": self.created_by,
            "token": self.token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "used": self.used,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "used_ip": self.used_ip,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @property
    def is_active(self) -> bool:
        """未过期 + 未使用 = 有效。"""
        if self.used:
            return False
        exp = self.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp is None or exp > datetime.now(timezone.utc)


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


# -------- 审计日志（企业合规：登录/改密/增删敏感资源 都要留痕） --------

class AuditLog(Base):
    """审计日志：记录敏感操作。

    设计：
    - actor_id 允许 NULL（失败的登录、匿名操作也能记）
    - actor_type 区分 user / client / anonymous
    - action 格式: "domain.verb"，如 "user.login", "user.password_reset", "draft.delete"
    - resource / resource_id 标记被操作的对象（可选）
    - ip / user_agent 用于追踪来源
    - extra 存 JSON 灵活字段（diff、reason、old_value 等）
    - 默认保留 6 个月（cleanup_loop 跑 SQLAlchemy delete 清旧）
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor_ts", "actor_id", "ts"),
        Index("ix_audit_action_ts", "action", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(16))    # "user" / "client" / "anonymous"
    action: Mapped[str] = mapped_column(String(64), index=True)  # "user.login", "draft.delete" ...
    resource: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # "user"/"client"/"draft"/"asset"
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success")  # "success"/"failure"
    ts: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "action": self.action,
            "resource": self.resource,
            "resource_id": self.resource_id,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "extra": self.extra,
            "status": self.status,
            "ts": self.ts.isoformat() if self.ts else None,
        }


def init_all_tables() -> None:
    """建所有表（users / clients / assets / folders / uploaded_assets / tasks / task_logs / setup_codes / drafts / draft_shares）。"""
    Base.metadata.create_all(bind=auth_mod.engine)
    _migrate_add_columns()


def _migrate_add_columns() -> None:
    """轻量级 schema 迁移：只处理"加列"，安全可重入。

    SQLAlchemy 的 create_all 不会改已存在的表，所以新加的 nullable 列需要
    在这里手动 ALTER。SQLite/PG/MySQL 语法差异大 → 用 SQLAlchemy Inspector。
    """
    from sqlalchemy import inspect, text  # 局部 import 避免污染顶部
    insp = inspect(auth_mod.engine)
    with auth_mod.engine.begin() as conn:
        # users.quota_mb（草稿云端存储 quota，MB）
        if insp.has_table("users"):
            cols = {c["name"] for c in insp.get_columns("users")}
            if "quota_mb" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN quota_mb INTEGER"))
                log.info("[db] migration: added users.quota_mb")
            # users.asset_quota_mb（素材上传 quota，MB）
            if "asset_quota_mb" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN asset_quota_mb INTEGER"))
                log.info("[db] migration: added users.asset_quota_mb")

        # tasks.main_upload_id / tasks.broll_upload_ids
        if insp.has_table("tasks"):
            cols = {c["name"] for c in insp.get_columns("tasks")}
            if "main_upload_id" not in cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN main_upload_id INTEGER "
                    "REFERENCES uploaded_assets(id) ON DELETE SET NULL"
                ))
                log.info("[db] migration: added tasks.main_upload_id")
            if "broll_upload_ids" not in cols:
                conn.execute(text(
                    "ALTER TABLE tasks ADD COLUMN broll_upload_ids JSON DEFAULT '[]'"
                ))
                log.info("[db] migration: added tasks.broll_upload_ids")

        # uploaded_assets.folder_id
        if insp.has_table("uploaded_assets"):
            cols = {c["name"] for c in insp.get_columns("uploaded_assets")}
            if "folder_id" not in cols:
                conn.execute(text(
                    "ALTER TABLE uploaded_assets ADD COLUMN folder_id INTEGER "
                    "REFERENCES folders(id) ON DELETE SET NULL"
                ))
                log.info("[db] migration: added uploaded_assets.folder_id")


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
