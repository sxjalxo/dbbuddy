"""Auth dependencies — resolve the current user from a JWT and gate by permission."""

import logging
from datetime import datetime, timezone
import os
import threading
import uuid
import time
from collections import OrderedDict

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import AuditLog, User
from .security import decode_token

logger = logging.getLogger(__name__)

# ── Access-token revocation (see security.create_access_token) ────────────────
# The claims-only path exists to keep /query, /execute and /analyze off the app DB
# on every request. Checking revocation naively would undo that, so the account's
# (token_version, is_active) is cached in-process for a few seconds: the hot path
# does at most one app-DB read per user per window instead of one per request,
# and a logout / deactivation stops working credentials within that window rather
# than at the end of the token's full lifetime.
#
# ### Scope — read this before scaling out
#
# The cache is **per worker process**, like `login_guard`, `erp_concurrency` and
# the prepared DB contexts (see docs/PRE_DEPLOYMENT_REVIEW.md §1). Concretely:
# `invalidate_revocation()` only clears the entry in the worker that *handled the
# logout*. Other workers keep serving their own cached state until it expires, so
# with N workers revocation is immediate on one and bounded by
# `AUTH_REVOCATION_CACHE_TTL` on the rest.
#
# That is a real, if small, security window and it is why the TTL default is 10 s
# rather than something more efficient: the TTL *is* the multi-worker convergence
# bound, so it is sized to be tolerable as one, not to maximize cache hits. A
# shared store (Redis) would make invalidation global; until then, prefer one
# worker, or accept a bounded window and keep the TTL short.
REVOCATION_CACHE_TTL = float(os.getenv("AUTH_REVOCATION_CACHE_TTL", "10"))

# Hard cap on cached accounts. Without one this dict is an unbounded, never-swept
# map keyed by user id: entries expire *logically* but are only ever overwritten,
# never removed, so a long-lived process accumulates one entry per user that has
# ever authenticated — and self-registration means an attacker can choose how many
# that is. Eviction is LRU with an expired-first sweep.
REVOCATION_CACHE_MAX = int(os.getenv("AUTH_REVOCATION_CACHE_MAX", "10000"))

# How long a request waits on another thread's in-flight read before doing its
# own. Short: this is a single indexed primary-key lookup.
_FLIGHT_WAIT_SECONDS = 5.0

_rev_lock = threading.Lock()
# user_id -> (expires_at, token_version, is_active)
_rev_cache: "OrderedDict[str, tuple[float, int, bool]]" = OrderedDict()
# user_id -> invalidation counter. Bumped by invalidate_revocation(); a reader
# publishes its result only if the counter has not moved since it started.
_rev_gen: dict[str, int] = {}
# user_id -> Event signalling an in-flight DB read (single-flight).
_rev_flights: dict[str, threading.Event] = {}


def _evict_locked() -> None:
    """Trim the cache to ``REVOCATION_CACHE_MAX``. Caller holds ``_rev_lock``."""
    if len(_rev_cache) <= REVOCATION_CACHE_MAX:
        return
    now = time.monotonic()
    for key in [k for k, v in _rev_cache.items() if v[0] <= now]:
        _rev_cache.pop(key, None)
    while len(_rev_cache) > REVOCATION_CACHE_MAX:
        _rev_cache.popitem(last=False)  # least-recently-used
    # Generations only matter while an entry is cached or a read is in flight.
    live = _rev_cache.keys() | _rev_flights.keys()
    for key in [k for k in _rev_gen if k not in live]:
        _rev_gen.pop(key, None)


def _read_account_state(user_id: str) -> tuple[int, bool] | None:
    from .database import SessionLocal

    with SessionLocal() as db:
        user = db.get(User, user_id)
        return (user.token_version, user.is_active) if user is not None else None


def _account_state(user_id: str | None) -> tuple[int, bool] | None:
    """(token_version, is_active) for a user, cached briefly. None if unknown.

    Three concurrency properties this has to get right, none of them optional:

    1. **No stampede.** A cache miss under load (TTL expiry on a hot service
       account hammering /query) would otherwise send every in-flight request for
       that user to the app DB at once. Readers single-flight through an Event —
       the same shape as ``context_store._resolve_schema``.
    2. **No stale resurrection.** A reader that started *before* a logout must not
       publish the pre-logout value *after* ``invalidate_revocation()`` ran; that
       would keep a revoked token working for a full TTL despite an explicit,
       synchronous invalidation — silently defeating the guarantee the caller
       thinks it bought. Each reader captures the user's generation counter up
       front and publishes only if it has not moved.
    3. **No unbounded growth.** See ``REVOCATION_CACHE_MAX``.
    """
    if not user_id:
        return None

    now = time.monotonic()
    with _rev_lock:
        hit = _rev_cache.get(user_id)
        if hit is not None and hit[0] > now:
            _rev_cache.move_to_end(user_id)  # LRU touch
            return hit[1], hit[2]
        flight = _rev_flights.get(user_id)
        leader = flight is None
        if leader:
            flight = _rev_flights[user_id] = threading.Event()
        generation = _rev_gen.get(user_id, 0)

    if not leader:
        flight.wait(timeout=_FLIGHT_WAIT_SECONDS)
        with _rev_lock:
            hit = _rev_cache.get(user_id)
            if hit is not None and hit[0] > time.monotonic():
                _rev_cache.move_to_end(user_id)
                return hit[1], hit[2]
            # The leader failed, or an invalidation landed while it read. Fall
            # through and read for ourselves rather than inherit its outcome —
            # never serve a request off a failed lookup.
            generation = _rev_gen.get(user_id, 0)

    try:
        state = _read_account_state(user_id)
    finally:
        if leader:
            with _rev_lock:
                _rev_flights.pop(user_id, None)
            flight.set()

    with _rev_lock:
        if state is not None and _rev_gen.get(user_id, 0) == generation:
            _rev_cache[user_id] = (
                time.monotonic() + REVOCATION_CACHE_TTL, state[0], state[1],
            )
            _rev_cache.move_to_end(user_id)
            _evict_locked()
    return state


def reset_revocation_cache() -> None:
    """Drop all cached account state. For tests and for an immediate re-read."""
    with _rev_lock:
        _rev_cache.clear()
        _rev_gen.clear()


# ── Cross-worker invalidation ─────────────────────────────────────────────────
# Dropping the local entry only fixes the process that did the revoking. With N
# workers the other N−1 kept serving the old (token_version, is_active) until
# their own TTL expired — so a logout or a deactivation stayed partly effective
# for up to REVOCATION_CACHE_TTL on the endpoints that authorize from JWT claims
# alone (/query, /execute, /analyze), which are the ones that reach customer data.
#
# The two obvious alternatives both cost more than they save: reading the shared
# store per request replaces one DB read per window with one Redis read per
# *request* — the exact property this cache exists to protect — and shortening the
# TTL narrows the window without closing it, paying in database load.
#
# So the revoking process publishes the user id and every worker drops its own
# entry on receipt. No per-request cost, and convergence in a fan-out.
#
# Without Redis this degrades to the previous behaviour — immediate locally,
# bounded by the TTL elsewhere — not to something worse. Same rule login_guard
# follows: a control protecting authentication may weaken when its dependency is
# gone, but it must not vanish, and must not take the service down with it.
REVOCATION_CHANNEL = "dbbuddy:revocation"

_listener_started = False
_listener_lock = threading.Lock()


def _revocation_channel():
    """The Redis client to publish/subscribe on, or None when unavailable."""
    try:
        from dbbuddy_core.context_store import _get_cache

        cache = _get_cache()
        if cache is None or getattr(cache, "client", None) is None:
            return None
        return cache.client
    except Exception:  # noqa: BLE001 — no shared store is a supported state
        return None


def _publish_invalidation(user_id: str) -> None:
    client = _revocation_channel()
    if client is None:
        return
    client.publish(REVOCATION_CHANNEL, user_id)


def _apply_remote_invalidation(user_id) -> None:
    """Drop one user's entry because another worker revoked them.

    Tolerant of anything arriving on the channel: a pub/sub topic is not a trusted
    schema, and a malformed message must not take down the listener thread that
    every other revocation depends on.
    """
    if not isinstance(user_id, str) or not user_id:
        return
    with _rev_lock:
        _rev_cache.pop(user_id, None)
        # The receiving worker has in-flight readers too — same reason the local
        # path bumps this.
        _rev_gen[user_id] = _rev_gen.get(user_id, 0) + 1


def _listen_for_invalidations(pubsub) -> None:
    """Poll the channel forever, surviving idle timeouts and reconnects.

    Deliberately ``get_message(timeout=...)`` rather than the more natural
    ``for message in pubsub.listen()``. The shared client carries a short
    ``socket_timeout`` — correct for request-path commands, where a hung Redis
    must not stall a query — but a subscriber is *supposed* to sit idle between
    messages, so a blocking listen raises ``TimeoutError`` within a second and
    kills the thread. The feature then looks fine and silently stops working,
    which is worse than never having shipped it.

    So an idle timeout is an expected outcome here, not an error, and anything
    else is logged and retried rather than allowed to end the loop: this thread is
    the only thing keeping every other worker's revocations honest.
    """
    while True:
        try:
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        except TimeoutError:
            continue                      # idle: the normal case
        except Exception:  # noqa: BLE001 — connection reset, Redis restart, …
            logger.debug("revocation listener hiccuped; retrying", exc_info=True)
            time.sleep(1.0)
            continue

        if not message:
            continue
        try:
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            _apply_remote_invalidation(data)
        except Exception:  # noqa: BLE001 — one bad message must not end the loop
            logger.debug("revocation listener ignored a bad message", exc_info=True)


def start_revocation_listener() -> bool:
    """Subscribe this worker to revocations. Returns whether it is listening.

    Called once at startup. Idempotent, and a no-op without Redis — in which case
    the TTL remains the convergence bound, as it was before.
    """
    global _listener_started
    with _listener_lock:
        if _listener_started:
            return True
        client = _revocation_channel()
        if client is None:
            return False
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(REVOCATION_CHANNEL)
        except Exception:  # noqa: BLE001
            logger.warning("could not subscribe to revocation broadcasts; sessions "
                           "will converge within AUTH_REVOCATION_CACHE_TTL instead",
                           exc_info=True)
            return False

        thread = threading.Thread(
            target=_listen_for_invalidations, args=(pubsub,),
            name="revocation-listener", daemon=True,
        )
        thread.start()
        _listener_started = True
        logger.info("listening for cross-worker session revocations")
        return True


def invalidate_revocation(user_id: str | None) -> None:
    """Force the next request for ``user_id`` to re-read the account.

    Called wherever ``token_version`` is bumped, so a logout / deactivation /
    forced-reset takes effect on the very next request in **this** process rather
    than at the end of the cache window.

    Other workers are told directly (see ``_publish_invalidation``) and drop their
    own entry on receipt. Only when there is no shared store do they fall back to
    converging within ``REVOCATION_CACHE_TTL``.

    Bumping the generation is what makes this safe against a concurrent reader:
    dropping the entry alone would let a read already in flight write the
    pre-invalidation value straight back.
    """
    if not user_id:
        return
    with _rev_lock:
        _rev_cache.pop(user_id, None)
        _rev_gen[user_id] = _rev_gen.get(user_id, 0) + 1

    # After the local drop, and never at its expense: a broadcast that fails must
    # not undo the revocation in the process that actually performed it.
    try:
        _publish_invalidation(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not broadcast a session revocation; other workers "
                       "will converge within AUTH_REVOCATION_CACHE_TTL",
                       exc_info=True)


def revocation_cache_stats() -> dict:
    """Size/occupancy of the revocation cache, for diagnostics and tests."""
    with _rev_lock:
        return {
            "entries": len(_rev_cache),
            "max_entries": REVOCATION_CACHE_MAX,
            "in_flight": len(_rev_flights),
            "tracked_generations": len(_rev_gen),
            "ttl_seconds": REVOCATION_CACHE_TTL,
        }


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve and validate the bearer access token → User."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid access token") from None

    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # This path already holds the live row, so the revocation check is free here.
    if int(payload.get("tv", 0)) != int(user.token_version):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token has been revoked")
    return user


def require_permission(permission: str):
    """Dependency factory that enforces a single permission claim."""
    def _checker(user: User = Depends(get_current_user)) -> User:
        if permission not in user.permission_names():
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {permission}")
        return user
    return _checker


def is_platform_admin(user: User) -> bool:
    """A platform admin (holds ``org:manage``) acts across all organizations;
    everyone else with ``user:manage`` is scoped to their own org."""
    return "org:manage" in user.permission_names()


def get_token_payload(authorization: str | None = Header(default=None)) -> dict:
    """Decode and validate the bearer access token → JWT claims, with no DB hit.

    For hot paths (query/analyze/execute) where the token's own ``permissions``
    claim is authoritative and a per-request user lookup would only add latency.
    Use ``get_current_user`` instead when you need the live DB row.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid access token") from None

    # Revocation check, served from the short-TTL cache above. A token whose ``tv``
    # no longer matches the account was minted before a logout / password change /
    # MFA change / deactivation, and a deactivated account is rejected outright —
    # neither was previously enforced anywhere on the claims-only path.
    state = _account_state(payload.get("sub"))
    if state is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    token_version, is_active = state
    if not is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    if int(payload.get("tv", 0)) != int(token_version):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token has been revoked")
    return payload


def require_token_permission(permission: str):
    """Like ``require_permission`` but enforced purely from JWT claims (no DB)."""
    def _checker(payload: dict = Depends(get_token_payload)) -> dict:
        if permission not in (payload.get("permissions") or []):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {permission}")
        return payload
    return _checker


def write_audit(db: Session, *, user_id: str | None, action: str,
                entity_type: str | None = None, entity_id: str | None = None,
                detail: dict | None = None, organization_id: str | None = None,
                ip_address: str | None = None) -> None:
    """Append an audit-log row modelled as (actor, org, entity, action). Caller commits.

    ``action`` should be a bare verb (login, create, publish, …) and
    ``entity_type`` the entity it acted on (user, chart, report, …).
    """
    from .request_context import get_request_id

    from .audit_integrity import sign

    entry = AuditLog(
        user_id=user_id, organization_id=organization_id, action=action,
        entity_type=entity_type, entity_id=entity_id, detail=detail, ip_address=ip_address,
        request_id=get_request_id(),
    )
    # Defaults are applied by the ORM/DB, not by the constructor, so id and
    # created_at are still None here — and both are signed. Fill them in first,
    # or every row would be signed over a different value than it stores.
    if entry.id is None:
        entry.id = str(uuid.uuid4())
    if entry.created_at is None:
        entry.created_at = datetime.now(timezone.utc)
    entry.entry_hash = sign(entry)
    db.add(entry)
