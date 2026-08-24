"""Revoking a session must reach every worker, not just the one that did it.

`invalidate_revocation()` dropped the entry from the *calling* process's cache.
With N uvicorn workers that left N−1 still serving the old
`(token_version, is_active)` until their own TTL expired — so a logout, a
deactivation or a password reset kept working for up to
`AUTH_REVOCATION_CACHE_TTL` seconds on the endpoints that authorize from JWT
claims alone. Those are `/query`, `/execute` and `/analyze`: precisely the ones
that reach customer data.

The obvious fixes both cost more than they save:

* reading the shared store on every request replaces one DB read per window with
  one Redis read per *request*, which is the property the cache exists to protect;
* shortening the TTL narrows the window without closing it, and pays for the
  privilege in database load.

So invalidation is **broadcast**: the process doing the revoking publishes the
user id, and every worker drops its own entry on receipt. No per-request cost, and
convergence in the time it takes Redis to fan out a message.

Without Redis this degrades to exactly today's behaviour — a bounded TTL window —
rather than to something worse. That is the same rule `login_guard` follows: a
control protecting authentication may weaken when its dependency is gone, but it
must not vanish, and it must not take the service down with it.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_revbroadcast_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "revocation-test-secret-key-long-enough1")
os.environ.setdefault("APP_SECRET_KEY", "revocation-test-app-secret")

deps = importlib.import_module("app_db.deps")


@pytest.fixture(autouse=True)
def clean_cache():
    deps._rev_cache.clear()
    deps._rev_gen.clear()
    yield
    deps._rev_cache.clear()
    deps._rev_gen.clear()


def _seed(user_id: str, token_version: int = 0, is_active: bool = True):
    """Put an entry in the local cache, as a served request would."""
    import time

    deps._rev_cache[user_id] = (time.monotonic() + 999, token_version, is_active)


# ── Local behaviour is unchanged ─────────────────────────────────────────────

def test_invalidating_drops_the_local_entry():
    _seed("user-1")
    deps.invalidate_revocation("user-1")
    assert "user-1" not in deps._rev_cache


def test_invalidating_bumps_the_generation():
    """Guards against a read already in flight writing the stale value back."""
    _seed("user-1")
    before = deps._rev_gen.get("user-1", 0)
    deps.invalidate_revocation("user-1")
    assert deps._rev_gen["user-1"] > before


def test_invalidating_an_unknown_user_is_harmless():
    deps.invalidate_revocation("never-seen")
    deps.invalidate_revocation(None)


# ── The broadcast ────────────────────────────────────────────────────────────

def test_invalidation_is_published(monkeypatch):
    published = []
    monkeypatch.setattr(deps, "_publish_invalidation", lambda uid: published.append(uid))

    _seed("user-2")
    deps.invalidate_revocation("user-2")
    assert published == ["user-2"]


def test_a_received_message_drops_that_users_entry():
    """What another worker does on receipt — the whole point of the broadcast."""
    _seed("user-3")
    _seed("user-4")

    deps._apply_remote_invalidation("user-3")

    assert "user-3" not in deps._rev_cache
    assert "user-4" in deps._rev_cache, "one user's logout must not flush everyone"


def test_a_received_message_also_bumps_the_generation():
    """A receiving worker has in-flight readers too."""
    _seed("user-5")
    before = deps._rev_gen.get("user-5", 0)
    deps._apply_remote_invalidation("user-5")
    assert deps._rev_gen["user-5"] > before


def test_a_malformed_message_is_ignored():
    """A broadcast channel is not a trusted schema; junk must not take a worker down."""
    _seed("user-6")
    for junk in (None, "", b"", 12345):
        deps._apply_remote_invalidation(junk)
    assert "user-6" in deps._rev_cache


# ── Degradation ──────────────────────────────────────────────────────────────

def test_publishing_without_redis_is_silent(monkeypatch):
    """No shared store means the previous behaviour, not an error.

    Revocation still takes effect immediately in this process and within the TTL
    everywhere else — exactly what it did before broadcasting existed.
    """
    monkeypatch.setattr(deps, "_revocation_channel", lambda: None)
    _seed("user-7")
    deps.invalidate_revocation("user-7")          # must not raise
    assert "user-7" not in deps._rev_cache


def test_a_publish_failure_does_not_break_the_revocation(monkeypatch):
    """The local drop is the part that must never be lost."""
    def explode(_uid):
        raise RuntimeError("redis went away mid-publish")

    monkeypatch.setattr(deps, "_publish_invalidation", explode)
    _seed("user-8")
    deps.invalidate_revocation("user-8")
    assert "user-8" not in deps._rev_cache
