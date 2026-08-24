"""Bridge from per-org AI provider records to a runtime provider chain.

The engine (`dbbuddy_core`) is decoupled from the application DB, so it never reads
provider records itself. This module resolves an organization's active provider
(and its fallback chain) into a flat list of self-contained
``ProviderRuntimeConfig`` objects that the backend injects into ``DBConfig``.
"""

import logging

from dbbuddy_core.ai_providers import ProviderRuntimeConfig

from .database import SessionLocal
from .models import AIProviderConfig
from .security import decrypt_secret

logger = logging.getLogger(__name__)


def _to_runtime(row: AIProviderConfig) -> ProviderRuntimeConfig:
    """Build a runtime config from a record, decrypting the key if present.

    A key that no longer decrypts (at-rest encryption key changed) is treated as
    absent — the adapter then reports it as a recoverable "no API key" error, which
    lets a configured fallback take over rather than raising a blank 500.
    """
    api_key = None
    if row.api_key_encrypted:
        try:
            api_key = decrypt_secret(row.api_key_encrypted)
        except Exception:  # noqa: BLE001 — stale/rotated key → treat as missing
            logger.warning("AI provider %s (%s) has an undecryptable key; treating as unset.", row.name, row.id)
    return ProviderRuntimeConfig(
        adapter=row.adapter, model=row.model, base_url=row.base_url,
        api_key=api_key, name=row.name,
    )


def resolve_active_provider_chain(org_id: str | None) -> list[ProviderRuntimeConfig] | None:
    """Return the org's active provider followed by its fallback chain, or None.

    The active provider is the ``enabled`` record with ``priority == 1``. Fallbacks
    are followed via ``fallback_provider_id`` (enabled links only), de-duplicated
    with a ``visited`` set so a mis-configured cycle terminates. Returns ``None``
    when the org has no usable active provider (the caller then falls back to the
    legacy ``ai_provider`` string, so nothing breaks before any records exist).
    """
    if not org_id:
        return None

    with SessionLocal() as db:
        active = (
            db.query(AIProviderConfig)
            .filter(
                AIProviderConfig.organization_id == org_id,
                AIProviderConfig.priority == 1,
                AIProviderConfig.enabled.is_(True),
            )
            .first()
        )
        if active is None:
            return None

        chain: list[ProviderRuntimeConfig] = []
        visited: set[str] = set()
        node = active
        while node is not None and node.id not in visited:
            visited.add(node.id)
            chain.append(_to_runtime(node))
            nxt = node.fallback_provider_id
            if not nxt or nxt in visited:
                break
            node = (
                db.query(AIProviderConfig)
                .filter(
                    AIProviderConfig.id == nxt,
                    AIProviderConfig.organization_id == org_id,
                    AIProviderConfig.enabled.is_(True),
                )
                .first()
            )

        return chain or None
