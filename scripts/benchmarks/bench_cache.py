"""Benchmark: the Redis cache client's cost, present or absent.

Caching here is optional, so the interesting numbers are the *failure* ones. A
cache that is merely slow costs a few milliseconds; a cache that is unreachable
but not recognized as such costs a socket timeout per call, twice per query,
forever. `dead_redis_call` is that metric — it must stay near zero.

Runs against a stub client rather than a real server, so it measures the client's
own logic and produces the same numbers on a machine with no Redis installed.
"""

import argparse

import os
import sys

# Allow direct execution as well as import from the suite runner: the repo root
# must be importable before the shared harness is.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.benchmarks import _harness as h  # noqa: E402

h.bootstrap()


class _StubError(Exception):
    """Stands in for redis.RedisError."""


class _SlowDeadClient:
    """A client that behaves like a server which stopped answering.

    ``delay_ms`` mimics the socket timeout: the real cost of talking to a Redis
    that is gone is not the error, it is the wait before it.
    """

    def __init__(self, delay_ms: float):
        self._delay = delay_ms / 1000.0
        self.calls = 0

    def _fail(self, *_a, **_k):
        import time
        self.calls += 1
        time.sleep(self._delay)
        raise _StubError("connection reset")

    get = _fail
    set = _fail
    delete = _fail


class _LiveClient:
    """An in-memory stand-in for a healthy server."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def _cache_with(client, cache_module):
    from dbbuddy_core.cache import Cache
    c = Cache.__new__(Cache)
    c.client = client
    c.connected = True
    c._consecutive_errors = 0
    c._blocked_until = 0.0
    return c


def run(iterations: int = 200, timeout_ms: float = 20.0) -> h.BenchResult:
    import dbbuddy_core.cache as cache_module
    from unittest.mock import MagicMock

    result = h.BenchResult("cache")

    fake_redis = MagicMock()
    fake_redis.RedisError = _StubError
    original = cache_module.redis
    cache_module.redis = fake_redis
    try:
        # A representative payload: a query response, not a toy string.
        payload = {"sql": "SELECT * FROM customers WHERE country = 'India'",
                   "query_type": "select", "confidence": "high",
                   "semantic_layer": {"customers": {"country": {"term": "country",
                                                                "source": "rule"}}},
                   "results": [{"id": i, "name": f"Customer {i}"} for i in range(50)]}

        live = _cache_with(_LiveClient(), cache_module)
        result.add("healthy_set_p50",
                   h.percentiles(h.repeat_ms(
                       lambda: live.set("query", "show customers", payload), iterations))["p50"])
        live.set("query", "show customers", payload)
        result.add("healthy_get_p50",
                   h.percentiles(h.repeat_ms(
                       lambda: live.get("query", "show customers"), iterations))["p50"])

        # The scenario that matters. `timeout_ms` stands in for the socket
        # timeout; the gate should absorb it after a few failures so the mean
        # cost per call collapses toward zero regardless of how large it is.
        dead_client = _SlowDeadClient(timeout_ms)
        dead = _cache_with(dead_client, cache_module)
        samples = h.repeat_ms(lambda: dead.get("query", "show customers"), iterations)
        result.add("dead_redis_call_mean", sum(samples) / len(samples))
        result.add("dead_redis_call_p95", h.percentiles(samples)["p95"])
        # The structural version of the same fact, and the one that cannot be
        # explained away by a fast machine: a working gate lets exactly
        # ERROR_THRESHOLD calls through, whatever the timeout is.
        result.add("dead_redis_client_calls", float(dead_client.calls), unit="calls")
        result.notes.append(
            f"{dead_client.calls} of {iterations} lookups reached the socket "
            f"(threshold {cache_module.ERROR_THRESHOLD})")

        # An input, not a measurement — it is whatever --timeout-ms was set to.
        # Recorded so the numbers above are readable without this file: it is the
        # per-call cost the gate avoids.
        result.add("dead_redis_ungated_equivalent", timeout_ms, kind=h.MetricKind.DIAGNOSTIC)
    finally:
        cache_module.redis = original

    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--timeout-ms", type=float, default=20.0,
                    help="stand-in for REDIS_SOCKET_TIMEOUT (kept small so the run is quick)")
    a = ap.parse_args()
    raise SystemExit(h.standalone(run, iterations=a.iterations, timeout_ms=a.timeout_ms))
