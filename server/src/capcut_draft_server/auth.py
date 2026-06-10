"""用户认证与授权。

- SQLite 存用户表（`data/capcut.db`）
- bcrypt 哈希密码
- JWT (HS256)：access 2h + refresh 30d
- FastAPI 依赖：get_current_user（任何已登录用户）/ require_admin（管理员）
- 启动时自动 seed 默认管理员 `xiaoma / niubi666`（已存在则跳过）

环境变量：
- `CAPCUT_JWT_SECRET`：JWT 签名密钥。**生产环境必须改！** 不设则用开发默认值（启动会打 warning）。
"""
from __future__ import annotations

import logging
import os
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import Boolean, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)

# -------- 路径 & DB --------

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "capcut.db"

# DB URL：默认 SQLite，可通过环境变量切到 PostgreSQL/MySQL 等
#  示例：export CAPCUT_DB_URL=postgresql+psycopg://user:pass@host:5432/capcut
DB_URL = os.environ.get("CAPCUT_DB_URL", f"sqlite:///{DB_PATH}")
_DB_KIND = "sqlite" if DB_URL.startswith("sqlite") else "other"

if _DB_KIND == "sqlite":
    # SQLite 需要 check_same_thread=False（FastAPI 多线程）
    engine = create_engine(
        DB_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )
else:
    # PG / MySQL 等：开连接池 + 心跳检测断连
    engine = create_engine(
        DB_URL,
        pool_pre_ping=True,    # 每次连接前 SELECT 1，断线自动重连
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,     # 30 分钟回收连接（避免 PG/MySQL 服务端超时）
        echo=False,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # 草稿云端存储 quota（MB）。NULL = 用环境变量默认值；0 = 不限
    # 由 db_models._migrate_add_columns() 在 init_all_tables 时补列
    quota_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


# -------- JWT 配置 --------

JWT_SECRET = os.environ.get("CAPCUT_JWT_SECRET", "")
_DEV_SECRET = "capcut-draft-dev-only-DO-NOT-USE-IN-PROD-please-set-CAPCUT_JWT_SECRET"
if not JWT_SECRET:
    JWT_SECRET = _DEV_SECRET
    warnings.warn(
        "[auth] CAPCUT_JWT_SECRET 未设置，正在使用开发默认密钥。"
        "生产部署前请 `export CAPCUT_JWT_SECRET=一段随机长字符串`。",
        stacklevel=2,
    )

JWT_ALG = "HS256"
ACCESS_TTL_SEC = 2 * 60 * 60       # 2h
REFRESH_TTL_SEC = 30 * 24 * 60 * 60  # 30d

# bcrypt 工作因子：12 是 2024 年的安全推荐（~100ms/次 on 现代 CPU）
_BCRYPT_ROUNDS = 12

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# -------- 工具函数 --------

def hash_pwd(p: str) -> str:
    """bcrypt 哈希密码（自动截断到 72 字节，符合 bcrypt 4.x 限制）。"""
    p_bytes = p.encode("utf-8")[:72]
    return bcrypt.hashpw(p_bytes, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_pwd(p: str, h: str) -> bool:
    """bcrypt 校验密码。"""
    if not h:
        return False
    try:
        return bcrypt.checkpw(p.encode("utf-8")[:72], h.encode("utf-8"))
    except Exception:
        return False


def make_token(uid: int, username: str, is_admin: bool, *, kind: str, ttl: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(uid),
        "uname": username,
        "adm": is_admin,
        "typ": kind,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])


def get_db():
    """FastAPI 依赖：每个请求一个 session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------- FastAPI 依赖 --------

async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录：缺少 token")
    try:
        p = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "token 无效")
    if p.get("typ") != "access":
        raise HTTPException(401, "需要 access token（请用 /api/auth/refresh 续期）")
    u = db.get(User, int(p["sub"]))
    if not u:
        raise HTTPException(401, "用户不存在或已被删除")
    return u


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


# -------- 业务函数 --------

def authenticate_user(db: Session, username: str, password: str) -> User | None:
    u = db.scalar(select(User).where(User.username == username))
    if not u:
        return None
    if not verify_pwd(password, u.password_hash):
        return None
    return u


def create_user(db: Session, username: str, password: str, *, email: str | None = None,
                is_admin: bool = False) -> User:
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(409, f"用户名已存在: {username}")
    if len(password) < 6:
        raise HTTPException(400, "密码太短（至少 6 位）")
    u = User(
        username=username,
        email=email,
        password_hash=hash_pwd(password),
        is_admin=is_admin,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def change_password(db: Session, user: User, old: str, new: str) -> None:
    if not verify_pwd(old, user.password_hash):
        raise HTTPException(400, "原密码错误")
    if len(new) < 6:
        raise HTTPException(400, "新密码太短（至少 6 位）")
    user.password_hash = hash_pwd(new)
    db.commit()


# -------- 启动时初始化 --------

def seed_admin(username: str = "xiaoma", password: str = "niubi666") -> bool:
    """如果默认管理员不存在则创建。返回是否新建。"""
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)):
            return False
        u = User(
            username=username,
            email=None,
            password_hash=hash_pwd(password),
            is_admin=True,
        )
        db.add(u)
        db.commit()
        log.warning(
            "[auth] 已 seed 默认管理员 %s（密码 %s）— 首次登录后请改密！",
            username, password,
        )
        return True
