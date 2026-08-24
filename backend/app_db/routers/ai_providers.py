"""Unified AI provider management — providers as per-organization records.

Each record configures one provider (an ``adapter`` + ``base_url`` + ``model`` +
optional key). One record per org is the active default (``priority == 1``); an
optional ``fallback_provider_id`` chains to another for infra-failure resilience.
Supporting a new provider is creating a record — no code, no new settings section.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_permission, write_audit
from ..models import AIProviderConfig, User
from ..schemas import AIProviderIn, AIProviderOut, AIProviderTestResult, AIProviderUpdate, VALID_ADAPTERS
from ..security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/ai-providers", tags=["ai-providers"])


def _provider_out(row: AIProviderConfig) -> AIProviderOut:
    """Serialize a record. The key is never returned; ``credentials_ok`` probes
    whether a stored key still decrypts so the UI can flag it for re-entry."""
    has_key = bool(row.api_key_encrypted)
    credentials_ok = True
    if has_key:
        try:
            decrypt_secret(row.api_key_encrypted)  # plaintext discarded — probe only
        except Exception:
            credentials_ok = False
    return AIProviderOut(
        id=row.id, name=row.name, adapter=row.adapter, base_url=row.base_url,
        model=row.model, enabled=row.enabled, priority=row.priority,
        is_active=(row.priority == 1), has_key=has_key, credentials_ok=credentials_ok,
        fallback_provider_id=row.fallback_provider_id,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _get_owned(db: Session, provider_id: str, org_id: str) -> AIProviderConfig:
    row = db.get(AIProviderConfig, provider_id)
    if row is None or row.organization_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI provider not found")
    return row


def _validate_fallback(db: Session, org_id: str, record_id: str | None, fallback_id: str | None) -> None:
    """A fallback must be a same-org record, not the record itself, and must not
    close a cycle (following fallback links from it must never reach record_id)."""
    if not fallback_id:
        return
    if fallback_id == record_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A provider cannot fall back to itself.")
    target = db.get(AIProviderConfig, fallback_id)
    if target is None or target.organization_id != org_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fallback provider not found in this organization.")

    visited: set[str] = set()
    node = target
    while node is not None:
        if node.id == record_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fallback chain would form a cycle.")
        if node.id in visited or not node.fallback_provider_id:
            break
        visited.add(node.id)
        node = db.get(AIProviderConfig, node.fallback_provider_id)


@router.get("", response_model=list[AIProviderOut])
def list_providers(db: Session = Depends(get_db), user: User = Depends(require_permission("settings:ai"))):
    rows = (
        db.query(AIProviderConfig)
        .filter(AIProviderConfig.organization_id == user.organization_id)
        .order_by(AIProviderConfig.priority.desc(), AIProviderConfig.created_at.asc())
        .all()
    )
    return [_provider_out(r) for r in rows]


@router.post("", response_model=AIProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(
    req: AIProviderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    _validate_fallback(db, user.organization_id, None, req.fallback_provider_id)
    row = AIProviderConfig(
        organization_id=user.organization_id,
        name=req.name,
        adapter=req.adapter,
        base_url=(req.base_url or None),
        model=req.model,
        api_key_encrypted=encrypt_secret(req.api_key) if req.api_key else None,
        enabled=req.enabled,
        priority=0,  # new records are inactive until explicitly activated
        fallback_provider_id=req.fallback_provider_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # The only user-triggerable constraint on this table is the per-org unique
        # name (uq_ai_provider_org_name). Catch just that, so a genuine persistence
        # failure (connectivity, a future constraint) surfaces as a 500 instead of
        # being mislabeled a name conflict.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A provider with this name already exists.")
    write_audit(db, user_id=user.id, action="create", entity_type="ai_provider", entity_id=row.id,
                organization_id=user.organization_id, detail={"adapter": row.adapter})
    db.commit()
    db.refresh(row)
    return _provider_out(row)


@router.get("/{provider_id}", response_model=AIProviderOut)
def get_provider_record(
    provider_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    return _provider_out(_get_owned(db, provider_id, user.organization_id))


@router.patch("/{provider_id}", response_model=AIProviderOut)
def update_provider(
    provider_id: str,
    req: AIProviderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    """Edit a record. Any subset of fields may change; a non-empty ``api_key``
    re-encrypts under the current key (the way to heal a stale-key record). A
    blank/omitted key is left untouched."""
    row = _get_owned(db, provider_id, user.organization_id)

    if req.adapter is not None:
        row.adapter = req.adapter
    if req.name is not None:
        row.name = req.name
    if req.base_url is not None:
        row.base_url = req.base_url or None
    if req.model is not None:
        row.model = req.model
    if req.enabled is not None:
        row.enabled = req.enabled
    if "fallback_provider_id" in req.model_fields_set:
        _validate_fallback(db, user.organization_id, row.id, req.fallback_provider_id)
        row.fallback_provider_id = req.fallback_provider_id
    if req.api_key:
        row.api_key_encrypted = encrypt_secret(req.api_key)

    # Guard the invariant that an OpenAI-compatible provider always has an endpoint.
    if row.adapter == "openai_compatible" and not (row.base_url or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "openai_compatible providers require a base_url.")
    if row.adapter not in VALID_ADAPTERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported adapter {row.adapter!r}.")

    try:
        db.flush()
    except IntegrityError:
        # The only user-triggerable constraint on this table is the per-org unique
        # name (uq_ai_provider_org_name). Catch just that, so a genuine persistence
        # failure (connectivity, a future constraint) surfaces as a 500 instead of
        # being mislabeled a name conflict.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A provider with this name already exists.")
    write_audit(db, user_id=user.id, action="update", entity_type="ai_provider", entity_id=row.id,
                organization_id=user.organization_id, detail={"key_changed": bool(req.api_key)})
    db.commit()
    db.refresh(row)
    return _provider_out(row)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    row = _get_owned(db, provider_id, user.organization_id)
    # Unlink any records that fall back to this one so no dangling reference remains.
    db.query(AIProviderConfig).filter(
        AIProviderConfig.organization_id == user.organization_id,
        AIProviderConfig.fallback_provider_id == provider_id,
    ).update({AIProviderConfig.fallback_provider_id: None}, synchronize_session=False)
    db.delete(row)
    write_audit(db, user_id=user.id, action="delete", entity_type="ai_provider", entity_id=provider_id,
                organization_id=user.organization_id)
    db.commit()


@router.post("/{provider_id}/activate", response_model=AIProviderOut)
def activate_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    """Make this the org's active/default provider (priority 1). Demotes whichever
    record was active. Takes effect immediately — no restart."""
    row = _get_owned(db, provider_id, user.organization_id)
    if not row.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enable the provider before making it active.")
    # Demote any currently-active provider, then promote this one.
    db.query(AIProviderConfig).filter(
        AIProviderConfig.organization_id == user.organization_id,
        AIProviderConfig.priority == 1,
        AIProviderConfig.id != row.id,
    ).update({AIProviderConfig.priority: 0}, synchronize_session=False)
    row.priority = 1
    write_audit(db, user_id=user.id, action="activate", entity_type="ai_provider", entity_id=row.id,
                organization_id=user.organization_id)
    db.commit()
    db.refresh(row)
    return _provider_out(row)


@router.post("/{provider_id}/duplicate", response_model=AIProviderOut, status_code=status.HTTP_201_CREATED)
def duplicate_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    """Clone a record, including its stored key — so a user can try another model
    on the same credential without re-entering it. The copy starts inactive."""
    row = _get_owned(db, provider_id, user.organization_id)

    # Find a free "<name> (copy)" name within the org.
    base = f"{row.name} (copy)"
    name = base
    n = 2
    while db.query(AIProviderConfig).filter(
        AIProviderConfig.organization_id == user.organization_id, AIProviderConfig.name == name,
    ).first() is not None:
        name = f"{base} {n}"
        n += 1

    clone = AIProviderConfig(
        organization_id=user.organization_id,
        name=name,
        adapter=row.adapter,
        base_url=row.base_url,
        model=row.model,
        api_key_encrypted=row.api_key_encrypted,  # carry the encrypted key verbatim
        enabled=row.enabled,
        priority=0,
        fallback_provider_id=row.fallback_provider_id,
    )
    db.add(clone)
    db.flush()
    write_audit(db, user_id=user.id, action="duplicate", entity_type="ai_provider", entity_id=clone.id,
                organization_id=user.organization_id, detail={"source": provider_id})
    db.commit()
    db.refresh(clone)
    return _provider_out(clone)


@router.post("/{provider_id}/test", response_model=AIProviderTestResult)
def test_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("settings:ai")),
):
    """Probe transport/credentials with a cheap healthcheck (not classification)."""
    from dbbuddy_core.ai_providers import ProviderRuntimeConfig, get_provider

    row = _get_owned(db, provider_id, user.organization_id)
    api_key = None
    if row.api_key_encrypted:
        try:
            api_key = decrypt_secret(row.api_key_encrypted)
        except Exception:
            return AIProviderTestResult(
                ok=False, adapter=row.adapter, model=row.model,
                error="Stored API key can no longer be decrypted — re-enter it.",
            )
    cfg = ProviderRuntimeConfig(
        adapter=row.adapter, model=row.model, base_url=row.base_url, api_key=api_key, name=row.name,
    )
    try:
        provider = get_provider(cfg)
    except ValueError as exc:
        return AIProviderTestResult(ok=False, adapter=row.adapter, model=row.model, error=str(exc))
    ok, error = provider.healthcheck()
    return AIProviderTestResult(ok=ok, adapter=row.adapter, model=row.model, error=error)
