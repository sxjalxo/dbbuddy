"""Milestone 1 — RBAC enforcement tests.

Verifies that every state-changing / query endpoint is reachable only by an
authenticated user holding the right permission, and in particular that the
inline-credential path on /query, /analyze, /execute no longer bypasses auth.

The backend app (``backend/main.py``) imports ``app_db.*`` by bare name, so the
``backend`` dir must be importable. We point the app at an isolated temp SQLite
DB and a fixed JWT secret *before* importing, so this suite never touches a real
deployment DB or keyring.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

# ── Isolate the app DB + secrets BEFORE importing the backend ────────────────
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_rbac_test.db")
os.close(_DB_FD)
os.environ["APP_DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ["JWT_SECRET"] = "rbac-test-secret-key-long-enough-12345"
os.environ["APP_SECRET_KEY"] = "rbac-test-app-secret"

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Role, User  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # The context manager fires the startup event → init_db (create + seed).
    with TestClient(main.app) as c:
        yield c


def _register(client, email: str) -> dict:
    """Register a new analyst-by-default user, return its token pair."""
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _demote_to_user(email: str) -> None:
    """Strip all roles and grant only the read-only ``user`` role."""
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        user_role = db.query(Role).filter(Role.name == "user").one()
        u.roles = [user_role]
        db.commit()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _call(client, method: str, path: str, body, headers=None):
    """Invoke a TestClient method, sending a JSON body only when one is given
    (httpx's .get() rejects a json= kwarg)."""
    kwargs = {}
    if body is not None:
        kwargs["json"] = body
    if headers is not None:
        kwargs["headers"] = headers
    return getattr(client, method)(path, **kwargs)


@pytest.fixture(scope="module")
def analyst_token(client) -> str:
    _register(client, "analyst@test.io")
    return _login(client, "analyst@test.io")


@pytest.fixture(scope="module")
def viewer_token(client) -> str:
    _register(client, "viewer@test.io")
    _demote_to_user("viewer@test.io")  # roles change → must re-login for fresh claims
    return _login(client, "viewer@test.io")


# ── Unauthenticated requests are rejected (401) ──────────────────────────────

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/query", {"question": "count users", "host": "h", "user": "u", "password": "p", "database": "d"}),
        ("post", "/analyze", {"host": "h", "user": "u", "password": "p", "database": "d"}),
        ("post", "/execute", {"sql": "SELECT 1", "host": "h", "user": "u", "password": "p", "database": "d"}),
        ("get", "/connections", None),
        ("post", "/connections", {"name": "c", "engine": "mysql", "host": "h", "username": "u", "password": "p", "database": "d"}),
        ("get", "/charts", None),
        ("post", "/charts", {"title": "t", "sql": "SELECT 1"}),
        ("get", "/history", None),
        ("post", "/api-key", {"provider": "nemotron", "api_key": "x"}),
    ],
)
def test_unauthenticated_is_401(client, method, path, body):
    res = _call(client, method, path, body)
    assert res.status_code == 401, f"{method.upper()} {path} → {res.status_code}, expected 401"


# ── A 'user'-role token is forbidden from analyst/admin actions (403) ────────

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/query", {"question": "count users", "host": "h", "user": "u", "password": "p", "database": "d"}),
        ("post", "/analyze", {"host": "h", "user": "u", "password": "p", "database": "d"}),
        ("post", "/execute", {"sql": "SELECT 1", "host": "h", "user": "u", "password": "p", "database": "d"}),
        ("post", "/connections", {"name": "c", "engine": "mysql", "host": "h", "username": "u", "password": "p", "database": "d"}),
        ("post", "/charts", {"title": "t", "sql": "SELECT 1"}),
        ("get", "/history", None),
        ("post", "/api-key", {"provider": "nemotron", "api_key": "x"}),
    ],
)
def test_viewer_role_is_403(client, viewer_token, method, path, body):
    res = _call(client, method, path, body, headers=_auth(viewer_token))
    assert res.status_code == 403, f"{method.upper()} {path} → {res.status_code}, expected 403"


# ── The analyst role can reach its surfaces ──────────────────────────────────

def test_analyst_can_list_connections_and_charts(client, analyst_token):
    assert client.get("/connections", headers=_auth(analyst_token)).status_code == 200
    assert client.get("/charts", headers=_auth(analyst_token)).status_code == 200
    assert client.get("/history", headers=_auth(analyst_token)).status_code == 200


def test_analyst_can_save_a_chart(client, analyst_token):
    res = client.post("/charts", json={"title": "Revenue", "sql": "SELECT 1"}, headers=_auth(analyst_token))
    assert res.status_code == 201, res.text


def test_inline_query_path_requires_auth_then_runs(client, analyst_token, monkeypatch):
    """The old bypass: inline credentials with no token used to skip auth.

    Now an authenticated analyst passes the gate and the handler runs (we stub
    the pipeline so no real DB connection is attempted)."""
    monkeypatch.setattr(main, "process_query", lambda *a, **k: {"ok": True, "stubbed": True})
    res = client.post(
        "/query",
        json={"question": "count users", "host": "h", "user": "u", "password": "p", "database": "d"},
        headers=_auth(analyst_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["stubbed"] is True


# ── Introspection + claims ───────────────────────────────────────────────────

def test_permissions_catalogue_requires_auth(client, analyst_token):
    assert client.get("/auth/permissions").status_code == 401
    res = client.get("/auth/permissions", headers=_auth(analyst_token))
    assert res.status_code == 200
    names = {p["name"] for p in res.json()["permissions"]}
    assert {"query:run", "settings:ai", "settings:system", "audit:read", "user:manage", "org:manage"} <= names


def test_analyst_me_has_expected_permissions(client, analyst_token):
    res = client.get("/auth/me", headers=_auth(analyst_token))
    assert res.status_code == 200
    perms = set(res.json()["permissions"])
    assert {"query:run", "schema:analyze", "chart:save", "settings:ai"} <= perms
    assert "user:manage" not in perms  # analyst is not an admin
    assert "settings:system" not in perms  # analyst manages AI, not platform settings


def test_module_teardown_cleanup():
    """Best-effort removal of the temp DB file (runs last alphabetically isn't
    guaranteed; the OS temp dir is cleaned regardless)."""
    try:
        os.remove(_DB_PATH)
    except OSError:
        pass
