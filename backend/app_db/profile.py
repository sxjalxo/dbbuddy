"""What this deployment shares between workers, and what it does not.

Several coordination structures in this runtime live in one process. Which of
them are actually shared depends on two things the process cannot infer: how many
workers there are, and whether Redis is reachable. Get that combination wrong and
nothing errors — a customer's ERP takes N times the intended concurrent load, four
schedulers fire the same job four times, and each worker answers from whichever
schema it happened to prepare. All of it looks like ordinary behaviour.

So the profile is resolved once at startup and stated plainly in the log.

**Never refuses to boot.** A container that will not start because a cache is down
turns a Redis blip during a rolling restart into a total outage. It starts, and it
says what it cannot guarantee.

**The worker count has to be declared.** A uvicorn worker cannot see its siblings;
nothing in the process knows how many others exist. ``LOGIN_GUARD_WORKERS`` already
existed for exactly this reason — the degraded login-throttle maths divides by it —
and is kept as an alias so existing env files keep working.
"""

from __future__ import annotations

import os

from dbbuddy_core.logger import get_logger

logger = get_logger()

# The new name. LOGIN_GUARD_WORKERS remains an alias: it shipped first, it is in
# existing deployments, and it means the same thing.
_WORKER_ENV = "DBBUDDY_WORKERS"
_WORKER_ENV_LEGACY = "LOGIN_GUARD_WORKERS"


def worker_count() -> int:
    """How many worker processes the operator says are running. At least 1.

    A malformed value resolves to 1 rather than failing: refusing to start over an
    env var typo is the hard-fail this module exists to avoid, and guessing a
    large number would under-size every per-process limit derived from it.
    """
    raw = os.getenv(_WORKER_ENV) or os.getenv(_WORKER_ENV_LEGACY) or "1"
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; assuming a single worker.", _WORKER_ENV, raw)
        return 1


def _redis_available() -> bool:
    """Whether the shared store can be used right now."""
    from dbbuddy_core.context_store import _get_cache

    cache = _get_cache()
    return bool(cache is not None and cache._available())


def resolve() -> dict:
    """The current profile: worker count, shared store, and per subsystem.

    ``degraded`` means *this configuration cannot keep its promises*: more than one
    worker, and no shared store to coordinate them. A single worker is never
    degraded — with one process there is nothing to coordinate, so no amount of
    missing Redis makes it wrong.
    """
    from dbbuddy_core.erp_concurrency import MAX_CONCURRENT_PER_TARGET

    workers = worker_count()
    shared = _redis_available()
    multi = workers > 1

    subsystems = {
        # Counted in Redis when available, otherwise per process.
        "erp_concurrency": "shared" if shared else "per-worker",
        "session_revocation": "broadcast" if shared else "per-worker (TTL convergence)",
        # Never "shared", and that is not a gap to be closed later: a prepared
        # context holds a live connection pool and a Chroma client handle, and
        # neither survives serialization. Only the invalidation crosses workers.
        "prepared_contexts": (
            "per-worker (invalidation broadcast)" if shared else "per-worker"
        ),
        "login_throttle": "shared" if shared else "per-worker (divided cap)",
        # Not a Redis question at all: one worker must own it, by configuration.
        "scheduler": (
            "single-owner (set DBBUDDY_DISABLE_SCHEDULER=1 on extra workers)"
            if multi else "single-owner"
        ),
    }

    return {
        "workers": workers,
        "shared_store": "redis" if shared else "none",
        "degraded": multi and not shared,
        "erp_limit": MAX_CONCURRENT_PER_TARGET,
        # What a customer's database actually sees. Without a shared count the
        # configured limit applies once per worker.
        "effective_erp_limit": (
            MAX_CONCURRENT_PER_TARGET if shared else MAX_CONCURRENT_PER_TARGET * workers
        ),
        "subsystems": subsystems,
    }


def log_profile() -> None:
    """State the profile at startup. Never raises.

    Runs inside the lifespan startup, so a report that can fail a boot is a worse
    outcome than one that is missing.
    """
    try:
        resolved = resolve()
    except Exception:                           # noqa: BLE001
        logger.debug("could not resolve the worker profile", exc_info=True)
        return

    summary = ", ".join(f"{name}={state}" for name, state in resolved["subsystems"].items())
    logger.info("worker profile: workers=%d shared_store=%s effective ERP ceiling=%d — %s",
                resolved["workers"], resolved["shared_store"],
                resolved["effective_erp_limit"], summary)

    if resolved["degraded"]:
        logger.warning(
            "Running %d workers with no shared store. Each worker keeps its own "
            "ERP concurrency ceiling (a target database sees up to %d concurrent "
            "queries, not %d), its own prepared schema contexts, and its own "
            "session-revocation cache. Start Redis, or run a single worker.",
            resolved["workers"], resolved["effective_erp_limit"], resolved["erp_limit"],
        )
