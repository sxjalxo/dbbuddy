"""Provider-independent resilience for the AI labeling path.

Covers the retry/backoff + rate-limit handling in `classify_columns` and the
per-provider metrics recorded to `dbbuddy_core.ai_metrics`:

  * transient failures retry the SAME provider (with backoff) before failing over;
  * a non-transient RecoverableProviderError fails over immediately (no retry);
  * an exhausted transient chain degrades to rule-based labeling;
  * a large `Retry-After` fails over instead of blocking;
  * metrics capture calls / successes / failovers / retries / rate-limit hits.

`time.sleep` is patched to a no-op so backoff never slows the suite.
"""

import pathlib
import sys
from unittest.mock import patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import dbbuddy_core.ai_metrics as m  # noqa: E402
import dbbuddy_core.ai_providers as aip  # noqa: E402


def _cfg(name, adapter="ollama"):
    return aip.ProviderRuntimeConfig(adapter=adapter, model="x", name=name)


class _Scripted:
    """A fake provider whose generate() plays back a scripted list of behaviors.

    Each item is either an Exception (raised) or a string (returned). Runs the
    last item repeatedly once the script is exhausted.
    """

    def __init__(self, cfg, script):
        self.config = cfg
        self.adapter = cfg.adapter
        self._script = list(script)
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _run(chain, providers_by_name, policy=None):
    """classify_columns with get_provider resolving to the given fakes."""
    def _get(cfg):
        return providers_by_name[cfg.name]

    with patch("time.sleep"), patch.object(aip, "get_provider", _get):
        return aip.classify_columns(["users.id"], {"users": ["id"]}, chain, policy=policy)


@pytest.fixture(autouse=True)
def _reset_metrics():
    m.reset()
    yield
    m.reset()


def test_transient_then_success_retries_same_provider():
    cfg = _cfg("P")
    p = _Scripted(cfg, [aip.TransientProviderError("t1"), aip.TransientProviderError("t2"), '{"users.id": "identifier"}'])
    out = _run([cfg], {"P": p})
    assert out["users.id"]["term"] == "identifier" and out["users.id"]["provider"] == "P"
    assert p.calls == 3  # retried twice on the same provider
    snap = {s["name"]: s for s in m.snapshot()}
    assert snap["P"]["successes"] == 1 and snap["P"]["retries"] == 2 and snap["P"]["failovers"] == 0


def test_non_transient_fails_over_immediately():
    a, b = _cfg("A"), _cfg("B")
    pa = _Scripted(a, [aip.RecoverableProviderError("no key")])   # non-transient → no retry
    pb = _Scripted(b, ['{"users.id": "identifier"}'])
    out = _run([a, b], {"A": pa, "B": pb})
    assert out["users.id"]["provider"] == "B"
    assert pa.calls == 1  # tried once, did NOT retry
    snap = {s["name"]: s for s in m.snapshot()}
    assert snap["A"]["failovers"] == 1 and snap["A"]["retries"] == 0
    assert snap["B"]["successes"] == 1


def test_transient_exhausted_then_fallback_provider():
    a, b = _cfg("A"), _cfg("B")
    pa = _Scripted(a, [aip.TransientProviderError("always")])  # never recovers
    pb = _Scripted(b, ['{"users.id": "identifier"}'])
    out = _run([a, b], {"A": pa, "B": pb})
    assert out["users.id"]["provider"] == "B"
    assert pa.calls == aip.DEFAULT_RETRY_POLICY.max_attempts  # exhausted retries
    snap = {s["name"]: s for s in m.snapshot()}
    assert snap["A"]["failovers"] == 1 and snap["A"]["retries"] == aip.DEFAULT_RETRY_POLICY.max_attempts - 1


def test_whole_chain_exhausted_degrades_to_rule_based():
    a = _cfg("A")
    pa = _Scripted(a, [aip.TransientProviderError("down")])
    out = _run([a], {"A": pa})
    assert out["users.id"]["provider"] is None  # rule-based fallback


def test_large_retry_after_fails_over_without_blocking():
    a, b = _cfg("A", "openai_compatible"), _cfg("B")
    # Retry-After beyond the cap → don't wait, fail straight over.
    pa = _Scripted(a, [aip.TransientProviderError("429", retry_after=9999, rate_limited=True)])
    pb = _Scripted(b, ['{"users.id": "identifier"}'])
    out = _run([a, b], {"A": pa, "B": pb})
    assert out["users.id"]["provider"] == "B"
    assert pa.calls == 1  # did not retry (Retry-After too large)
    snap = {s["name"]: s for s in m.snapshot()}
    assert snap["A"]["rate_limited"] == 1 and snap["A"]["failovers"] == 1


def test_retry_delay_honors_and_caps_retry_after():
    policy = aip.RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=8.0, retry_after_cap=8.0)
    # Within cap → honored.
    assert aip._retry_delay(aip.TransientProviderError("x", retry_after=3), 1, policy) == 3
    # Beyond the cap → None (fail over instead of blocking).
    assert aip._retry_delay(aip.TransientProviderError("x", retry_after=100), 1, policy) is None
    # No server hint → exponential backoff with full jitter within [0, backoff].
    d = aip._retry_delay(aip.TransientProviderError("x"), 2, policy)
    assert 0 <= d <= 1.0  # base_delay * 2^(2-1) = 1.0


def test_metrics_snapshot_shape():
    cfg = _cfg("Solo")
    out = _run([cfg], {"Solo": _Scripted(cfg, ['{"users.id": "identifier"}'])})
    assert out["users.id"]["provider"] == "Solo"
    row = m.snapshot()[0]
    assert row["name"] == "Solo" and row["calls"] == 1 and row["avg_latency_ms"] >= 0
    assert set(row) >= {"name", "adapter", "calls", "successes", "failovers", "retries",
                        "rate_limited", "skipped", "avg_latency_ms", "last_latency_ms",
                        "last_outcome", "last_ts"}


# ── Circuit breaker (deterministic fake clock) ────────────────────────────────

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _breaker(threshold=3, base=60, mx=300):
    return aip.CircuitBreaker(threshold=threshold, base_cooldown=base, max_cooldown=mx, clock=_Clock())


def test_breaker_trips_after_threshold_transient_failures():
    b = _breaker(threshold=3)
    assert b.allow("P")
    for _ in range(3):
        b.record_failure("P", transient=True)
    assert b.allow("P") is False  # tripped OPEN
    assert b.snapshot()["P"]["state"] == "open"


def test_breaker_non_transient_does_not_trip():
    b = _breaker(threshold=3)
    for _ in range(10):
        b.record_failure("P", transient=False)
    assert b.allow("P") is True
    assert b.snapshot()["P"]["state"] == "closed"


def test_breaker_success_resets_counter():
    b = _breaker(threshold=3)
    b.record_failure("P", transient=True)
    b.record_failure("P", transient=True)
    b.record_success("P")           # reset
    b.record_failure("P", transient=True)
    assert b.allow("P") is True     # only 1 failure since reset → still closed


def test_breaker_half_open_probe_closes_on_success():
    clock = _Clock()
    b = aip.CircuitBreaker(threshold=2, base_cooldown=60, max_cooldown=300, clock=clock)
    b.record_failure("P", transient=True)
    b.record_failure("P", transient=True)  # OPEN, cooldown 60s
    assert b.allow("P") is False
    clock.advance(61)
    assert b.allow("P") is True            # → HALF_OPEN probe granted
    assert b.snapshot()["P"]["state"] == "half_open"
    b.record_success("P")
    assert b.snapshot()["P"]["state"] == "closed" and b.allow("P") is True


def test_breaker_half_open_probe_failure_reopens_with_backoff():
    clock = _Clock()
    b = aip.CircuitBreaker(threshold=1, base_cooldown=60, max_cooldown=300, clock=clock)
    b.record_failure("P", transient=True)  # opens #1 → cooldown 60
    clock.advance(61)
    assert b.allow("P")                    # HALF_OPEN probe
    b.record_failure("P", transient=True)  # probe fails → reopen #2 → cooldown 120
    assert b.allow("P") is False
    clock.advance(61)
    assert b.allow("P") is False           # still cooling (needs 120s)
    clock.advance(60)
    assert b.allow("P") is True            # after 120s → probe again


def test_open_provider_short_circuits_to_fallback(monkeypatch):
    """End-to-end: once a provider trips, classify_columns skips it (no call) and
    uses the fallback, recording a 'skipped' metric."""
    clock = _Clock()
    monkeypatch.setattr(aip, "_breaker", aip.CircuitBreaker(threshold=2, base_cooldown=60, clock=clock))
    a, b = _cfg("A"), _cfg("B")
    pa = _Scripted(a, [aip.TransientProviderError("down")])   # always transient-fails
    pb = _Scripted(b, ['{"users.id": "identifier"}'])
    providers = {"A": pa, "B": pb}

    # Two failing requests trip A's breaker (one failover recorded per request).
    for _ in range(2):
        out = _run([a, b], providers)
        assert out["users.id"]["provider"] == "B"
    calls_before = pa.calls
    m.reset()

    # Third request: A is OPEN → skipped without a call; fallback B answers.
    out = _run([a, b], providers)
    assert out["users.id"]["provider"] == "B"
    assert pa.calls == calls_before  # A was NOT called
    snap = {s["name"]: s for s in m.snapshot()}
    assert snap["A"]["skipped"] == 1 and snap["A"]["calls"] == 0
