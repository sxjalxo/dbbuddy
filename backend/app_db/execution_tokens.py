"""Single-use execution tokens — the server-side binding between a reviewed
``/query`` plan and the ``/execute`` that runs it.

A planner-generated write is executed only by redeeming a token that maps to the
*exact SQL the server produced and the user confirmed*. Because the SQL lives on
the server, the client cannot alter it after confirmation — closing the gap where
``/execute`` would run whatever SQL the client happened to post.

Tokens are:
  * **opaque bearer strings** — only their SHA-256 is stored (like personal API
    keys), so a leaked application DB does not expose reusable tokens;
  * **single-use** — consumed atomically on redemption (a single UPDATE guarded on
    ``consumed_at IS NULL``, safe across worker processes);
  * **short-lived** — they expire after ``TOKEN_TTL_SECONDS``;
  * **context-bound** — tied to a hash of the execution target (engine/host/port/
    database/user), so a token cannot be replayed against a different database.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .models import ExecutionToken

# How long a confirmed plan stays executable before the user must re-confirm.
TOKEN_TTL_SECONDS = 300  # 5 minutes


class TokenError(Exception):
    """Base class for redemption failures (invalid / expired / wrong target)."""


class TokenInvalid(TokenError):
    """Unknown token, or it does not belong to the caller."""


class TokenExpiredOrUsed(TokenError):
    """The token has already been consumed or has expired."""


class TokenContextMismatch(TokenError):
    """The token is valid but was issued for a different execution target."""


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def context_hash(engine, host, port, database, user) -> str:
    """A stable fingerprint of the execution target (no secrets).

    Redemption must resolve to the same target the plan was reviewed against, so
    a token minted for one connection cannot run its write against another.
    """
    material = f"{engine}|{host}|{port}|{database}|{user}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def issue(
    db: Session,
    *,
    user_id: str,
    sql: str,
    safety_category: str,
    context_hash: str,
    connection_id: str | None = None,
    organization_id: str | None = None,
) -> str:
    """Mint a token for ``sql`` and persist it. Returns the raw token (shown once).

    The caller is expected to ``commit`` (this stages the row via ``add``/``flush``
    so it participates in the caller's transaction).
    """
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.add(ExecutionToken(
        id=_hash_token(raw),
        user_id=user_id,
        organization_id=organization_id,
        database_connection_id=connection_id,
        sql=sql,
        safety_category=safety_category,
        context_hash=context_hash,
        created_at=now,
        expires_at=now + timedelta(seconds=TOKEN_TTL_SECONDS),
    ))
    db.flush()
    return raw


def redeem(db: Session, *, token: str, user_id: str, context_hash: str) -> ExecutionToken:
    """Validate and atomically consume a token, returning its stored row.

    Order matters: the execution-target check happens *before* consumption, so
    presenting a valid token against the wrong database rejects without burning it
    (the user can retry against the right connection). Double-use and expiry are
    enforced by an atomic guarded UPDATE, so two concurrent workers cannot both
    redeem the same token. The caller ``commit``s the transaction.
    """
    token_id = _hash_token(token)
    row = db.get(ExecutionToken, token_id)
    if row is None or row.user_id != user_id:
        # Same generic error for unknown vs. not-yours — never confirm existence.
        raise TokenInvalid("This execution token is not valid.")

    if row.context_hash != context_hash:
        raise TokenContextMismatch(
            "This execution token was issued for a different database connection."
        )

    now = datetime.now(timezone.utc)
    consumed = (
        db.query(ExecutionToken)
        .filter(
            ExecutionToken.id == token_id,
            ExecutionToken.consumed_at.is_(None),
            ExecutionToken.expires_at > now,
        )
        .update({ExecutionToken.consumed_at: now}, synchronize_session=False)
    )
    if not consumed:
        raise TokenExpiredOrUsed("This execution token has already been used or has expired.")

    db.refresh(row)
    return row
