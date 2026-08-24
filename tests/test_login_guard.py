"""Login/registration throttle: shared across workers, degrading closed.

Same bootstrap contract as the other platform suites — ``backend/`` must be
importable and the app pointed at an isolated SQLite file before ``app_db`` is
imported.
"""

import importlib
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_loginguard_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "loginguard-test-secret-key-long-enough-1")
os.environ.setdefault("APP_SECRET_KEY", "loginguard-test-app-secret")

login_guard = importlib.import_module("app_db.login_guard")


@pytest.fixture(autouse=True)
def _clean():
    login_guard.clear_all()
    yield
    login_guard.clear_all()


def _redis_available() -> bool:
    return login_guard._cache() is not None


requires_redis = pytest.mark.skipif(
    not _redis_available(), reason="needs a running Redis for the shared window")


# ── Local window (the always-present floor) ──────────────────────────────────

class TestLocalWindow:
    def test_lockout_after_the_cap(self):
        key = "ip::local@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            assert login_guard.retry_after(key) is None
            login_guard.record_failure(key)
        locked = login_guard.retry_after(key)
        assert locked is not None and locked > 0

    def test_a_successful_auth_clears_the_key(self):
        key = "ip::reset@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure(key)
        assert login_guard.retry_after(key) is not None
        login_guard.reset(key)
        assert login_guard.retry_after(key) is None

    def test_keys_are_independent(self):
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure("ip::a@example.com")
        assert login_guard.retry_after("ip::a@example.com") is not None
        assert login_guard.retry_after("ip::b@example.com") is None


# ── Shared window: the point of the exercise ─────────────────────────────────

@requires_redis
class TestSharedWindow:
    def test_the_shared_window_is_authoritative_across_workers(self):
        """Another worker's failures must lock this one out.

        Simulated by recording attempts (which write to Redis), then wiping *only*
        the local deque — leaving exactly the state a second worker process would
        have: no local history, but a populated shared window. Before this change
        that worker would have allowed a full fresh set of attempts, which is how
        `--workers 4` quietly multiplied the brute-force budget by four.
        """
        key = "ip::shared@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure(key)

        with login_guard._lock:
            login_guard._failures.clear()  # this "worker" knows nothing locally

        locked = login_guard.retry_after(key)
        assert locked is not None, "a fresh worker ignored the shared lockout"
        assert 0 < locked <= login_guard.WINDOW_SECONDS + 1

    def test_reset_clears_the_shared_window_too(self):
        key = "ip::sharedreset@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure(key)
        login_guard.reset(key)
        with login_guard._lock:
            login_guard._failures.clear()
        assert login_guard.retry_after(key) is None

    def test_attempts_in_the_same_tick_all_count(self):
        # Scores alone would collide for failures within one clock tick; the
        # member carries a uuid so each attempt is a distinct set entry.
        key = "ip::tick@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure(key)
        cache = login_guard._cache()
        assert cache.client.zcard(login_guard._redis_key(key)) == login_guard.MAX_FAILURES

    def test_clear_all_wipes_the_shared_window(self):
        key = "ip::wipe@example.com"
        for _ in range(login_guard.MAX_FAILURES):
            login_guard.record_failure(key)
        login_guard.clear_all()
        assert login_guard.retry_after(key) is None


# ── Degradation: closed, never open ──────────────────────────────────────────

class _BrokenCache:
    """A cache that is 'available' but fails every command."""

    connected = True

    def __init__(self):
        self.client = self
        self.errors = 0

    def _available(self):
        return True

    def _note_success(self):
        pass

    def _note_error(self):
        self.errors += 1

    def pipeline(self):
        raise ConnectionError("redis is gone")

    def delete(self, *a):
        raise ConnectionError("redis is gone")

    def clear_prefix(self, *a):
        raise ConnectionError("redis is gone")


class TestDegradesClosed:
    def test_a_redis_outage_does_not_lift_the_limit(self):
        """The whole reason this limiter does not fail open.

        If it did, a Redis outage would hand every attacker unlimited password
        attempts — a security control vanishing exactly when the system is
        already degraded.
        """
        key = "ip::outage@example.com"
        broken = _BrokenCache()
        with patch.object(login_guard, "_cache", lambda: broken):
            for _ in range(login_guard.MAX_FAILURES):
                login_guard.record_failure(key)
            assert login_guard.retry_after(key) is not None
        assert broken.errors > 0, "the failure path was never exercised"

    def test_the_local_cap_shrinks_with_the_worker_count(self, monkeypatch):
        """Degrading gets *stricter*, not laxer.

        With no shared view each worker sees only its own traffic, so the local
        cap is divided by the worker count: N workers each allowing cap/N stays
        near the intended global limit instead of N times it.
        """
        monkeypatch.setattr(login_guard, "WORKER_COUNT", 5)
        key = "ip::scaled@example.com"
        with patch.object(login_guard, "_cache", lambda: None):
            # MAX_FAILURES=5 over 5 workers → this process allows 1.
            assert login_guard._local_cap(login_guard.MAX_FAILURES) == 1
            login_guard.record_failure(key)
            assert login_guard.retry_after(key) is not None

    def test_the_cap_never_reaches_zero(self, monkeypatch):
        # A large worker count must not lock everyone out before their first try.
        monkeypatch.setattr(login_guard, "WORKER_COUNT", 1000)
        assert login_guard._local_cap(login_guard.MAX_FAILURES) == 1
        with patch.object(login_guard, "_cache", lambda: None):
            assert login_guard.retry_after("ip::fresh@example.com") is None

    def test_single_worker_default_is_unchanged(self):
        assert login_guard.WORKER_COUNT == 1
        assert login_guard._local_cap(login_guard.MAX_FAILURES) == login_guard.MAX_FAILURES
        assert login_guard._local_cap(login_guard.MAX_REGISTRATIONS) == login_guard.MAX_REGISTRATIONS

    def test_registration_uses_its_own_cap(self):
        key = "register::1.2.3.4"
        with patch.object(login_guard, "_cache", lambda: None):
            for _ in range(login_guard.MAX_FAILURES + 1):
                login_guard.record_failure(key)
            # Above the login cap, still below the (more lenient) registration one.
            assert login_guard.retry_after(key, max_attempts=login_guard.MAX_REGISTRATIONS) is None
            for _ in range(login_guard.MAX_REGISTRATIONS):
                login_guard.record_failure(key)
            assert login_guard.retry_after(key, max_attempts=login_guard.MAX_REGISTRATIONS) is not None
