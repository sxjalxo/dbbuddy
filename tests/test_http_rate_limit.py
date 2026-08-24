"""HTTP-layer budgets for the endpoints that cost something to serve.

`login_guard` covered `/login`, `/mfa/login` and registration; the engine's own
limiter covers query *cost* per user. Between them sat a gap: `/analyze` walks an
entire schema and was unmetered, `/auth/refresh` was unmetered, and
`/ai-providers/{id}/test` makes this server issue an outbound request at a
caller's direction.

The interesting decision is the failure mode, and it is deliberately *not*
uniform — the codebase already argues both sides and this has to respect both:

* **Throughput budgets fail open.** If Redis is gone, `/analyze` and `/query`
  still work. The cost of a wrong answer here is a warm database; the cost of
  refusing is an outage caused by a cache being down. Same reasoning as
  `dbbuddy_core.rate_limiter`.
* **`/auth/refresh` degrades closed.** It mints sessions, so it follows the
  `login_guard` rule: a control protecting authentication must not vanish exactly
  when the system is already degraded.

A limiter that picked one policy for everything would be wrong for half the
endpoints it covers.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_ratelimit_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "ratelimit-test-secret-key-long-enough-12")
os.environ.setdefault("APP_SECRET_KEY", "ratelimit-test-app-secret")

main = importlib.import_module("main")

from app_db import rate_limit as rl  # noqa: E402
from app_db.login_guard import clear_all  # noqa: E402


@pytest.fixture(autouse=True)
def clean_windows():
    clear_all()
    yield
    clear_all()


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


# ── The key ──────────────────────────────────────────────────────────────────

def test_the_budget_is_per_user_when_a_token_is_present():
    """Two users behind one NAT must not share a budget."""
    from app_db.security import create_access_token

    a = create_access_token(user_id="user-a", email="a@x.io", roles=[], permissions=[])
    b = create_access_token(user_id="user-b", email="b@x.io", roles=[], permissions=[])

    key_a = rl.budget_key("analyze", authorization=f"Bearer {a}", client_ip="10.0.0.1")
    key_b = rl.budget_key("analyze", authorization=f"Bearer {b}", client_ip="10.0.0.1")
    assert key_a != key_b
    assert "user-a" in key_a


def test_the_budget_falls_back_to_the_ip_without_a_token():
    key = rl.budget_key("analyze", authorization=None, client_ip="10.0.0.9")
    assert "10.0.0.9" in key


def test_a_forged_token_does_not_buy_a_private_budget():
    """An unverifiable token falls back to the IP rather than trusting its claims.

    Otherwise anyone could mint `sub: whatever` locally and get a fresh budget per
    request — the limiter would be self-service.
    """
    key = rl.budget_key("analyze", authorization="Bearer not.a.real.token",
                        client_ip="10.0.0.5")
    assert "10.0.0.5" in key
    assert "whatever" not in key


def test_budgets_are_separate_per_endpoint():
    assert rl.budget_key("analyze", authorization=None, client_ip="10.0.0.1") != \
        rl.budget_key("query", authorization=None, client_ip="10.0.0.1")


# ── Enforcement ──────────────────────────────────────────────────────────────

def test_requests_are_refused_once_the_budget_is_spent(monkeypatch):
    # With the shared window present — which is what a real deployment has, and
    # what makes this an enforcement test rather than a fail-open one.
    monkeypatch.setattr(rl, "shared_window_available", lambda: True)
    check = rl.make_check("testbudget", max_requests=3, fail_open=True)
    for _ in range(3):
        assert check(authorization=None, client_ip="10.1.1.1") is None
    retry = check(authorization=None, client_ip="10.1.1.1")
    assert isinstance(retry, int) and retry > 0


def test_one_caller_exhausting_a_budget_does_not_affect_another(monkeypatch):
    monkeypatch.setattr(rl, "shared_window_available", lambda: True)
    check = rl.make_check("isolation", max_requests=2, fail_open=True)
    for _ in range(3):
        check(authorization=None, client_ip="10.2.2.2")
    assert check(authorization=None, client_ip="10.3.3.3") is None


# ── Failure modes ────────────────────────────────────────────────────────────

def test_a_throughput_budget_fails_open_without_the_shared_window(monkeypatch):
    """No Redis must not mean no analysis.

    The cost of not limiting here is a warm database; the cost of refusing is an
    outage caused by a cache being down.
    """
    monkeypatch.setattr(rl, "shared_window_available", lambda: False)
    check = rl.make_check("openbudget", max_requests=1, fail_open=True)
    for _ in range(10):
        assert check(authorization=None, client_ip="10.4.4.4") is None


def test_an_auth_budget_still_applies_without_the_shared_window(monkeypatch):
    """A control protecting authentication must not vanish when Redis does."""
    monkeypatch.setattr(rl, "shared_window_available", lambda: False)
    check = rl.make_check("closedbudget", max_requests=2, fail_open=False)
    seen = [check(authorization=None, client_ip="10.5.5.5") for _ in range(6)]
    assert any(r is not None for r in seen), "the local window still has to decide"


# ── Wired up ─────────────────────────────────────────────────────────────────

def test_refresh_is_throttled(client):
    """Unmetered, this endpoint mints sessions for anyone holding a stale token."""
    from app_db.rate_limit import REFRESH_BUDGET

    last = None
    for _ in range(REFRESH_BUDGET + 2):
        last = client.post("/auth/refresh", json={"refresh_token": "nonsense"})
    assert last.status_code == 429, last.status_code
    assert last.headers.get("Retry-After")


def test_the_429_names_how_long_to_wait(client):
    from app_db.rate_limit import REFRESH_BUDGET

    for _ in range(REFRESH_BUDGET + 1):
        res = client.post("/auth/refresh", json={"refresh_token": "nonsense"})
    assert int(res.headers["Retry-After"]) > 0
