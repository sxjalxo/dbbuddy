"""Personal API keys — long-lived credentials for the CLI and automation.

A key is minted for the authenticated user and shown exactly once. It carries no
permissions of its own: it is *exchanged* for a normal JWT access/refresh pair
(``POST /auth/keys/exchange``), so every downstream endpoint keeps using the
existing token-based authorization middleware unchanged, and a key always
reflects its owner's *current* roles/permissions.

Keys are self-scoped — a user manages only their own — mirroring the web app's
key management screen so the two stay in sync automatically.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, write_audit
from ..models import ApiKey, User
from ..schemas import (
    ApiKeyCreate, ApiKeyCreated, ApiKeyExchangeRequest, ApiKeyOut, ApiKeyUpdate, TokenPair,
)
from ..security import (
    create_access_token, create_refresh_token, generate_api_key,
    parse_api_key_prefix, verify_api_key,
)

router = APIRouter(prefix="/auth/keys", tags=["api-keys"])


def _client_ip(request: Request | None) -> str | None:
    return request.client.host if request and request.client else None


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_key(
    req: ApiKeyCreate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mint a new personal API key. The raw token is returned once and never again."""
    plaintext, prefix, token_hash = generate_api_key()
    key = ApiKey(user_id=user.id, name=req.name.strip(), token_prefix=prefix, token_hash=token_hash)
    db.add(key)
    db.flush()
    write_audit(db, user_id=user.id, action="create", entity_type="api_key", entity_id=key.id,
                organization_id=user.organization_id, detail={"name": key.name},
                ip_address=_client_ip(request))
    db.commit()
    db.refresh(key)
    return ApiKeyCreated(
        id=key.id, name=key.name, token_prefix=key.token_prefix,
        last_used_at=key.last_used_at, revoked_at=key.revoked_at, created_at=key.created_at,
        api_key=plaintext,
    )


@router.get("", response_model=list[ApiKeyOut])
def list_keys(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """List the caller's keys (secrets never included). Revoked keys are kept."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


@router.patch("/{key_id}", response_model=ApiKeyOut)
def rename_key(
    key_id: str,
    req: ApiKeyUpdate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rename a key. Only the label changes — the secret is unaffected."""
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    key.name = req.name.strip()
    write_audit(db, user_id=user.id, action="update", entity_type="api_key", entity_id=key.id,
                organization_id=user.organization_id, detail={"name": key.name},
                ip_address=_client_ip(request))
    db.commit()
    db.refresh(key)
    return key


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: str,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke a key. Idempotent — revoking an already-revoked key is a no-op."""
    from ..models import _now

    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if key.revoked_at is None:
        key.revoked_at = _now()
        write_audit(db, user_id=user.id, action="revoke", entity_type="api_key", entity_id=key.id,
                    organization_id=user.organization_id, ip_address=_client_ip(request))
        db.commit()


@router.post("/exchange", response_model=TokenPair)
def exchange_key(req: ApiKeyExchangeRequest, request: Request = None, db: Session = Depends(get_db)):
    """Exchange a raw API key for a normal JWT access/refresh pair.

    This is the CLI's bootstrap: it authenticates once with the key, then uses
    the returned tokens for every subsequent call — exactly like the web app.
    """
    token = (req.api_key or "").strip()
    prefix = parse_api_key_prefix(token)
    key = db.query(ApiKey).filter(ApiKey.token_prefix == prefix).one_or_none() if prefix else None

    # Uniform failure for unknown/malformed/revoked keys — don't leak which.
    if key is None or key.revoked_at is not None or not verify_api_key(token, key.token_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key.")

    user = db.get(User, key.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key.")

    from ..models import _now
    key.last_used_at = _now()
    write_audit(db, user_id=user.id, action="login", entity_type="api_key", entity_id=key.id,
                organization_id=user.organization_id, detail={"amr": ["apikey"]},
                ip_address=_client_ip(request))
    db.commit()

    return TokenPair(
        access_token=create_access_token(
            user_id=user.id, email=user.email, org_id=user.organization_id,
            roles=user.role_names(), permissions=user.permission_names(), amr=["apikey"],
            token_version=user.token_version,
        ),
        refresh_token=create_refresh_token(user_id=user.id, token_version=user.token_version),
    )
