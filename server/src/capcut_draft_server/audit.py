"""审计日志：合规记录敏感操作。

用法（被业务代码主动调用）：
    from .audit import log_audit
    log_audit(db, request, actor_id=user.id, actor_type="user",
              action="user.login", status="success",
              extra={"ip_user": req.username})

- 不抛异常：审计失败不能影响主业务
- 自动从 Request 提取 IP / User-Agent
- actor_id 允许 None（匿名 / 失败登录也能记）
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from . import db_models

log = logging.getLogger(__name__)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """提取 IP：优先取反向代理头（生产有 nginx），fallback 到 client.host。"""
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    real = request.headers.get("x-real-ip")
    if real:
        return real[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


def _user_agent(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    ua = request.headers.get("user-agent", "")
    return ua[:255] if ua else None


def log_audit(
    db: Session,
    *,
    request: Optional[Request] = None,
    actor_id: Optional[int] = None,
    actor_type: str = "user",
    action: str,
    resource: Optional[str] = None,
    resource_id: Optional[Any] = None,
    status: str = "success",
    extra: Optional[dict] = None,
) -> None:
    """记一条审计日志。**绝不抛异常**（审计失败不能让业务挂）。

    参数:
    - db: SQLAlchemy Session（用传入的，同事务一起 commit）
    - request: FastAPI Request（用来取 IP + UA）
    - actor_id: 操作者 user.id（None = 匿名/失败登录）
    - actor_type: "user" | "client" | "anonymous"
    - action: 动词，格式 "domain.verb"，如 "user.login" / "draft.delete"
    - resource / resource_id: 被操作对象（可选）
    - status: "success" | "failure"
    - extra: 任意 JSON 字段（reason / old_value / diff 等）
    """
    try:
        entry = db_models.AuditLog(
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource=resource,
            resource_id=str(resource_id) if resource_id is not None else None,
            ip=_client_ip(request),
            user_agent=_user_agent(request),
            extra=extra,
            status=status,
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        # 审计失败不能让业务挂掉；只记 warning
        log.warning("[audit] 写审计失败: action=%s err=%s", action, e)
        try:
            db.rollback()
        except Exception:
            pass


def cleanup_old_audit_logs(db: Session, retention_days: int) -> int:
    """清超过 retention_days 天的审计日志。返回删除行数。

    建议每 24h 跑一次（cleanup_loop 里挂上）。
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete as _del
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    n = db.execute(
        _del(db_models.AuditLog).where(db_models.AuditLog.ts < cutoff)
    ).rowcount
    db.commit()
    if n:
        log.info("[cleanup] 审计日志: 删 %d 条（%d 天前）", n, retention_days)
    return n
