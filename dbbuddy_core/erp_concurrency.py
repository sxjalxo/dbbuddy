"""Backpressure: a concurrency ceiling per customer database.

DB Buddy queries databases it does not own. Pool sizes and parallelism limits
protect *DB Buddy* from running out of connections; nothing so far protected the
**customer's ERP** from DB Buddy. Eighty analysts opening dashboards at 9 a.m. is
a legitimate, entirely foreseeable usage pattern, and with everything else working
correctly — Redis warm, SQL fast, pools sized right — it still lands as hundreds
of simultaneous queries on a production ERP that also has a business to run.

An analytics tool making its customer's database unavailable because the tool got
popular is the worst failure mode available to this product. So concurrency is
capped **per target database**: work beyond the cap waits rather than piling on.

    ERP A → at most 10 concurrent queries
    ERP B → at most 10 concurrent queries
    (independent; a slow ERP A never starves ERP B)

Waiting is bounded too — a caller that cannot get a slot within
``ERP_QUEUE_TIMEOUT`` gives up with a clear error instead of holding a worker
forever behind a queue that is not moving.

## Where the counting happens

With Redis, the count is **global**: a sorted set per target whose members are
leases, so N workers share one ceiling and the configured number means what it
says. Without Redis it falls back to the per-process semaphore below, where N
workers give N × the limit.

That fallback is deliberate and is *not* the fail-open policy the engine's query
limiter uses. Failing open here would mean unbounded concurrent queries against a
customer's production database at the moment our own cache is unhealthy — an
outage we cause at someone else's site. Degraded-but-bounded beats unbounded.

**Leases, not just a counter.** A worker that dies mid-query cannot release its
slot, and a leaked slot permanently shrinks the ceiling for that target — a slow,
hard-to-diagnose failure. So each holder's entry carries an expiry and the next
acquire prunes whatever has passed. The lease is derived from
``ERP_STATEMENT_TIMEOUT``, which is what guarantees the query cannot outlive it;
see :func:`lease_seconds` for what happens when that timeout is disabled.

**Admission is optimistic.** Acquire prunes expired leases, adds its own, and
counts — one transaction, so nothing interleaves between the count and the
decision. Over the limit, it takes its entry back out and retries. Two workers
racing for the last slot can both back out and both retry, which costs a retry
cycle and never over-admits. For a ceiling protecting someone else's database,
under-admission is the correct direction to be wrong in. (Admitting by *rank*
instead looks tempting and is unsound — see :meth:`_SharedSlots.acquire`.)
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from contextlib import contextmanager

from dbbuddy_core.logger import get_logger

logger = get_logger()

# Concurrent queries allowed against any one target database. Sized to leave a
# production ERP plenty of headroom: this is an analytics sidecar, not the
# system of record.
MAX_CONCURRENT_PER_TARGET = int(os.getenv("ERP_MAX_CONCURRENT_QUERIES", "10"))

# How long a caller waits for a slot before failing. Long enough to ride out a
# burst, short enough that a wedged target surfaces as an error rather than as a
# worker that never returns.
QUEUE_TIMEOUT_SECONDS = float(os.getenv("ERP_QUEUE_TIMEOUT", "20"))

# Read here rather than imported from db.py to keep this module free of the
# connection layer: it is consulted only to size a lease.
STATEMENT_TIMEOUT_SECONDS = float(os.getenv("ERP_STATEMENT_TIMEOUT", "60"))

# Lease length when there is no statement timeout to derive one from. A bound
# rather than a guarantee — see lease_seconds().
SLOT_LEASE_SECONDS = float(os.getenv("ERP_SLOT_LEASE_SECONDS", "300"))

# Margin between the statement timeout and the lease. The query must be over
# before its slot can be handed to someone else.
_LEASE_MARGIN_SECONDS = 30.0

# No lease shorter than this, however small the statement timeout: a holder still
# opening its connection must not have its slot reclaimed underneath it.
_LEASE_FLOOR_SECONDS = 60.0

# Redis key prefix for the shared count.
_SLOT_KEY_PREFIX = "dbbuddy:erp:slots:"

# How long to wait between admission attempts, plus jitter, so a crowd of
# contenders does not retry in lockstep.
_RETRY_BASE_SECONDS = 0.05


def lease_seconds() -> float:
    """How long a holder's claim on a slot stays valid.

    Derived from ``ERP_STATEMENT_TIMEOUT`` plus a margin, because that timeout is
    the thing that guarantees a query cannot still be running when its lease
    expires — which is what makes reclaiming an expired lease safe rather than a
    way to admit more work than the ceiling allows.

    With the statement timeout disabled (``0``) there is no such guarantee. The
    fallback is a fixed ``ERP_SLOT_LEASE_SECONDS``, and a query that outlives it
    *will* have its slot reclaimed while still executing. That is real
    over-admission, and it is the documented cost of turning off the statement
    timeout rather than something this module can paper over.
    """
    if STATEMENT_TIMEOUT_SECONDS > 0:
        return max(_LEASE_FLOOR_SECONDS, STATEMENT_TIMEOUT_SECONDS + _LEASE_MARGIN_SECONDS)
    return SLOT_LEASE_SECONDS


class ERPBusy(RuntimeError):
    """No slot became available for a target within the queue timeout.

    Distinct from a connection failure: the database may be perfectly healthy and
    simply busy with our own traffic. Callers should report it as "busy, try
    again", never as "this database is broken".
    """


_lock = threading.Lock()
_semaphores: dict[str, threading.BoundedSemaphore] = {}

# Targets this process has asked for a slot against. Needed because Redis deletes
# a sorted set the moment it becomes empty, so an idle target has no key to scan
# and would vanish from snapshot() rather than reporting zero — a metrics
# endpoint that shows nothing for a database it has been querying all day.
_seen_targets: set[str] = set()


def _queued_out(key: str):
    """Report a target at its ceiling. Always raises."""
    logger.warning("ERP target %s is at its concurrency limit (%d); request queued out.",
                   key, MAX_CONCURRENT_PER_TARGET)
    raise ERPBusy(
        f"This database is handling too many requests right now "
        f"(limit {MAX_CONCURRENT_PER_TARGET}). Try again in a moment."
    )


def target_key(engine: str | None, host: str | None, port: int | None,
               database: str | None) -> str:
    """Identity of a *physical* target database.

    Deliberately excludes the username: two analysts with separate credentials on
    the same server still contend for the same machine, and it is the machine we
    are protecting.
    """
    return f"{engine}|{host}|{port}|{database}".lower()


class _SharedSlots:
    """The concurrency ceiling counted in Redis, so it holds across workers.

    Takes a Redis-like client rather than reaching for one, so the admission
    algorithm can be exercised against a stub. That matters more than it sounds:
    the algorithm is the part that is easy to get wrong, and it is the part a
    live-server-only test would leave unchecked on any machine without Redis.
    """

    def __init__(self, client, limit: int, lease: float):
        self._client = client
        self._limit = limit
        self._lease = lease

    @staticmethod
    def _key(target: str) -> str:
        return f"{_SLOT_KEY_PREFIX}{target}"

    def acquire(self, target: str, token: str) -> bool:
        """One admission attempt. True if this token now holds a slot.

        Prune, admit optimistically, then count — all in one transaction, so no
        other worker can interleave between the count and the decision.

        **Counting, not ranking.** An earlier version asked for this member's
        *rank* and admitted anything inside the limit, on the theory that every
        contender reads the same ordering and so agrees on who won. That is wrong
        whenever two members share a score, and they routinely do: the score is an
        expiry computed from ``time.time()``, whose resolution on Windows is about
        15 ms, so a burst of acquirers lands on the same value. Redis then breaks
        the tie lexicographically — by a random token — and a late arrival that
        happens to sort first gets rank 0 and is admitted while the current
        holders, who never re-check, keep theirs. The ceiling is exceeded, silently.

        A count does not depend on ordering. Two workers racing for the last slot
        can now both back out and retry, which costs one retry cycle (~50 ms)
        against a 20-second queue timeout, and cannot over-admit.
        """
        key = self._key(target)
        now = time.time()
        pipe = self._client.pipeline(transaction=True)
        pipe.zremrangebyscore(key, 0, now)
        pipe.zadd(key, {token: now + self._lease})
        pipe.zcard(key)
        pipe.pexpire(key, int(self._lease * 1000))
        _, _, count, _ = pipe.execute()

        if count is not None and count <= self._limit:
            return True
        # Lost. Take the optimistic entry back out, or it holds a slot nobody is
        # using until its lease expires.
        self._client.zrem(key, token)
        return False

    def release(self, target: str, token: str) -> None:
        self._client.zrem(self._key(target), token)

    def occupancy(self, target: str) -> int:
        return int(self._client.zcard(self._key(target)) or 0)


def _shared_slots() -> "_SharedSlots | None":
    """The shared counter, or None when Redis is not usable right now.

    Consulted per acquire rather than once at import: a Redis that comes back
    should start counting globally again without a restart, and one that goes
    away should degrade without one.
    """
    from dbbuddy_core.context_store import _get_cache

    try:
        cache = _get_cache()
    except Exception:                           # noqa: BLE001 — never fail a query over this
        return None
    if cache is None or not cache._available() or cache.client is None:
        return None
    return _SharedSlots(cache.client, MAX_CONCURRENT_PER_TARGET, lease_seconds())


def backend_name() -> str:
    """Which ceiling is authoritative right now: 'redis' or 'in-process'."""
    return "redis" if _shared_slots() is not None else "in-process"


def _semaphore(key: str) -> threading.BoundedSemaphore:
    with _lock:
        sem = _semaphores.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(MAX_CONCURRENT_PER_TARGET)
            _semaphores[key] = sem
        return sem


@contextmanager
def query_slot(engine=None, host=None, port=None, database=None, *,
               timeout: float | None = None):
    """Hold one concurrency slot against a target database for the block's life.

    Raises :class:`ERPBusy` if no slot frees up within the timeout. Always
    releases, including on error — a leaked slot would permanently shrink the
    ceiling for that target, which is a slow, hard-to-diagnose failure.
    """
    key = target_key(engine, host, port, database)
    with _lock:
        _seen_targets.add(key)
    wait = QUEUE_TIMEOUT_SECONDS if timeout is None else timeout
    shared = _shared_slots()

    if shared is None:
        # No Redis: the per-process ceiling. Bounded at N × limit across N
        # workers, which is worse than the shared count and far better than none.
        sem = _semaphore(key)
        if not sem.acquire(timeout=wait):
            _queued_out(key)
        try:
            yield
        finally:
            sem.release()
        return

    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    deadline = time.monotonic() + wait
    admitted = False
    try:
        while True:
            try:
                admitted = shared.acquire(key, token)
            except Exception:                   # noqa: BLE001 — Redis went away mid-wait
                # Falling through to the local semaphore rather than failing the
                # query: the ceiling degrades, the request survives.
                logger.warning("Shared concurrency count unavailable; using the "
                               "per-process ceiling for %s.", key, exc_info=True)
                sem = _semaphore(key)
                remaining = max(0.0, deadline - time.monotonic())
                if not sem.acquire(timeout=remaining):
                    _queued_out(key)
                try:
                    yield
                finally:
                    sem.release()
                return

            if admitted or time.monotonic() >= deadline:
                break
            # Jittered, so a crowd of contenders does not retry in lockstep and
            # hand the slot to whoever happens to poll first every time.
            time.sleep(_RETRY_BASE_SECONDS * (1 + random.random()))

        if not admitted:
            _queued_out(key)
        yield
    finally:
        if admitted:
            try:
                shared.release(key, token)
            except Exception:                   # noqa: BLE001
                # The lease expires on its own; a failed release costs one slot
                # for one lease, not permanently.
                logger.warning("Could not release the shared slot for %s; its "
                               "lease will expire.", key, exc_info=True)


def snapshot() -> dict[str, dict]:
    """Per-target occupancy, for diagnostics.

    Reports whichever ceiling is authoritative. With Redis that is the shared
    count — reporting only this process's semaphores there would show an empty
    picture on a system that is actually saturated, which is worse than no
    metric.

    ``BoundedSemaphore`` exposes its counter only as a private attribute; read it
    defensively so a CPython change degrades this to "unknown" rather than
    breaking a metrics endpoint.
    """
    out: dict[str, dict] = {}

    shared = _shared_slots()
    if shared is not None:
        try:
            with _lock:
                targets = set(_seen_targets)
            # Plus anything another worker is holding right now, which this
            # process may never have queried itself.
            for raw in shared._client.scan_iter(match=f"{_SLOT_KEY_PREFIX}*"):
                name = raw.decode() if isinstance(raw, bytes) else str(raw)
                targets.add(name[len(_SLOT_KEY_PREFIX):])
            for target in targets:
                out[target] = {
                    "limit": MAX_CONCURRENT_PER_TARGET,
                    "in_use": shared.occupancy(target),
                    "backend": "redis",
                }
            return out
        except Exception:                       # noqa: BLE001 — diagnostics never raise
            logger.debug("Could not read shared occupancy; reporting local.", exc_info=True)

    with _lock:
        for key, sem in _semaphores.items():
            available = getattr(sem, "_value", None)
            out[key] = {
                "limit": MAX_CONCURRENT_PER_TARGET,
                "in_use": (MAX_CONCURRENT_PER_TARGET - available) if available is not None else None,
                "backend": "in-process",
            }
    return out


def reset() -> None:
    """Drop all slot state, local and shared (tests).

    The shared half matters: without it one test's leftover lease is another
    test's mysteriously missing slot, and the failure surfaces somewhere else
    entirely.
    """
    with _lock:
        _semaphores.clear()
        _seen_targets.clear()

    shared = _shared_slots()
    if shared is None:
        return
    try:
        keys = list(shared._client.scan_iter(match=f"{_SLOT_KEY_PREFIX}*"))
        if keys:
            shared._client.delete(*keys)
    except Exception:                           # noqa: BLE001
        logger.debug("Could not clear shared slot keys.", exc_info=True)
