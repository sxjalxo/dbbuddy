"""In-process metrics for AI provider calls — provider-independent observability.

Every labeling call through the provider chain records here: per-provider call
counts, success/failover tallies, retry attempts, and latency (last + rolling
average). It's a lightweight, thread-safe, process-global accumulator (same shape
as the login throttle / context-store metrics) exposed through the backend so an
operator can see which provider is slow, flaky, or being rate-limited.

Keyed by the provider's display *name* (what the engine knows about a runtime
config). In a multi-org deployment two orgs that reuse the same provider name
share a bucket — acceptable for observability; not a security boundary.
"""

import threading
import time

_lock = threading.Lock()
_stats: dict[str, dict] = {}


def _bucket(name: str, adapter: str) -> dict:
    b = _stats.get(name)
    if b is None:
        b = {
            "name": name,
            "adapter": adapter,
            "calls": 0,          # provider invocations (a chain of N tries = N calls)
            "successes": 0,
            "failovers": 0,      # gave up on this provider → next in chain / rule-based
            "retries": 0,        # extra attempts beyond the first, summed
            "rate_limited": 0,   # calls that hit at least one 429
            "skipped": 0,        # short-circuited (circuit breaker open) — no call made
            "total_latency_ms": 0.0,
            "last_latency_ms": 0.0,
            "last_outcome": None,
            "last_ts": None,
        }
        _stats[name] = b
    b["adapter"] = adapter  # keep fresh if a record's adapter changed
    return b


def record(
    name: str,
    adapter: str,
    *,
    latency_ms: float,
    outcome: str,          # "success" | "failover" | "skipped"
    attempts: int = 1,
    rate_limited: bool = False,
) -> None:
    """Record one provider outcome. ``skipped`` means the circuit breaker was open
    and no request was made — it does not count as a call and carries no latency."""
    with _lock:
        b = _bucket(name or adapter or "unknown", adapter)
        b["last_outcome"] = outcome
        b["last_ts"] = time.time()
        if outcome == "skipped":
            b["skipped"] += 1
            return
        b["calls"] += 1
        b["retries"] += max(0, attempts - 1)
        b["total_latency_ms"] += latency_ms
        b["last_latency_ms"] = round(latency_ms, 1)
        if outcome == "success":
            b["successes"] += 1
        else:
            b["failovers"] += 1
        if rate_limited:
            b["rate_limited"] += 1


def snapshot() -> list[dict]:
    """Per-provider aggregates, most-recently-used first."""
    with _lock:
        out = []
        for b in _stats.values():
            calls = b["calls"] or 1
            out.append({
                "name": b["name"],
                "adapter": b["adapter"],
                "calls": b["calls"],
                "successes": b["successes"],
                "failovers": b["failovers"],
                "retries": b["retries"],
                "rate_limited": b["rate_limited"],
                "skipped": b["skipped"],
                "avg_latency_ms": round(b["total_latency_ms"] / calls, 1),
                "last_latency_ms": b["last_latency_ms"],
                "last_outcome": b["last_outcome"],
                "last_ts": b["last_ts"],
            })
        out.sort(key=lambda r: r["last_ts"] or 0, reverse=True)
        return out


def reset() -> None:
    """Clear all recorded metrics (test isolation / manual reset)."""
    with _lock:
        _stats.clear()
