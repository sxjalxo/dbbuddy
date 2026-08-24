"""Login / registration throttling — shared across workers, degrading closed.

A sliding-window limiter keyed by ``(client-ip, identity)``. Two backends:

* **Redis (authoritative when reachable).** A sorted set per key, scores are
  attempt timestamps. One shared window across every worker process, so the
  configured limit is the *actual* limit no matter how the deployment is scaled.
* **In-process (always maintained).** The original ``deque`` window, kept in
  lockstep with Redis so it is a valid — if partial — view at any moment.

### Why this one does not fail open

The query rate limiter (``dbbuddy_core.rate_limiter``) fails open on purpose: no
Redis means queries still run and the customer's ERP just gets a little warmer.
Authentication is not that. If this limiter vanished when Redis did, a Redis
outage would silently hand every attacker unlimited password attempts — a
security control disappearing exactly when the system is already degraded.

So it degrades *closed*: on a Redis error the local window still decides, and the
local cap is divided by ``LOGIN_GUARD_WORKERS`` so that N workers each enforcing
``cap/N`` add up to roughly the intended global limit instead of N times it. The
limiter therefore gets **stricter** when the shared view is lost, never laxer,
and never disappears.

Note what "fail closed" deliberately does *not* mean here: refusing all logins
when Redis is down. That converts a cache outage into a total authentication
outage — trading a bounded weakening for a guaranteed one. Degrading to a
stricter local limit is the better failure mode.

### Residual limitation

With Redis down and ``LOGIN_GUARD_WORKERS`` set correctly the effective global
ceiling is approximate (each worker counts only its own traffic), and credential
stuffing spread across many distinct accounts from one IP is still only partly
mitigated — each identity is its own key. Front an internet-facing deployment
with a WAF for that.
"""

import math
import os
import threading
import time
import uuid
from collections import defaultdict, deque

from dbbuddy_core.logger import get_logger

logger = get_logger()

# Max failed attempts allowed within the window before a key is locked out.
MAX_FAILURES = 5
WINDOW_SECONDS = 900  # 15 minutes

# Registration is throttled with the same sliding window but a more lenient cap:
# it counts *every* attempt (not just failures), so the limit must tolerate a few
# analysts self-registering from behind one shared/NAT IP while still blunting
# automated account-creation loops.
MAX_REGISTRATIONS = 10

# Password-reset requests, counted per (ip, address) like registration counts every
# attempt rather than only failures. This endpoint sends mail on someone else's
# behalf, so an unthrottled one is a way to flood a third party's inbox using this
# server's reputation — and a way to burn an SMTP quota. Lower than registration
# because a person legitimately asks for a reset once, maybe twice.
MAX_RESET_REQUESTS = 5

# How many worker processes serve this deployment. Only consulted while the
# shared backend is unavailable, to shrink each worker's local cap so the
# processes together stay near the configured global limit. Default 1 leaves
# single-instance behavior exactly as it was.
WORKER_COUNT = max(1, int(os.getenv("LOGIN_GUARD_WORKERS", "1")))

_KEY_PREFIX = "loginguard"

_lock = threading.Lock()
_failures: dict[str, deque] = defaultdict(deque)


# ── Shared backend ───────────────────────────────────────────────────────────

def _cache():
    """The shared Redis cache, or None when it is not usable right now.

    Reuses the process-wide client (and its availability gate) rather than
    opening a second one, so a Redis that died mid-process costs one timeout per
    cooldown here too, not one per login.
    """
    try:
        from dbbuddy_core.context_store import _get_cache

        cache = _get_cache()
        return cache if cache is not None and cache._available() else None
    except Exception:  # noqa: BLE001 — never let a cache problem break auth
        return None


def _redis_key(key: str) -> str:
    return f"{_KEY_PREFIX}:{key}"


def _shared_count(cache, key: str, now: float) -> tuple[int, float | None] | None:
    """``(attempts_in_window, oldest_timestamp)`` from Redis, or None on failure.

    None means "could not consult the shared view" — the caller must then fall
    back to the stricter local decision, never to allowing the request.
    """
    rkey = _redis_key(key)
    try:
        pipe = cache.client.pipeline()
        pipe.zremrangebyscore(rkey, "-inf", now - WINDOW_SECONDS)
        pipe.zcard(rkey)
        pipe.zrange(rkey, 0, 0, withscores=True)
        _, count, oldest = pipe.execute()
        cache._note_success()
        oldest_ts = float(oldest[0][1]) if oldest else None
        return int(count), oldest_ts
    except Exception:  # noqa: BLE001
        cache._note_error()
        logger.warning("Login guard could not read the shared window; "
                       "falling back to the stricter local limit", exc_info=True)
        return None


def _local_cap(max_attempts: int) -> int:
    """The per-process cap to apply when the shared window is unavailable."""
    return max(1, math.ceil(max_attempts / WORKER_COUNT))


# ── Public API ───────────────────────────────────────────────────────────────

def _prune(dq: deque, now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    while dq and dq[0] <= cutoff:
        dq.popleft()


def retry_after(key: str, max_attempts: int = MAX_FAILURES) -> int | None:
    """Seconds the caller must wait if ``key`` is currently locked out, else None."""
    now = time.time()

    cache = _cache()
    if cache is not None:
        shared = _shared_count(cache, key, now)
        if shared is not None:
            count, oldest = shared
            if count >= max_attempts and oldest is not None:
                return int(oldest + WINDOW_SECONDS - now) + 1
            return None
        # Shared view unreadable — fall through to the local window below.

    # Reached only when the shared window is absent or unreadable. The local cap
    # is per process, so it is divided by the worker count: N workers each
    # allowing cap/N ≈ the intended global limit, rather than N times it.
    effective = _local_cap(max_attempts)
    with _lock:
        dq = _failures.get(key)
        if not dq:
            return None
        _prune(dq, now)
        if not dq:
            _failures.pop(key, None)
            return None
        if len(dq) >= effective:
            return int(dq[0] + WINDOW_SECONDS - now) + 1
        return None


def record_failure(key: str) -> None:
    """Register one failed attempt against ``key``, in both windows.

    Written to Redis *and* the local deque unconditionally. Keeping the local
    window populated during normal operation is what makes the degraded path
    meaningful: if Redis disappears mid-attack, this process already knows about
    the attempts it has seen rather than starting from zero.
    """
    now = time.time()

    cache = _cache()
    if cache is not None:
        rkey = _redis_key(key)
        try:
            pipe = cache.client.pipeline()
            # Unique member per attempt — two failures in the same clock tick must
            # count twice, and a plain score would collide.
            pipe.zadd(rkey, {f"{now}:{uuid.uuid4().hex}": now})
            pipe.zremrangebyscore(rkey, "-inf", now - WINDOW_SECONDS)
            pipe.expire(rkey, WINDOW_SECONDS)
            pipe.execute()
            cache._note_success()
        except Exception:  # noqa: BLE001
            cache._note_error()
            logger.warning("Login guard could not record an attempt in the shared "
                           "window; the local limit still applies", exc_info=True)

    with _lock:
        dq = _failures[key]
        _prune(dq, now)
        dq.append(now)


def reset(key: str) -> None:
    """Clear the failure history for ``key`` (call on a successful auth)."""
    cache = _cache()
    if cache is not None:
        try:
            cache.client.delete(_redis_key(key))
            cache._note_success()
        except Exception:  # noqa: BLE001
            cache._note_error()
            # A stale shared counter only makes the limiter stricter, so this is
            # safe to swallow — but it will lock out a legitimate user early, so
            # it is worth seeing.
            logger.warning("Login guard could not clear the shared window for a "
                           "successful auth", exc_info=True)
    with _lock:
        _failures.pop(key, None)


def clear_all() -> None:
    """Drop all tracked history, local and shared. For test isolation.

    Clearing Redis matters as much as clearing the deque: the shared window
    outlives the process, so without this a suite that registers several users
    from one TestClient IP inherits the previous run's lockout.
    """
    with _lock:
        _failures.clear()
    cache = _cache()
    if cache is not None:
        try:
            cache.clear_prefix(_KEY_PREFIX)
        except Exception:  # noqa: BLE001
            logger.debug("Login guard could not clear the shared window", exc_info=True)


def backend_name() -> str:
    """Which window is authoritative right now: 'redis' or 'in-process'."""
    return "redis" if _cache() is not None else "in-process"
