"""Audit dashboard — read + aggregate over the audit trail.

Scope follows the same rule as user/org management: a platform admin
(``org:manage``) sees every org's events; an org admin (``audit:read`` only) is
restricted to their own organization. Events are modelled as (actor, org,
entity, action), so the dashboard filters each axis independently.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import is_platform_admin, require_permission
from ..models import AuditLog, User
from ..schemas import AuditEntryOut, AuditSummaryOut

router = APIRouter(prefix="/admin/audit", tags=["audit"])


def _scoped(query, actor: User):
    """Platform admins see all orgs; everyone else only their own."""
    if not is_platform_admin(actor):
        return query.filter(AuditLog.organization_id == actor.organization_id)
    return query


@router.get("", response_model=list[AuditEntryOut])
def list_audit(
    entity_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("audit:read")),
):
    q = _scoped(db.query(AuditLog, User.email).outerjoin(User, AuditLog.user_id == User.id), actor)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if action:
        q = q.filter(AuditLog.action == action)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    if request_id:
        q = q.filter(AuditLog.request_id == request_id)
    if created_from:
        q = q.filter(AuditLog.created_at >= created_from)
    if created_to:
        q = q.filter(AuditLog.created_at <= created_to)

    rows = q.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset).all()
    return [
        AuditEntryOut(
            id=log.id, user_id=log.user_id, actor_email=email, organization_id=log.organization_id,
            entity_type=log.entity_type, action=log.action, entity_id=log.entity_id,
            detail=log.detail, ip_address=log.ip_address, request_id=log.request_id,
            created_at=log.created_at,
        )
        for (log, email) in rows
    ]


@router.get("/summary", response_model=AuditSummaryOut)
def audit_summary(
    window_days: int = Query(default=7, ge=1, le=365),
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("audit:read")),
):
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    base = _scoped(db.query(AuditLog).filter(AuditLog.created_at >= since), actor)

    def _grouped(column) -> dict[str, int]:
        rows = (
            _scoped(
                db.query(column, func.count(AuditLog.id)).filter(AuditLog.created_at >= since), actor
            )
            .group_by(column)
            .all()
        )
        return {str(k): int(n) for k, n in rows if k is not None}

    return AuditSummaryOut(
        window_days=window_days,
        total=base.count(),
        by_action=_grouped(AuditLog.action),
        by_entity=_grouped(AuditLog.entity_type),
        active_users=(
            _scoped(
                db.query(func.count(func.distinct(AuditLog.user_id))).filter(AuditLog.created_at >= since),
                actor,
            ).scalar() or 0
        ),
        recent_failures=(
            _scoped(
                db.query(func.count(AuditLog.id)).filter(
                    AuditLog.created_at >= since, AuditLog.action == "login_failed"
                ),
                actor,
            ).scalar() or 0
        ),
    )
