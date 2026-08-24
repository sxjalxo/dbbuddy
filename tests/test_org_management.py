"""Milestone 2 — Organization & user-management tests.

Covers the tenancy invariant (every user has an org), org_id in JWT claims, org
CRUD + scoping (platform admin vs org admin), and the anti-escalation guards
(an org admin cannot mint platform/org admins or reach into another org).

Like the RBAC suite, this points the app at an isolated temp SQLite DB before
importing the backend. Assertions are isolation-tolerant (unique emails, relative
counts, filter by created ids) so the suite is robust if it shares the process's
app-DB singleton with another backend test module.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import jwt
import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_org_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "org-test-secret-key-long-enough-12345")
os.environ.setdefault("APP_SECRET_KEY", "org-test-app-secret")

main = importlib.import_module("main")
from app_db.config import settings as app_settings  # noqa: E402
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Organization, Role, User  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> None:
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code in (201, 409), res.text


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _set_role_and_org(email: str, role_name: str, org_id: str | None = None) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        role = db.query(Role).filter(Role.name == role_name).one()
        u.roles = [role]
        if org_id is not None:
            u.organization_id = org_id
        db.commit()


def _default_org_id() -> str:
    with SessionLocal() as db:
        return db.query(Organization).filter_by(is_default=True).one().id


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    """A platform admin, an org admin in Org B, and a default-org analyst."""
    _register(client, "padmin@org.io")
    _set_role_and_org("padmin@org.io", "admin")  # keeps default org
    padmin = _login(client, "padmin@org.io")

    rb = client.post("/orgs", json={"name": "Org B"}, headers=_auth(padmin))
    assert rb.status_code == 201, rb.text
    org_b = rb.json()["id"]

    _register(client, "oadmin@org.io")
    _set_role_and_org("oadmin@org.io", "org_admin", org_b)
    oadmin = _login(client, "oadmin@org.io")  # re-login → fresh claims (org_b, org_admin)

    _register(client, "analyst2@org.io")  # analyst by default, default org
    analyst = _login(client, "analyst2@org.io")

    return {
        "padmin": padmin, "oadmin": oadmin, "analyst": analyst,
        "org_b": org_b, "default_org": _default_org_id(),
    }


# ── Tenancy invariant + claims ───────────────────────────────────────────────

def test_registered_user_belongs_to_an_org(client, world):
    me = client.get("/auth/me", headers=_auth(world["analyst"])).json()
    assert me["organization_id"], "self-registered user must land in the default org"


def test_jwt_carries_org_id(world):
    payload = jwt.decode(world["oadmin"], app_settings.JWT_SECRET, algorithms=[app_settings.JWT_ALGORITHM])
    assert payload["org_id"] == world["org_b"]


# ── Org CRUD + scoping ───────────────────────────────────────────────────────

def test_create_org_requires_org_manage(client, world):
    assert client.post("/orgs", json={"name": "Nope"}, headers=_auth(world["analyst"])).status_code == 403
    assert client.post("/orgs", json={"name": "Nope"}, headers=_auth(world["oadmin"])).status_code == 403


def test_platform_admin_sees_all_org_admin_sees_own(client, world):
    all_orgs = client.get("/orgs", headers=_auth(world["padmin"])).json()
    assert len(all_orgs) >= 2  # default + Org B (+ any others)
    own = client.get("/orgs", headers=_auth(world["oadmin"])).json()
    assert [o["id"] for o in own] == [world["org_b"]]


def test_default_org_is_protected(client, world):
    orgs = client.get("/orgs", headers=_auth(world["padmin"])).json()
    default = [o for o in orgs if o["is_default"]]
    assert len(default) == 1
    res = client.delete(f"/orgs/{default[0]['id']}", headers=_auth(world["padmin"]))
    assert res.status_code == 400


def test_delete_empty_org_succeeds(client, world):
    created = client.post("/orgs", json={"name": "Disposable"}, headers=_auth(world["padmin"])).json()
    assert client.delete(f"/orgs/{created['id']}", headers=_auth(world["padmin"])).status_code == 204


# ── User management scoping + anti-escalation ────────────────────────────────

def test_user_management_requires_user_manage(client, world):
    assert client.get("/admin/users", headers=_auth(world["analyst"])).status_code == 403


def test_org_admin_cannot_grant_privileged_roles(client, world):
    for role in ("admin", "org_admin"):
        res = client.post(
            "/admin/users",
            json={"email": f"esc-{role}@org.io", "password": "password123", "role": role},
            headers=_auth(world["oadmin"]),
        )
        assert res.status_code == 403, f"org_admin granting {role} should be forbidden"


def test_org_admin_creates_member_forced_into_own_org(client, world):
    # Even though we pass the default org id, an org admin's new user is forced
    # into the org admin's own org (Org B).
    res = client.post(
        "/admin/users",
        json={"email": "member1@org.io", "password": "password123", "role": "analyst",
              "organization_id": world["default_org"]},
        headers=_auth(world["oadmin"]),
    )
    assert res.status_code == 201, res.text
    assert res.json()["organization_id"] == world["org_b"]


def test_platform_admin_creates_user_in_target_org(client, world):
    res = client.post(
        "/admin/users",
        json={"email": "member2@org.io", "password": "password123", "role": "analyst",
              "organization_id": world["org_b"]},
        headers=_auth(world["padmin"]),
    )
    assert res.status_code == 201, res.text
    assert res.json()["organization_id"] == world["org_b"]


def test_user_listing_is_org_scoped(client, world):
    padmin_list = client.get("/admin/users", headers=_auth(world["padmin"])).json()
    oadmin_list = client.get("/admin/users", headers=_auth(world["oadmin"])).json()
    assert len(padmin_list) > len(oadmin_list)
    assert all(u["organization_id"] == world["org_b"] for u in oadmin_list)


def test_org_admin_cannot_touch_other_org_user(client, world):
    padmin_list = client.get("/admin/users", headers=_auth(world["padmin"])).json()
    default_user = next(u for u in padmin_list if u["email"] == "analyst2@org.io")
    res = client.patch(
        f"/admin/users/{default_user['id']}",
        json={"is_active": False},
        headers=_auth(world["oadmin"]),
    )
    assert res.status_code == 404  # other-org users are invisible


def test_only_platform_admin_moves_users_between_orgs(client, world):
    created = client.post(
        "/admin/users",
        json={"email": "mover@org.io", "password": "password123", "role": "analyst",
              "organization_id": world["org_b"]},
        headers=_auth(world["padmin"]),
    ).json()
    # org admin cannot move users between orgs
    blocked = client.patch(
        f"/admin/users/{created['id']}",
        json={"organization_id": world["default_org"]},
        headers=_auth(world["oadmin"]),
    )
    assert blocked.status_code in (403, 404)  # 404 if out of scope, 403 if scope ok but move denied
    # platform admin can
    ok = client.patch(
        f"/admin/users/{created['id']}",
        json={"organization_id": world["default_org"]},
        headers=_auth(world["padmin"]),
    )
    assert ok.status_code == 200
    assert ok.json()["organization_id"] == world["default_org"]


def test_cannot_deactivate_self(client, world):
    me = client.get("/auth/me", headers=_auth(world["padmin"])).json()
    res = client.patch(f"/admin/users/{me['id']}", json={"is_active": False}, headers=_auth(world["padmin"]))
    assert res.status_code == 400


# ── User deletion (hard delete + owned-data cleanup) ─────────────────────────

def _find_user(client, admin_token: str, email: str) -> dict:
    users = client.get("/admin/users", headers=_auth(admin_token)).json()
    return next(u for u in users if u["email"] == email)


def test_delete_user_requires_user_manage(client, world):
    victim = _find_user(client, world["padmin"], "analyst2@org.io")
    assert client.delete(f"/admin/users/{victim['id']}", headers=_auth(world["analyst"])).status_code == 403


def test_cannot_delete_self(client, world):
    me = client.get("/auth/me", headers=_auth(world["padmin"])).json()
    assert client.delete(f"/admin/users/{me['id']}", headers=_auth(world["padmin"])).status_code == 400


def test_delete_nonexistent_user_is_404(client, world):
    assert client.delete("/admin/users/does-not-exist", headers=_auth(world["padmin"])).status_code == 404


def test_org_admin_cannot_delete_other_org_user(client, world):
    victim = _find_user(client, world["padmin"], "analyst2@org.io")  # default org, invisible to Org B admin
    assert client.delete(f"/admin/users/{victim['id']}", headers=_auth(world["oadmin"])).status_code == 404


def test_delete_user_removes_account_and_owned_data(client, world):
    """A hard delete removes the account and everything it owns, but keeps the
    audit trail (rows de-attributed, not deleted). Exercised end-to-end so the
    explicit cleanup is proven on the app DB, not just assumed from cascades."""
    from app_db.models import AuditLog, DatabaseConnection, PublishedReport, SavedChart, User

    # A disposable analyst with owned data: a connection, a chart, a publication.
    _register(client, "doomed@org.io")
    doomed = _login(client, "doomed@org.io")
    uid = client.get("/auth/me", headers=_auth(doomed)).json()["id"]

    conn = client.post("/connections", json={
        "name": "erp", "engine": "mysql", "host": "h", "username": "u",
        "password": "p", "database": "d",
    }, headers=_auth(doomed))
    assert conn.status_code == 201, conn.text
    chart = client.post("/charts", json={"title": "Doomed chart", "sql": "SELECT 1"}, headers=_auth(doomed))
    assert chart.status_code == 201, chart.text
    chart_id = chart.json()["id"]
    assert client.post(f"/charts/{chart_id}/publish", headers=_auth(doomed)).status_code == 200

    # Owned rows and de-attributable audit rows exist before deletion.
    with SessionLocal() as db:
        assert db.query(SavedChart).filter_by(user_id=uid).count() == 1
        assert db.query(DatabaseConnection).filter_by(user_id=uid).count() == 1
        assert db.query(PublishedReport).filter_by(published_by=uid).count() == 1
        audit_ids_before = {r.id for r in db.query(AuditLog).filter(AuditLog.user_id == uid).all()}
    assert audit_ids_before, "the doomed user should have authored some audit rows"

    # Platform admin deletes the account.
    assert client.delete(f"/admin/users/{uid}", headers=_auth(world["padmin"])).status_code == 204

    # The account and its owned data are gone; the audit rows survive, de-attributed.
    with SessionLocal() as db:
        assert db.get(User, uid) is None
        assert db.query(SavedChart).filter_by(user_id=uid).count() == 0
        assert db.query(DatabaseConnection).filter_by(user_id=uid).count() == 0
        assert db.query(PublishedReport).filter_by(published_by=uid).count() == 0
        assert db.query(AuditLog).filter(AuditLog.user_id == uid).count() == 0  # de-attributed
        surviving = {r.id for r in db.query(AuditLog).filter(AuditLog.id.in_(audit_ids_before)).all()}
        assert surviving == audit_ids_before, "audit rows must be kept, only their actor nulled"
        # The deletion itself is recorded, attributed to the acting admin.
        deleted_event = (
            db.query(AuditLog)
            .filter(AuditLog.action == "delete", AuditLog.entity_type == "user", AuditLog.entity_id == uid)
            .one()
        )
        assert deleted_event.detail.get("email") == "doomed@org.io"

    # The deleted user can no longer authenticate.
    assert client.post("/auth/login", json={"email": "doomed@org.io", "password": "password123"}).status_code == 401


def test_org_admin_deletes_own_org_member(client, world):
    """The core ask: an org admin can clean up a dead account in their own org."""
    made = client.post("/admin/users", json={
        "email": "cleanup@org.io", "password": "password123", "role": "user",
    }, headers=_auth(world["oadmin"]))
    assert made.status_code == 201, made.text
    assert client.delete(f"/admin/users/{made.json()['id']}", headers=_auth(world["oadmin"])).status_code == 204
    assert all(u["email"] != "cleanup@org.io" for u in client.get("/admin/users", headers=_auth(world["oadmin"])).json())
