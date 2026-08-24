"""Tests for the hardening pass: egress guard, access-token revocation, opaque
500s, security headers, ERP backpressure on the API paths, and per-user rate
limiting.

Same bootstrap contract as ``test_rbac_enforcement.py``: the backend app imports
``app_db.*`` by bare name, so ``backend/`` must be importable and the app must be
pointed at an isolated SQLite file and a fixed JWT secret *before* ``main`` is
imported.
"""

import importlib
import os
import pathlib
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_hardening_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "hardening-test-secret-key-long-enough-123")
os.environ.setdefault("APP_SECRET_KEY", "hardening-test-app-secret")

main = importlib.import_module("main")

from app_db import deps  # noqa: E402
from app_db.url_guard import validate_outbound_url  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> dict:
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()


# ── Egress guard (SSRF) ───────────────────────────────────────────────────────

class TestOutboundUrlGuard:
    def test_cloud_metadata_address_is_always_refused(self):
        # The single highest-value case: on any cloud VM this endpoint hands out
        # IAM credentials to whatever can reach it.
        with pytest.raises(ValueError, match="link-local"):
            validate_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO"):
            with pytest.raises(ValueError, match="http or https"):
                validate_outbound_url(url)

    def test_localhost_is_allowed_by_default(self):
        # DB Buddy's default deployment runs Ollama on localhost; blocking private
        # space by default would break the product's normal configuration.
        assert validate_outbound_url("http://localhost:11434") == "http://localhost:11434"

    def test_strict_mode_blocks_private_space(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", "1")
        with pytest.raises(ValueError, match="private or loopback"):
            validate_outbound_url("http://10.0.0.5:8000/v1")
        with pytest.raises(ValueError, match="private or loopback"):
            validate_outbound_url("http://127.0.0.1:11434")

    def test_blank_and_none_pass_through(self):
        assert validate_outbound_url(None) is None
        assert validate_outbound_url("") == ""

    def test_provider_creation_rejects_a_metadata_endpoint(self, client):
        tokens = _register(client, "ssrf-probe@hardening.io")
        res = client.post("/ai-providers", json={
            "name": "probe", "adapter": "openai_compatible",
            "base_url": "http://169.254.169.254/", "model": "x",
        }, headers=_auth(tokens["access_token"]))
        # 422 (schema validation) when permitted, 403 when the role lacks
        # settings:ai — either way the record is never created.
        assert res.status_code in (403, 422), res.text


# ── Access-token revocation ───────────────────────────────────────────────────

class TestAccessTokenRevocation:
    def test_logout_invalidates_the_access_token_immediately(self, client):
        tokens = _register(client, "revoke-me@hardening.io")
        access = tokens["access_token"]
        assert client.get("/auth/me", headers=_auth(access)).status_code == 200

        assert client.post("/auth/logout", headers=_auth(access)).status_code == 200
        # Previously the access token kept working for its full lifetime.
        assert client.get("/auth/me", headers=_auth(access)).status_code == 401

    def test_revoked_token_cannot_reach_the_erp_query_path(self, client):
        # The claims-only endpoints are the ones that touch customer data, and
        # they were the ones with no revocation check at all.
        tokens = _register(client, "revoke-query@hardening.io")
        access = tokens["access_token"]
        client.post("/auth/logout", headers=_auth(access))
        res = client.post("/query", json={"question": "how many users"}, headers=_auth(access))
        assert res.status_code == 401
        assert "revoked" in res.json()["detail"].lower()

    def test_a_concurrent_invalidation_is_not_overwritten(self, client):
        """A read in flight when a logout lands must not resurrect the old state.

        Ordering: reader starts → captures generation → logout bumps the version
        and invalidates → reader finishes and tries to publish. Publishing the
        pre-logout value would keep the revoked token working for a full TTL,
        which is precisely the guarantee invalidate_revocation() sells.
        """
        deps.reset_revocation_cache()
        started = threading.Event()
        release = threading.Event()
        real_read = deps._read_account_state

        def _slow_read(user_id):
            started.set()
            release.wait(timeout=5)
            return real_read(user_id)

        with patch.object(deps, "_read_account_state", _slow_read):
            result = {}
            reader = threading.Thread(
                target=lambda: result.update(state=deps._account_state("u-race")))
            reader.start()
            assert started.wait(timeout=5)
            deps.invalidate_revocation("u-race")  # lands mid-read
            release.set()
            reader.join(timeout=5)

        assert "u-race" not in deps._rev_cache, \
            "a stale read was published after an explicit invalidation"

    def test_concurrent_misses_do_not_stampede_the_app_db(self):
        deps.reset_revocation_cache()
        calls = []
        barrier = threading.Barrier(8, timeout=5)

        def _counted(user_id):
            calls.append(user_id)
            time.sleep(0.05)  # hold the flight open so the others pile up
            return (0, True)

        with patch.object(deps, "_read_account_state", _counted):
            def _worker():
                barrier.wait()
                deps._account_state("u-hot")

            threads = [threading.Thread(target=_worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # Single-flight: one DB read for eight simultaneous misses, not eight.
        assert len(calls) == 1, f"{len(calls)} app-DB reads for one cache miss"

    def test_the_cache_is_bounded(self, monkeypatch):
        deps.reset_revocation_cache()
        monkeypatch.setattr(deps, "REVOCATION_CACHE_MAX", 50)
        with patch.object(deps, "_read_account_state", lambda uid: (0, True)):
            for i in range(500):
                deps._account_state(f"user-{i}")
        stats = deps.revocation_cache_stats()
        assert stats["entries"] <= 50, "cache grew past its cap — unbounded memory"
        # Generations must not become the leak the cache no longer is.
        assert stats["tracked_generations"] <= 50

    def test_a_missing_subject_is_not_a_db_lookup(self):
        # payload["sub"] can be absent on a malformed token; don't hand None to
        # Session.get().
        assert deps._account_state(None) is None
        assert deps._account_state("") is None

    def test_deactivation_cuts_the_claims_only_path_off(self, client):
        from app_db.database import SessionLocal
        from app_db.models import User

        tokens = _register(client, "deactivate@hardening.io")
        access = tokens["access_token"]
        with SessionLocal() as db:
            u = db.query(User).filter(User.email == "deactivate@hardening.io").one()
            u.is_active = False
            db.commit()
        deps.reset_revocation_cache()  # skip the cache window, not the check

        assert client.post("/query", json={"question": "x"}, headers=_auth(access)).status_code == 401


# ── Response hygiene ──────────────────────────────────────────────────────────

class TestResponseHygiene:
    def test_security_headers_are_present(self, client):
        res = client.get("/")
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Referrer-Policy"] == "no-referrer"
        assert res.headers["Cache-Control"] == "no-store"

    def test_internal_errors_do_not_echo_driver_text(self, client):
        tokens = _register(client, "opaque-500@hardening.io")
        secret = "host=10.1.2.3 user=erp_admin password=hunter2"

        def _boom(*a, **k):
            raise RuntimeError(secret)

        with patch("main.process_query", _boom):
            res = client.post("/query", json={"question": "anything"},
                              headers=_auth(tokens["access_token"]))
        assert res.status_code == 500
        # The DSN must not come back to the caller; the correlation id must.
        assert secret not in res.text
        assert "erp_admin" not in res.text
        assert res.headers["X-Request-ID"] in res.json()["detail"]


# ── ERP backpressure reaches the API paths ────────────────────────────────────

class TestBackpressure:
    def test_a_saturated_target_is_503_not_500(self, client):
        from dbbuddy_core import erp_concurrency

        tokens = _register(client, "busy@hardening.io")

        def _busy(*a, **k):
            raise erp_concurrency.ERPBusy("This database is handling too many requests right now")

        with patch("main.process_query", _busy):
            res = client.post("/query", json={"question": "anything"},
                              headers=_auth(tokens["access_token"]))
        assert res.status_code == 503
        assert res.headers["Retry-After"] == "5"

    def test_the_pooled_query_path_holds_a_slot(self):
        # The pool bounds only *idle* connections, so the semaphore is what keeps
        # concurrent borrows — and therefore live ERP sessions — bounded.
        from dbbuddy_core import context_store, erp_concurrency

        erp_concurrency.reset()
        seen = {}

        class _Pool:
            _config = type("C", (), {"engine": "mysql", "host": "h", "port": 3306,
                                     "database": "d"})()

            def acquire(self):
                seen["in_use"] = erp_concurrency.snapshot()["mysql|h|3306|d"]["in_use"]
                return object()

            def release(self, conn):
                pass

        ctx = context_store.DBContext(
            key="k", schema={}, schema_hash="h", semantic={}, vector_store=None,
            relationship_graph={}, cache=None, pool=_Pool(),
        )
        with ctx.connection():
            pass
        assert seen["in_use"] == 1, "connection() borrowed without holding a concurrency slot"
        assert erp_concurrency.snapshot()["mysql|h|3306|d"]["in_use"] == 0


# ── Rate limiting is per-caller ───────────────────────────────────────────────

class TestRateLimitKeying:
    def test_the_query_endpoint_keys_the_limiter_by_user(self, client):
        # Every authenticated caller previously collapsed onto the bucket
        # "default", making the 10 req/s ceiling global rather than per-user.
        tokens = _register(client, "ratekey@hardening.io")
        captured = {}

        def _capture(config, question, **kwargs):
            captured.update(kwargs)
            return {"sql": None, "auto_executed": False}

        with patch("main.process_query", _capture):
            client.post("/query", json={"question": "x"},
                        headers=_auth(tokens["access_token"]))

        assert captured.get("user_id"), "no user_id threaded to the rate limiter"
        assert captured["user_id"] != "default"

    def test_distinct_users_get_distinct_buckets(self):
        from dbbuddy_core.rate_limiter import RateLimiter

        class _Cache:
            connected = True

            def __init__(self):
                self.keys = []
                self.client = self

            def lrange(self, key, a, b):
                self.keys.append(key)
                return []

            def pipeline(self):
                return self

            def lpush(self, *a):
                pass

            def ltrim(self, *a):
                pass

            def expire(self, *a):
                pass

            def execute(self):
                pass

        cache = _Cache()
        limiter = RateLimiter(cache=cache, max_requests=10, window_seconds=1)
        limiter.check_rate_limit("user-a")
        limiter.check_rate_limit("user-b")
        assert cache.keys == ["rate_limit:user-a", "rate_limit:user-b"]
