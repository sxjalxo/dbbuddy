"""A saved connection can name a schema within its database.

The engine and the CLI already honour ``db_schema`` — introspection follows it
rather than assuming PostgreSQL's ``public``. The platform did not: the
``DatabaseConnection`` record had nowhere to put one, so a hosted user with an
ERP that keeps its tables in a named schema (ordinary, and what Hibernate and
Entity Framework produce) got an empty schema and every question failing to
ground. No error — just no tables.

This closes that: the column, the API field, and the value actually reaching the
``DBConfig`` the pipeline runs on. The last one is the point. A field that is
stored and returned but dropped on the way to the engine looks completely correct
from the UI and changes nothing about the query.

Uses the same TestClient bootstrap as the API-key suite (temp SQLite, real app).
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_dbschema_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "dbschema-test-secret-key-long-enough1")
os.environ.setdefault("APP_SECRET_KEY", "dbschema-test-app-secret")

main = importlib.import_module("main")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _register(client, email: str) -> str:
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()["access_token"]


def _payload(**over):
    body = {
        "name": "erp",
        "engine": "postgresql",
        "host": "db.internal",
        "port": 5432,
        "username": "reader",
        "password": "secret",
        "database": "erp",
    }
    body.update(over)
    return body


# ── The stored field ──────────────────────────────────────────────────────────

def test_a_connection_can_be_created_with_a_schema(client):
    token = _register(client, "create@schema.io")
    res = client.post("/connections", json=_payload(db_schema="sales"), headers=_auth(token))
    assert res.status_code in (200, 201), res.text
    assert res.json()["db_schema"] == "sales"


def test_the_schema_comes_back_on_read(client):
    token = _register(client, "read@schema.io")
    client.post("/connections", json=_payload(db_schema="sales"), headers=_auth(token))
    listed = client.get("/connections", headers=_auth(token)).json()
    assert listed[0]["db_schema"] == "sales"


def test_omitting_it_is_null_not_public(client):
    # "no schema chosen" and "the schema named public" are different states: the
    # first means follow the connection's search_path, which is what a role that
    # already selects a schema needs.
    token = _register(client, "absent@schema.io")
    res = client.post("/connections", json=_payload(), headers=_auth(token))
    assert res.json()["db_schema"] is None


def test_it_can_be_set_on_an_existing_connection(client):
    token = _register(client, "update@schema.io")
    created = client.post("/connections", json=_payload(), headers=_auth(token)).json()
    res = client.patch(f"/connections/{created['id']}",
                       json={"db_schema": "warehouse"}, headers=_auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["db_schema"] == "warehouse"


def test_updating_another_field_leaves_the_schema_alone(client):
    token = _register(client, "partial@schema.io")
    created = client.post("/connections", json=_payload(db_schema="sales"),
                          headers=_auth(token)).json()
    res = client.patch(f"/connections/{created['id']}",
                       json={"name": "renamed"}, headers=_auth(token))
    assert res.json()["db_schema"] == "sales"


def test_it_can_be_cleared(client):
    # Back to following search_path. An empty string is the form's way of saying
    # "none", and must not be stored as a schema literally named "".
    token = _register(client, "clear@schema.io")
    created = client.post("/connections", json=_payload(db_schema="sales"),
                          headers=_auth(token)).json()
    res = client.patch(f"/connections/{created['id']}",
                       json={"db_schema": ""}, headers=_auth(token))
    assert res.json()["db_schema"] is None


# ── Reaching the engine ───────────────────────────────────────────────────────

def test_a_saved_schema_reaches_the_resolved_connection(client):
    token = _register(client, "resolve@schema.io")
    created = client.post("/connections", json=_payload(db_schema="sales"),
                          headers=_auth(token)).json()

    import jwt as _jwt
    payload = _jwt.decode(token, options={"verify_signature": False})

    class _Req:
        connection_id = created["id"]

    resolved = main._resolve_connection(_Req(), payload)
    assert resolved.db_schema == "sales"
    assert resolved.database == "erp"        # the database is still the database


def test_an_inline_request_can_carry_a_schema(client):
    # Test-before-save: the connection is not stored yet, so the schema has to
    # travel on the request like every other credential field.
    class _Req:
        connection_id = None
        host, user, password, database, engine, port = (
            "db.internal", "reader", "secret", "erp", "postgresql", 5432,
        )
        db_schema = "sales"

    resolved = main._resolve_connection(_Req(), {})
    assert resolved.db_schema == "sales"


def test_a_request_without_a_schema_resolves_to_none(client):
    class _Req:
        connection_id = None
        host, user, password, database, engine, port = (
            "db.internal", "reader", "secret", "erp", "postgresql", 5432,
        )

    resolved = main._resolve_connection(_Req(), {})
    assert resolved.db_schema is None


def test_the_analyze_request_model_accepts_a_schema():
    # Whatever /analyze and /query parse must have somewhere to put it, or the
    # field is dropped before any of the above runs.
    req = main.AnalyzeRequest(host="h", user="u", password="p", database="d",
                              engine="postgresql", db_schema="sales")
    assert req.db_schema == "sales"
