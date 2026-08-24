"""Organization (tenant) management.

A platform admin (``org:manage``) acts across all orgs; an org admin (``user:manage``
without ``org:manage``) is scoped to their own organization. The default org is
protected: it can be renamed but never deleted.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import is_platform_admin, require_permission, write_audit
from ..models import Organization, User
from ..schemas import OrgIn, OrgOut, OrgPatch
from ..slug import unique_slug

router = APIRouter(prefix="/orgs", tags=["organizations"])


def _member_count(db: Session, org_id: str) -> int:
    return db.query(func.count(User.id)).filter(User.organization_id == org_id).scalar() or 0


def _org_out(db: Session, org: Organization) -> OrgOut:
    return OrgOut(
        id=org.id, name=org.name, slug=org.slug, is_default=org.is_default,
        member_count=_member_count(db, org.id), created_at=org.created_at,
    )


def _visible_or_404(db: Session, org_id: str, actor: User) -> Organization:
    org = db.get(Organization, org_id)
    # Org admins may only see/act on their own org; hide others as 404.
    if org is None or (not is_platform_admin(actor) and org.id != actor.organization_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return org


@router.get("", response_model=list[OrgOut])
def list_orgs(db: Session = Depends(get_db), actor: User = Depends(require_permission("user:manage"))):
    q = db.query(Organization)
    if not is_platform_admin(actor):
        q = q.filter(Organization.id == actor.organization_id)
    return [_org_out(db, o) for o in q.order_by(Organization.created_at).all()]


@router.post("", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
def create_org(
    req: OrgIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("org:manage")),
):
    org = Organization(name=req.name.strip(), slug=unique_slug(db, req.name))
    db.add(org)
    db.flush()
    write_audit(db, user_id=actor.id, action="create", entity_type="organization",
                entity_id=org.id, organization_id=org.id, detail={"name": org.name, "slug": org.slug})
    db.commit()
    db.refresh(org)
    return _org_out(db, org)


@router.patch("/{org_id}", response_model=OrgOut)
def rename_org(
    org_id: str,
    req: OrgPatch,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("user:manage")),
):
    org = _visible_or_404(db, org_id, actor)
    org.name = req.name.strip()
    write_audit(db, user_id=actor.id, action="update", entity_type="organization",
                entity_id=org.id, organization_id=org.id, detail={"name": org.name})
    db.commit()
    db.refresh(org)
    return _org_out(db, org)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_org(
    org_id: str,
    db: Session = Depends(get_db),
    actor: User = Depends(require_permission("org:manage")),
):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    if org.is_default:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The default organization cannot be deleted.")
    if _member_count(db, org.id) > 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reassign members before deleting this organization.")
    db.delete(org)
    write_audit(db, user_id=actor.id, action="delete", entity_type="organization", entity_id=org_id,
                organization_id=actor.organization_id)
    db.commit()
