"""Milestone 4 — audit dashboard (read API + aggregation).

Verifies events are recorded as (actor, org, entity, action) with org scoping, the
read API gates on audit:read and filters by entity/action, the summary aggregates,
and a failed login is captured as a login_failed event.
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
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_audit_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "audit-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "audit-test-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Organization, Role, User  # noqa: E402
from app_db.slug import unique_slug  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> None:
    assert client.post("/auth/register", json={"email": email, "password": "password123"}).status_code in (201, 409)


def _login(client, email: str, password: str = "password123") -> str:
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _set_roles(email: str, role_names: list[str], org_id: str | None = None) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == n).one() for n in role_names]
        if org_id is not None:
            u.organization_id = org_id
        db.commit()


def _make_org(name: str) -> str:
    with SessionLocal() as db:
        o = Organization(name=name, slug=unique_slug(db, name))
        db.add(o)
        db.commit()
        return o.id


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    _register(client, "auditadmin@a.io")
    _set_roles("auditadmin@a.io", ["admin"])  # platform admin (org:manage + audit:read)
    padmin = _login(client, "auditadmin@a.io")

    # An analyst whose actions generate audit events in the default org.
    _register(client, "worker@a.io")
    worker = _login(client, "worker@a.io")
    # generate a couple of events
    client.post("/connections", json={
        "name": "c", "engine": "mysql", "host": "h", "username": "u", "password": "p", "database": "d",
    }, headers=_auth(worker))

    # An org admin in another org: has audit:read (scoped) but not org:manage.
    other = _make_org("Audit Other")
    _register(client, "otheradmin@a.io")
    _set_roles("otheradmin@a.io", ["org_admin"], org_id=other)
    other_admin = _login(client, "otheradmin@a.io")  # re-login → fresh org-scoped claims

    return {"padmin": padmin, "worker": worker, "other_admin": other_admin, "other_org": other}


def test_audit_requires_audit_read(client, world):
    # the analyst has no audit:read
    assert client.get("/admin/audit", headers=_auth(world["worker"])).status_code == 403
    assert client.get("/admin/audit/summary", headers=_auth(world["worker"])).status_code == 403


def test_events_use_entity_and_bare_action(client, world):
    rows = client.get("/admin/audit", headers=_auth(world["padmin"])).json()
    # connection.create was recorded as entity_type="connection", action="create"
    conn_evt = next(r for r in rows if r["entity_type"] == "connection" and r["action"] == "create")
    assert "." not in conn_evt["action"]  # bare verb, not dotted
    assert conn_evt["organization_id"] is not None  # org captured
    # logins are recorded too
    assert any(r["entity_type"] == "user" and r["action"] == "login" for r in rows)


def test_filter_by_entity_and_action(client, world):
    only_conns = client.get("/admin/audit?entity_type=connection&action=create", headers=_auth(world["padmin"])).json()
    assert only_conns and all(r["entity_type"] == "connection" and r["action"] == "create" for r in only_conns)


def test_failed_login_is_audited(client, world):
    # a bad password for an existing user → login_failed event
    assert client.post("/auth/login", json={"email": "worker@a.io", "password": "wrong-password"}).status_code == 401
    rows = client.get("/admin/audit?action=login_failed", headers=_auth(world["padmin"])).json()
    assert any(r["detail"] and r["detail"].get("email") == "worker@a.io" for r in rows)


def test_summary_aggregates(client, world):
    s = client.get("/admin/audit/summary?window_days=30", headers=_auth(world["padmin"])).json()
    assert s["total"] >= 1
    assert s["by_action"].get("create", 0) >= 1  # at least the connection.create
    assert "user" in s["by_entity"]
    assert s["active_users"] >= 1


def test_audit_is_org_scoped(client, world):
    # The other-org admin sees only their own org's events — not the default-org
    # worker's connection.create.
    rows = client.get("/admin/audit", headers=_auth(world["other_admin"])).json()
    assert all(r["organization_id"] == world["other_org"] for r in rows if r["organization_id"])
    assert not any(r["entity_type"] == "connection" and r["action"] == "create" for r in rows)
