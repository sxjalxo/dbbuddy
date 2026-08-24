"""User administration.

Scope is keyed off ``org:manage``: a platform admin manages users in every org
and may grant any role; an org admin manages only their own org's members and may
grant only non-privileged roles (``analyst`` / ``user``) — they cannot mint
platform or org admins, nor move members between orgs.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import invalidate_revocation, is_platform_admin, require_permission, write_audit
from ..models import (
    ApiKey,
    AuditLog,
    Dashboard,
    DashboardItem,
    DatabaseConnection,
    ExecutionToken,
    InsightCache,
    JobRun,
    Notification,
    Organization,
    PublishedDashboard,
    PublishedReport,
    QueryHistory,
    Role,
    SavedChart,
    ScheduledJob,
    SchemaSnapshot,
    User,
)
from ..schemas import AdminUserCreate, AdminUserOut, AdminUserPatch
from ..security import hash_password

router = APIRouter(prefix="/admin/users", tags=["admin"])

# Roles an org admin (no org:manage) is allowed to grant. Platform admins may
# grant any role. This is the anti-privilege-escalation boundary.
ORG_ADMIN_GRANTABLE = {"analyst", "user"}


def _admin_user_out(user: User) -> AdminUserOut:
    return AdminUserOut(
        id=user.id, email=user.email, full_name=user.full_name, is_active=user.is_active,
        organization_id=user.organization_id, roles=user.role_names(), created_at=user.created_at,
    )


def _resolve_roles(db: Session, actor: User, names: list[str]) -> list[Role]:
    """Validate role names exist and that the actor is permitted to grant them."""
    roles: list[Role] = []
    for name in names:
        role = db.query(Role).filter_by(name=name).one_or_none()
        if role is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {name}")
        if not is_platform_admin(actor) and name not in ORG_ADMIN_GRANTABLE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You may not grant the '{name}' role.")
        roles.append(role)
    return roles


def _purge_user_owned_data(db: Session, user_id: str) -> None:
    """Remove every row a user owns, in FK-dependency order, then de-attribute their
    audit rows.

    Ownership is unwound *explicitly* rather than left to DB-level ``ondelete``,
    because that is not portable across the two engines this runs on: SQLite (tests)
    does not enforce foreign keys by default, so a cascade would silently orphan
    these rows, and on PostgreSQL (prod) ``published_reports.published_by`` is
    ``SET NULL`` on a NOT NULL column, so a cascade would raise. Deleting the rows
    here makes a hard delete behave identically on both.

    Audit rows are *kept* but their actor is nulled (mirroring the ``audit_logs``
    ``SET NULL`` design), so the compliance trail outlives the deleted account.
    """
    # Grandchildren first: job runs belong to the user's scheduled jobs.
    job_ids = [row[0] for row in db.query(ScheduledJob.id).filter(ScheduledJob.user_id == user_id).all()]
    if job_ids:
        db.query(JobRun).filter(JobRun.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(ScheduledJob).filter(ScheduledJob.user_id == user_id).delete(synchronize_session=False)

    # Publications reference the user's charts (chart_id) and the user (published_by);
    # remove the publications before the charts they point at. A user can only publish
    # their own charts, so filtering on published_by covers all of their reports.
    db.query(PublishedReport).filter(PublishedReport.published_by == user_id).delete(synchronize_session=False)

    # Dashboards sit between the user and their charts: a dashboard_item points at
    # a saved_chart, so both the publications and the items must go before the
    # charts do. A chart can only be pinned by its owner, so the user's own
    # dashboards hold every item that references their charts.
    dashboard_ids = [row[0] for row in db.query(Dashboard.id).filter(Dashboard.user_id == user_id).all()]
    db.query(PublishedDashboard).filter(PublishedDashboard.published_by == user_id).delete(synchronize_session=False)
    if dashboard_ids:
        db.query(PublishedDashboard).filter(
            PublishedDashboard.dashboard_id.in_(dashboard_ids)
        ).delete(synchronize_session=False)
        db.query(DashboardItem).filter(
            DashboardItem.dashboard_id.in_(dashboard_ids)
        ).delete(synchronize_session=False)
    db.query(Dashboard).filter(Dashboard.user_id == user_id).delete(synchronize_session=False)

    db.query(SavedChart).filter(SavedChart.user_id == user_id).delete(synchronize_session=False)

    # Cached insight bundles are user-owned; SQLite won't cascade them either.
    db.query(InsightCache).filter(InsightCache.user_id == user_id).delete(synchronize_session=False)

    db.query(ExecutionToken).filter(ExecutionToken.user_id == user_id).delete(synchronize_session=False)
    # Schema snapshots hang off the user's connections; remove them before the
    # connections (SQLite won't cascade — see the docstring).
    conn_ids = [row[0] for row in db.query(DatabaseConnection.id).filter(DatabaseConnection.user_id == user_id).all()]
    if conn_ids:
        db.query(SchemaSnapshot).filter(SchemaSnapshot.connection_id.in_(conn_ids)).delete(synchronize_session=False)
    db.query(DatabaseConnection).filter(DatabaseConnection.user_id == user_id).delete(synchronize_session=False)
    db.query(QueryHistory).filter(QueryHistory.user_id == user_id).delete(synchronize_session=False)
    db.query(ApiKey).filter(ApiKey.user_id == user_id).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)

    db.query(AuditLog).filter(AuditLog.user_id == user_id).update(
        {AuditLog.user_id: None}, synchronize_session=False
    )


def _target_in_scope_or_404(db: Session, user_id: str, actor: User) -> User:
    target = db.get(User, user_id)
    if target is None or (not is_platform_admin(actor) and target.organization_id != actor.organization_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    # An org admin cannot touch a platform admin who happens to share their org.
    if not is_platform_admin(actor) and "org:manage" in target.permission_names():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You may not modify this user.")
    return target


@router.get("", response_model=list[AdminUserOut])
def list_users(db: Session = Depends(get_db), actor: User = Depends(require_permission("user:manage"))):
    q = db.query(User)
    if not is_platform_admin(actor):
        q = q.filter(User.organization_id == actor.organization_id)
    return [_admin_user_out(u) for u in q.order_by(User.created_at).all()]


@router.post("", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    req: AdminUserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user:manage")),
):
    email = req.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    # Org admins always create within their own org; platform admins may target one.
    if is_platform_admin(actor):
        org_id = req.organization_id or actor.organization_id
        if db.get(Organization, org_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target organization not found.")
    else:
        org_id = actor.organization_id

    roles = _resolve_roles(db, actor, [req.role])
    user = User(
        email=email, password_hash=hash_password(req.password), full_name=req.full_name,
        organization_id=org_id,
    )
    user.roles = roles
    db.add(user)
    db.flush()
    write_audit(db, user_id=actor.id, action="create", entity_type="user", entity_id=user.id,
                organization_id=org_id, detail={"email": email, "role": req.role})
    db.commit()
    db.refresh(user)
    return _admin_user_out(user)


@router.patch("/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: str,
    req: AdminUserPatch,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user:manage")),
):
    target = _target_in_scope_or_404(db, user_id, actor)

    if req.roles is not None:
        target.roles = _resolve_roles(db, actor, req.roles)

    if req.is_active is not None:
        if target.id == actor.id and not req.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot deactivate your own account.")
        # Deactivating a user revokes their outstanding refresh tokens *and*, via
        # the ``tv`` claim, their current access token — so the cut-off is
        # immediate rather than "up to one access-token lifetime from now".
        if target.is_active and not req.is_active:
            target.token_version += 1
        target.is_active = req.is_active
        invalidate_revocation(target.id)

    if req.organization_id is not None:
        if not is_platform_admin(actor):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a platform admin can move users between orgs.")
        if db.get(Organization, req.organization_id) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target organization not found.")
        target.organization_id = req.organization_id

    write_audit(db, user_id=actor.id, action="update", entity_type="user", entity_id=target.id,
                organization_id=target.organization_id,
                detail={"roles": req.roles, "is_active": req.is_active, "organization_id": req.organization_id})
    db.commit()
    db.refresh(target)
    return _admin_user_out(target)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user:manage")),
):
    """Permanently delete a user and the resources they own (connections, charts,
    saved/published reports, query history, API keys, scheduled jobs, notifications).

    Irreversible — for a reversible suspension the active/inactive toggle
    (``PATCH``) remains. Scope matches the rest of this router: an org admin may
    only delete their own org's non-privileged members. Because an admin cannot
    delete their own account, at least one admin always survives — there is no way
    to delete the last one and lock the platform out.
    """
    target = _target_in_scope_or_404(db, user_id, actor)
    if target.id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")

    # Capture identifying detail before the rows go away, so the audit record of
    # the deletion is still meaningful.
    deleted_email = target.email
    deleted_roles = target.role_names()
    org_id = target.organization_id

    _purge_user_owned_data(db, target.id)
    write_audit(db, user_id=actor.id, action="delete", entity_type="user", entity_id=target.id,
                organization_id=org_id, detail={"email": deleted_email, "roles": deleted_roles})
    db.delete(target)  # also clears the user_roles association rows (ORM secondary)
    db.commit()
