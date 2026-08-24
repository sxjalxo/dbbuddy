"""Signatures that make an edited audit row detectable.

``audit_logs`` rows were ordinary rows. Anyone with write access to the
application database could change who did what, and nothing would show — which
matters most in exactly the situation these rows exist for: someone reconstructing
an incident, using them as evidence.

Each row carries an HMAC over its immutable content, keyed by the server secret.
Database access alone is no longer enough to forge one.

## Keyed, not merely hashed

A plain digest would let anyone who could edit a row also recompute its hash. The
key is what separates "can write to the database" from "can write to the database
*and* holds the application secret", and those are usually different people —
often a DBA and a deployment, or an intruder and neither.

It is derived from ``APP_SECRET_KEY`` through a distinct label, so the audit key
is not the encryption key even though both come from one secret. Rotating that
secret invalidates existing signatures: they were made by the old key, and the new
one legitimately cannot verify them. That is a real consequence and is documented
in the rotation procedure rather than worked around, because the alternative —
re-signing during rotation — would mean the rotation tool can forge audit rows.

## What this catches, and what it does not

**Caught:** any modification to a stored row.

**Not caught:** deleting one outright. Nothing in a per-row signature says how many
rows there should be.

A hash *chain* would catch deletion and was deliberately not built. Computing "the
previous row's hash" at insert time means reading the current tail inside the
writing transaction; two workers doing that concurrently choose the same
predecessor and the chain forks. The verifier would then report tampering on an
honest system, and the first false alarm is what teaches everyone to ignore the
next one. Closing it properly needs a database-assigned monotonic sequence so gaps
are visible without any app-side coordination — tracked in docs/ROADMAP.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timezone

from .config import settings

# Distinct from the at-rest encryption key derived from the same secret. Reusing
# one key for two purposes means a weakness in either becomes a weakness in both.
_KEY_LABEL = b"dbbuddy-audit-integrity-v1"

# Every field that describes what happened. Adding a meaningful column to
# AuditLog means adding it here — an unsigned field is one an attacker may edit
# freely, and the omission is invisible until someone relies on it.
_SIGNED_FIELDS = (
    "id", "user_id", "organization_id", "entity_type", "action",
    "entity_id", "ip_address", "request_id",
)


def _key() -> bytes:
    seed = (settings.APP_SECRET_KEY or settings.JWT_SECRET or "").encode("utf-8")
    return hmac.new(_KEY_LABEL, seed, hashlib.sha256).digest()


def _canonical(row) -> bytes:
    """A stable byte representation of one row.

    ``detail`` is serialised with sorted keys: JSON object order is not meaningful,
    and letting it change the signature would report tampering every time an
    unrelated dict happened to be built in a different order.
    """
    payload = {field: getattr(row, field, None) for field in _SIGNED_FIELDS}

    # Normalised to naive UTC with fixed precision, deliberately.
    #
    # The obvious `created_at.isoformat()` is wrong: SQLite hands back a *naive*
    # datetime even for a timezone-aware column, so a row signed with "+00:00" in
    # it fails verification the moment it is read back. Every row would then be
    # reported as tampered on a completely honest system — the exact false alarm
    # that teaches people to ignore the verifier.
    created_at = getattr(row, "created_at", None)
    if created_at is None:
        payload["created_at"] = None
    else:
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        payload["created_at"] = created_at.strftime("%Y-%m-%dT%H:%M:%S.%f")

    detail = getattr(row, "detail", None)
    payload["detail"] = json.dumps(detail, sort_keys=True, default=str) if detail else None

    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")


def sign(row) -> str:
    """The signature for this row."""
    return hmac.new(_key(), _canonical(row), hashlib.sha256).hexdigest()


def verify(row) -> bool:
    """Whether this row still matches its signature.

    An unsigned row returns False rather than True: rows written before signing
    existed are *reported*, not silently accepted, so "verified" never quietly
    means "not checked".
    """
    stored = getattr(row, "entry_hash", None)
    if not stored:
        return False
    return hmac.compare_digest(stored, sign(row))


def verify_rows(rows) -> tuple[int, list]:
    """Check many rows. Returns ``(checked, failures)``."""
    failures = [row for row in rows if not verify(row)]
    return len(list(rows)), failures
