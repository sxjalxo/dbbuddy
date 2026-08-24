"""Milestone 3 — chart publishing & client report access.

Covers the draft→publish→client lifecycle: publishing is idempotent and keeps the
draft editable; a PublishedReport is a publication record; clients see only their
org's active reports (visibility-aware) and never receive SQL or credentials; the
live re-query is read-only-guarded and degrades to "needs attention" on failure.

ERP execution is mocked — these tests assert the access-control + lifecycle flow,
not a real database round-trip.
"""

import importlib
import os
import pathlib
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_pub_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "pub-test-secret-key-long-enough-12345")
os.environ.setdefault("APP_SECRET_KEY", "pub-test-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Organization, Role, User  # noqa: E402
from app_db.slug import unique_slug  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> None:
    assert client.post("/auth/register", json={"email": email, "password": "password123"}).status_code in (201, 409)


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
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


def _create_chart(client, token, sql="SELECT month, total FROM sales", connection_id=None, title="Revenue"):
    body = {"title": title, "sql": sql}
    if connection_id is not None:
        body["database_connection_id"] = connection_id
    res = client.post("/charts", json=body, headers=_auth(token))
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    _register(client, "owner@pub.io")  # analyst (chart:save, chart:publish, report:view)
    owner = _login(client, "owner@pub.io")

    _register(client, "owner2@pub.io")  # a second analyst in the same org
    owner2 = _login(client, "owner2@pub.io")

    _register(client, "client@pub.io")  # User role, same (default) org
    _set_roles("client@pub.io", ["user"])
    viewer = _login(client, "client@pub.io")

    other_org = _make_org("Other Org")
    _register(client, "outsider@pub.io")  # User role, different org
    _set_roles("outsider@pub.io", ["user"], org_id=other_org)
    outsider = _login(client, "outsider@pub.io")

    _register(client, "noroles@pub.io")  # no roles at all → no report:view
    _set_roles("noroles@pub.io", [])
    noroles = _login(client, "noroles@pub.io")

    # A saved connection so reports can resolve a (mocked) data source.
    conn = client.post("/connections", json={
        "name": "erp", "engine": "mysql", "host": "db", "username": "u",
        "password": "p", "database": "d",
    }, headers=_auth(owner))
    assert conn.status_code == 201, conn.text

    return {
        "owner": owner, "owner2": owner2, "viewer": viewer,
        "outsider": outsider, "noroles": noroles, "conn_id": conn.json()["id"],
    }


# ── Publish lifecycle ────────────────────────────────────────────────────────

def test_publish_sets_chart_published_and_returns_active_record(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    res = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"]))
    assert res.status_code == 200, res.text
    pub = res.json()
    assert pub["status"] == "active" and pub["chart_id"] == chart_id

    charts = client.get("/charts", headers=_auth(world["owner"])).json()
    assert next(c for c in charts if c["id"] == chart_id)["status"] == "published"


def test_publish_is_idempotent(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    p1 = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    p2 = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    assert p1["id"] == p2["id"]  # same single publication record reused
    # exactly one active report for this chart shows up for a viewer
    reports = client.get("/reports", headers=_auth(world["viewer"])).json()
    assert sum(1 for r in reports if r["chart_id"] == chart_id) == 1


def test_publish_requires_ownership(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    # a different analyst cannot publish someone else's chart
    assert client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner2"])).status_code == 404


def test_publish_requires_chart_publish_permission(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    # the User role lacks chart:publish → 403 (before ownership is even checked)
    assert client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["viewer"])).status_code == 403


def test_unpublish_returns_chart_to_draft_and_hides_report(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    assert client.post(f"/charts/{chart_id}/unpublish", headers=_auth(world["owner"])).status_code == 204

    charts = client.get("/charts", headers=_auth(world["owner"])).json()
    assert next(c for c in charts if c["id"] == chart_id)["status"] == "draft"
    # viewer can no longer see or run it
    assert client.get(f"/reports/{pub['id']}", headers=_auth(world["viewer"])).status_code == 404
    assert client.post(f"/reports/{pub['id']}/run", headers=_auth(world["viewer"])).status_code == 404


# ── Client access scoping ────────────────────────────────────────────────────

def test_client_sees_org_report_without_sql_or_creds(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()

    listing = client.get("/reports", headers=_auth(world["viewer"]))
    assert listing.status_code == 200
    row = next(r for r in listing.json() if r["id"] == pub["id"])
    assert "sql" not in row and "database_connection_id" not in row  # never leak SQL/creds
    assert row["title"] == "Revenue"


def test_outsider_org_cannot_see_or_run_report(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    assert all(r["id"] != pub["id"] for r in client.get("/reports", headers=_auth(world["outsider"])).json())
    assert client.get(f"/reports/{pub['id']}", headers=_auth(world["outsider"])).status_code == 404
    assert client.post(f"/reports/{pub['id']}/run", headers=_auth(world["outsider"])).status_code == 404


def test_reports_require_report_view(client, world):
    assert client.get("/reports", headers=_auth(world["noroles"])).status_code == 403


# ── Live re-query: success, read-only guard, needs-attention ─────────────────

def test_run_returns_live_rows(client, world, monkeypatch):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()

    monkeypatch.setattr("dbbuddy_core.db.connect_db", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        "dbbuddy_core.query.execute_query",
        lambda conn, sql: [{"month": "Jan", "total": 10}, {"month": "Feb", "total": 20}],
    )
    res = client.post(f"/reports/{pub['id']}/run", headers=_auth(world["viewer"]))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["needs_attention"] is False
    assert body["columns"] == ["month", "total"]
    assert body["rows"][1] == {"month": "Feb", "total": 20}


def test_run_blocks_non_read_only_sql(client, world):
    chart_id = _create_chart(client, world["owner"], sql="DELETE FROM users", connection_id=world["conn_id"])
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    body = client.post(f"/reports/{pub['id']}/run", headers=_auth(world["viewer"])).json()
    assert body["ok"] is False and body["needs_attention"] is True
    assert "read-only" in body["message"].lower()


def test_run_needs_attention_when_source_missing(client, world):
    # read-only SQL but no connection attached → degrade gracefully, never stale
    chart_id = _create_chart(client, world["owner"], sql="SELECT 1", connection_id=None)
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()
    body = client.post(f"/reports/{pub['id']}/run", headers=_auth(world["viewer"])).json()
    assert body["ok"] is False and body["needs_attention"] is True
    assert "data source" in body["message"].lower()


# ── Chart customization (type + colors) ──────────────────────────────────────

def test_patch_persists_chart_type_and_config(client, world):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    cfg = {"palette": "sunset", "categoryColors": {"Jan": "#ff0000"}, "seriesColors": {"total": "#00ff00"}}
    stored = {**cfg, "version": 1}  # the server stamps the config schema version
    res = client.patch(
        f"/charts/{chart_id}",
        json={"chart_type": "doughnut", "config": cfg},
        headers=_auth(world["owner"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["chart_type"] == "doughnut"
    assert res.json()["config"] == stored

    # Persisted and returned by the list endpoint.
    got = next(c for c in client.get("/charts", headers=_auth(world["owner"])).json() if c["id"] == chart_id)
    assert got["chart_type"] == "doughnut"
    assert got["config"] == stored


def test_config_version_is_stamped_and_future_versions_rejected(client, world):
    chart_id = _create_chart(client, world["owner"])
    # Unversioned input is stamped v1 so a future schema change can migrate on read.
    res = client.patch(f"/charts/{chart_id}", json={"config": {"palette": "ocean"}},
                       headers=_auth(world["owner"]))
    assert res.json()["config"]["version"] == 1

    # An explicit, supported version is preserved.
    res = client.patch(f"/charts/{chart_id}", json={"config": {"version": 1, "palette": "grape"}},
                       headers=_auth(world["owner"]))
    assert res.json()["config"] == {"version": 1, "palette": "grape"}

    # A config newer than this server understands is refused rather than stored.
    assert client.patch(f"/charts/{chart_id}", json={"config": {"version": 99}},
                        headers=_auth(world["owner"])).status_code == 422
    assert client.patch(f"/charts/{chart_id}", json={"config": {"version": "x"}},
                        headers=_auth(world["owner"])).status_code == 422


def test_patch_rejects_unknown_chart_type(client, world):
    chart_id = _create_chart(client, world["owner"])
    res = client.patch(f"/charts/{chart_id}", json={"chart_type": "hologram"}, headers=_auth(world["owner"]))
    assert res.status_code == 422


def test_patch_rejects_pathological_config(client, world):
    # A deeply-nested config parses under the body-size limit but would crash
    # response serialization if stored (500 on every later chart-list read). It
    # must be rejected at input, and the chart list must stay healthy.
    chart_id = _create_chart(client, world["owner"])
    deep = {"x": {}}
    node = deep
    for _ in range(10):
        node["x"] = {}
        node = node["x"]
    assert client.patch(f"/charts/{chart_id}", json={"config": deep}, headers=_auth(world["owner"])).status_code == 422

    wide = {"categoryColors": {str(i): "#fff" for i in range(6000)}}
    assert client.patch(f"/charts/{chart_id}", json={"config": wide}, headers=_auth(world["owner"])).status_code == 422

    for bad in ("red", ["#fff"], 5, True):
        assert client.patch(f"/charts/{chart_id}", json={"config": bad}, headers=_auth(world["owner"])).status_code == 422

    # The chart list still serializes fine — nothing hostile was stored.
    assert client.get("/charts", headers=_auth(world["owner"])).status_code == 200


def test_patch_requires_ownership(client, world):
    chart_id = _create_chart(client, world["owner"])
    res = client.patch(f"/charts/{chart_id}", json={"chart_type": "pie"}, headers=_auth(world["owner2"]))
    assert res.status_code == 404


def test_published_report_run_carries_type_and_config(client, world, monkeypatch):
    chart_id = _create_chart(client, world["owner"], connection_id=world["conn_id"])
    cfg = {"palette": "ocean", "categoryColors": {"Jan": "#123456"}}
    client.patch(f"/charts/{chart_id}", json={"chart_type": "pie", "config": cfg}, headers=_auth(world["owner"]))
    pub = client.post(f"/charts/{chart_id}/publish", json={}, headers=_auth(world["owner"])).json()

    monkeypatch.setattr("dbbuddy_core.db.connect_db", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        "dbbuddy_core.query.execute_query",
        lambda conn, sql: [{"month": "Jan", "total": 10}],
    )
    body = client.post(f"/reports/{pub['id']}/run", headers=_auth(world["viewer"])).json()
    # The client receives the analyst's latest visual customization with the data.
    assert body["chart_type"] == "pie"
    assert body["config"] == {**cfg, "version": 1}
