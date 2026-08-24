"""Per-endpoint HTTP budgets for the requests that cost something to serve.

Three limiters now exist, and they are not redundant:

* ``login_guard`` — failed sign-ins. Counts attempts against a credential.
* ``dbbuddy_core.rate_limiter`` — query *cost*, per authenticated user, inside the
  engine.
* **this one** — how often a caller may reach an expensive endpoint at all,
  before any of the work starts.

The gap it closes: ``/analyze`` walks an entire schema and was unmetered,
``/auth/refresh`` was unmetered, and ``/ai-providers/{id}/test`` makes this server
issue an outbound request at a caller's direction.

## The failure mode is per budget, on purpose

``fail_open=True`` for throughput budgets. If the shared window is unreachable,
``/analyze`` and ``/query`` still work: the cost of not limiting is a warm
database, and the cost of refusing is an outage caused by a cache being down.
Same reasoning as the engine's query limiter.

``fail_open=False`` for ``/auth/refresh``, which mints sessions. That follows the
``login_guard`` rule instead — a control protecting authentication must not vanish
exactly when the system is already degraded, so it falls back to the per-process
window (stricter, never absent).

A limiter with one policy for everything would be wrong for half of what it
covers.

## Keying

By user when the request carries a verifiable token, else by client IP. Two
analysts behind one NAT should not share a budget; equally, an *unverifiable*
token falls back to the IP rather than trusting its claims — otherwise anyone
could mint ``sub: whatever`` locally and get a fresh budget per request, and the
limiter would be self-service.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from .login_guard import WINDOW_SECONDS, record_failure, retry_after
from .security import decode_token

# Budgets are per WINDOW_SECONDS (15 minutes), the window login_guard already
# maintains. Reusing it keeps one sliding-window implementation in the codebase
# rather than two that drift.
#
# Sizes are deliberately generous: this is an abuse ceiling, not a quota. A number
# an ordinary session can reach is a support ticket, and the first thing anyone
# does about a limiter that fires on normal use is disable it.
ANALYZE_BUDGET = 20        # a full schema walk; a person does this rarely
QUERY_BUDGET = 300         # a busy analyst session, with room to spare
PROVIDER_TEST_BUDGET = 30  # each one is an outbound request from this server
REFRESH_BUDGET = 60        # ~1 per 15 s; a 15-minute access token needs far fewer


def shared_window_available() -> bool:
    """Whether the Redis-backed window is usable right now.

    Its own function so the fail-open path has something to consult, and so tests
    can simulate an outage without a Redis to break.
    """
    from .login_guard import backend_name

    return backend_name() == "redis"


def budget_key(budget: str, authorization: str | None, client_ip: str | None) -> str:
    """The window key for this caller and this endpoint.

    Budgets are per endpoint as well as per caller: exhausting the analyze budget
    must not also stop the caller running queries, because they are different
    costs with different limits.
    """
    subject = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            # Verifies the signature. An unverifiable token is treated as absent,
            # not as whoever it claims to be.
            subject = decode_token(token).get("sub")
        except Exception:                          # noqa: BLE001
            subject = None

    who = f"user:{subject}" if subject else f"ip:{client_ip or '-'}"
    return f"rl::{budget}::{who}"


def make_check(budget: str, max_requests: int, fail_open: bool):
    """Build the counting function for one budget.

    Returns a callable that takes the caller's identity and returns ``None`` when
    the request may proceed, or the number of seconds to wait when it may not.
    Separated from the FastAPI dependency so the policy is testable without an
    HTTP layer.
    """
    def check(authorization: str | None, client_ip: str | None) -> int | None:
        if fail_open and not shared_window_available():
            # No shared view: allow. See the module docstring — for a throughput
            # budget, refusing would convert a cache outage into a service outage.
            return None

        key = budget_key(budget, authorization, client_ip)
        locked = retry_after(key, max_attempts=max_requests)
        if locked:
            return locked
        # Counted after the check so the Nth request is served and the N+1th is
        # not, rather than the other way round.
        record_failure(key)
        return None

    return check


def rate_limit(budget: str, max_requests: int, fail_open: bool = True):
    """A FastAPI dependency enforcing one budget.

    Usage::

        @router.post("/analyze", dependencies=[Depends(rate_limit("analyze", 20))])
    """
    check = make_check(budget, max_requests, fail_open)

    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else None
        wait = check(request.headers.get("authorization"), client_ip)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Try again shortly.",
                headers={"Retry-After": str(wait)},
            )

    return dependency


__all__ = [
    "ANALYZE_BUDGET", "PROVIDER_TEST_BUDGET", "QUERY_BUDGET", "REFRESH_BUDGET",
    "WINDOW_SECONDS", "budget_key", "make_check", "rate_limit",
    "shared_window_available",
]
