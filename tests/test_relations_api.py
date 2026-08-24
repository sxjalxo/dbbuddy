"""API tests for the analyst-only relation-graph endpoints.

Covers the RBAC gate (only schema:analyze holders — i.e. analysts — get in),
snapshotting a connection (live introspection mocked), the database-level
overview built from stored snapshots, the table-level detail graph, ownership
scoping, and cleanup when a connection is deleted.
"""

import importlib
import os
import pathlib
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_relations_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "relations-test-secret-key-long-enough-123456")
os.environ.setdefault("APP_SECRET_KEY", "relations-test-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Role, SchemaSnapshot, User  # noqa: E402
from dbbuddy_core.dialects.schema_meta import (  # noqa: E402
    ColumnMeta,
    DatabaseSchema,
    ForeignKeyMeta,
    TableMeta,
)


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _register(client, email: str) -> None:
    assert client.post("/auth/register", json={"email": email, "password": "password123"}).status_code in (201, 409)


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _set_roles(email: str, roles: list[str]) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == n).one() for n in roles]
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def world(client):
    _register(client, "rel-analyst@x.io")  # analyst → schema:analyze
    analyst = _login(client, "rel-analyst@x.io")
    _register(client, "rel-client@x.io")
    _set_roles("rel-client@x.io", ["user"])  # no schema:analyze
    client_tok = _login(client, "rel-client@x.io")
    return {"analyst": analyst, "client": client_tok}


def _make_conn(client, token, name, database):
    res = client.post("/connections", json={
        "name": name, "engine": "mysql", "host": "h", "username": "u",
        "password": "p", "database": database,
    }, headers=_auth(token))
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _rich(tables: dict[str, tuple[list[str], list[tuple]]]) -> DatabaseSchema:
    """Build a DatabaseSchema. tables = {name: ([cols], [(col, ref_table, ref_col)])}."""
    schema = DatabaseSchema()
    for name, (cols, fks) in tables.items():
        schema.tables[name] = TableMeta(
            name=name,
            columns=[ColumnMeta(c, "int", is_primary_key=(c == "id")) for c in cols],
            foreign_keys=[ForeignKeyMeta(*fk) for fk in fks],
        )
    return schema


def _snapshot(client, token, connection_id, schema: DatabaseSchema):
    """Snapshot a connection with live introspection mocked to return `schema`."""
    fake_conn = MagicMock()
    fake_conn.fetch_schema_rich.return_value = schema
    with patch("dbbuddy_core.db.connect_db", return_value=fake_conn):
        res = client.post(f"/relations/snapshot/{connection_id}", headers=_auth(token))
    # When a snapshot actually ran, the live connection must be closed (no leak).
    if res.status_code == 200:
        fake_conn.close.assert_called_once()
    return res


# ── RBAC gate ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
    ("get", "/relations/overview"),
    ("post", "/relations/snapshot"),
])
def test_non_analyst_is_forbidden(client, world, method, path):
    res = getattr(client, method)(path, headers=_auth(world["client"]))
    assert res.status_code == 403


def test_unauthenticated_is_401(client):
    assert client.get("/relations/overview").status_code == 401


# ── snapshot + graphs ─────────────────────────────────────────────────────────

def test_snapshot_captures_schema_and_detail_graph_uses_fks(client, world):
    cid = _make_conn(client, world["analyst"], "Shop", "shopdb")
    schema = _rich({
        "orders": (["id", "customer_id"], [("customer_id", "customers", "id")]),
        "customers": (["id", "name"], []),
    })
    res = _snapshot(client, world["analyst"], cid, schema)
    assert res.status_code == 200, res.text
    assert res.json()["table_count"] == 2

    detail = client.get(f"/relations/detail/{cid}", headers=_auth(world["analyst"]))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["level"] == "detail"
    assert {n["id"] for n in body["nodes"]} == {"orders", "customers"}
    assert body["edges"] == [
        {"source": "orders", "target": "customers", "from_col": "customer_id", "to_col": "id", "kind": "fk"}
    ]


def test_detail_requires_a_snapshot_first(client, world):
    cid = _make_conn(client, world["analyst"], "Unsnapped", "freshdb")
    res = client.get(f"/relations/detail/{cid}", headers=_auth(world["analyst"]))
    assert res.status_code == 409  # not snapshotted yet


def test_overview_lists_all_and_infers_cross_db_links(client, world):
    # A fresh analyst so the overview contains exactly the two DBs we set up.
    _register(client, "rel-a2@x.io")
    a2 = _login(client, "rel-a2@x.io")
    crm = _make_conn(client, a2, "CRM", "crmdb")
    billing = _make_conn(client, a2, "Billing", "billdb")
    _snapshot(client, a2, crm, _rich({"customers": (["id", "name"], [])}))
    _snapshot(client, a2, billing, _rich({"invoices": (["id", "customer_id"], [])}))

    res = client.get("/relations/overview", headers=_auth(a2))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["level"] == "overview"
    assert {n["id"] for n in body["nodes"]} == {crm, billing}
    assert body["stats"]["snapshotted"] == 2
    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert {edge["source"], edge["target"]} == {crm, billing}
    assert edge["confidence"] == 0.9 and "customer" in edge["shared"]


def test_overview_includes_unsnapshotted_connections_as_edgeless_nodes(client, world):
    _register(client, "rel-a3@x.io")
    a3 = _login(client, "rel-a3@x.io")
    _make_conn(client, a3, "NoSnap", "nosnapdb")
    body = client.get("/relations/overview", headers=_auth(a3)).json()
    assert body["stats"] == {
        "databases": 1, "snapshotted": 0, "links": 0, "total_links": 0, "truncated": False,
    }
    assert body["nodes"][0]["has_snapshot"] is False


# ── scoping + cleanup ─────────────────────────────────────────────────────────

def test_cannot_snapshot_or_view_another_users_connection(client, world):
    _register(client, "rel-owner@x.io")
    owner = _login(client, "rel-owner@x.io")
    cid = _make_conn(client, owner, "Private", "privdb")
    # A different analyst may not touch it.
    assert _snapshot(client, world["analyst"], cid, _rich({"t": (["id"], [])})).status_code == 404
    assert client.get(f"/relations/detail/{cid}", headers=_auth(world["analyst"])).status_code == 404


def test_deleting_connection_removes_its_snapshot(client, world):
    cid = _make_conn(client, world["analyst"], "Temp", "tempdb")
    _snapshot(client, world["analyst"], cid, _rich({"t": (["id"], [])}))
    with SessionLocal() as db:
        assert db.query(SchemaSnapshot).filter_by(connection_id=cid).count() == 1
    assert client.delete(f"/connections/{cid}", headers=_auth(world["analyst"])).status_code == 204
    with SessionLocal() as db:
        assert db.query(SchemaSnapshot).filter_by(connection_id=cid).count() == 0
