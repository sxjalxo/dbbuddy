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

**Scope, honestly:** these semaphores are per *process*, like the rest of the
runtime's shared state. With N worker processes the effective ceiling is N × the
configured limit. That is still a bound where there was none, but a true global
limit needs shared state (Redis) — the same prerequisite as the other
process-global items in ``docs/PRE_DEPLOYMENT_REVIEW.md``. Set the per-process
limit to `desired_total / worker_count` until then.
"""

from __future__ import annotations

import os
import threading
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


class ERPBusy(RuntimeError):
    """No slot became available for a target within the queue timeout.

    Distinct from a connection failure: the database may be perfectly healthy and
    simply busy with our own traffic. Callers should report it as "busy, try
    again", never as "this database is broken".
    """


_lock = threading.Lock()
_semaphores: dict[str, threading.BoundedSemaphore] = {}


def target_key(engine: str | None, host: str | None, port: int | None,
               database: str | None) -> str:
    """Identity of a *physical* target database.

    Deliberately excludes the username: two analysts with separate credentials on
    the same server still contend for the same machine, and it is the machine we
    are protecting.
    """
    return f"{engine}|{host}|{port}|{database}".lower()


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
    sem = _semaphore(key)
    wait = QUEUE_TIMEOUT_SECONDS if timeout is None else timeout

    if not sem.acquire(timeout=wait):
        logger.warning("ERP target %s is at its concurrency limit (%d); request queued out.",
                       key, MAX_CONCURRENT_PER_TARGET)
        raise ERPBusy(
            f"This database is handling too many requests right now "
            f"(limit {MAX_CONCURRENT_PER_TARGET}). Try again in a moment."
        )
    try:
        yield
    finally:
        sem.release()


def snapshot() -> dict[str, dict]:
    """Per-target occupancy, for diagnostics.

    ``BoundedSemaphore`` exposes its counter only as a private attribute; read it
    defensively so a CPython change degrades this to "unknown" rather than
    breaking a metrics endpoint.
    """
    with _lock:
        out = {}
        for key, sem in _semaphores.items():
            available = getattr(sem, "_value", None)
            out[key] = {
                "limit": MAX_CONCURRENT_PER_TARGET,
                "in_use": (MAX_CONCURRENT_PER_TARGET - available) if available is not None else None,
            }
        return out


def reset() -> None:
    """Drop all semaphores (tests)."""
    with _lock:
        _semaphores.clear()
