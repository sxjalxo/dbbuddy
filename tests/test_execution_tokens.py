"""Execution-token workflow — confirmed writes run server-stored SQL.

A planner-generated write from /query is held for confirmation and carries a
single-use ``execution_token``. /execute redeems the token and runs the exact SQL
the server stored, so the client cannot alter it after confirmation. Raw write SQL
is allowed only for callers holding ``query:write:manual``; raw reads are open.
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_exec_tokens.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "exec-token-secret-key-long-enough-1234567")
os.environ.setdefault("APP_SECRET_KEY", "exec-token-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import Role, User  # noqa: E402

_CONN = {"host": "erpdb", "user": "svc", "password": "pw", "database": "erp", "engine": "mysql"}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> None:
    r = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code in (201, 409), r.text


def _login(client, email: str) -> str:
    r = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _set_roles(email: str, role_names: list[str]) -> None:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).one()
        u.roles = [db.query(Role).filter(Role.name == n).one() for n in role_names]
        db.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def analyst(client):
    """A full analyst (has query:write:manual)."""
    _register(client, "analyst@tok.io")
    return _login(client, "analyst@tok.io")


@pytest.fixture
def restricted(client):
    """A custom role with query:run but NOT query:write:manual."""
    with SessionLocal() as db:
        from app_db.models import Permission
        role = db.query(Role).filter_by(name="restricted_runner").one_or_none()
        if role is None:
            role = Role(name="restricted_runner", description="run only")
            db.add(role)
        role.permissions = [db.query(Permission).filter_by(name="query:run").one()]
        db.commit()
    _register(client, "restricted@tok.io")
    _set_roles("restricted@tok.io", ["restricted_runner"])
    return _login(client, "restricted@tok.io")


def _mock_execution(monkeypatch, sql_seen: list):
    """Patch the DB layer so /execute records the SQL it ran without a real DB."""
    import dbbuddy_core.db as core_db
    import dbbuddy_core.query as core_query

    def _run(conn, sql):
        sql_seen.append(sql)
        return [{"rows_affected": 1}]

    monkeypatch.setattr(core_db, "connect_db", lambda *a, **k: MagicMock())
    monkeypatch.setattr(core_query, "execute_query", _run)


def _mock_planned_write(monkeypatch, sql="DELETE FROM orders WHERE id = 1"):
    """Patch the pipeline so /query returns a held write plan carrying `sql`."""
    monkeypatch.setattr(main, "process_query", lambda *a, **k: {
        "sql": sql, "auto_executed": False, "requires_confirmation": True,
        "safety_category": "write", "warning": "will delete",
    })


def _plan_write(client, token, monkeypatch, sql="DELETE FROM orders WHERE id = 1") -> str:
    _mock_planned_write(monkeypatch, sql)
    res = client.post("/query", json={**_CONN, "question": "delete order 1"}, headers=_auth(token))
    assert res.status_code == 200, res.text
    exec_token = res.json().get("execution_token")
    assert exec_token, "expected /query to mint an execution_token for a held write"
    return exec_token


# ── happy path ────────────────────────────────────────────────────────────────

def test_query_mints_token_and_execute_runs_stored_sql(client, analyst, monkeypatch):
    exec_token = _plan_write(client, analyst, monkeypatch)

    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    # The client tries to smuggle DIFFERENT sql — it must be ignored.
    res = client.post("/execute", json={
        **_CONN, "sql": "DELETE FROM orders", "execution_token": exec_token,
    }, headers=_auth(analyst))
    assert res.status_code == 200, res.text
    assert res.json()["write"] is True
    assert sql_seen == ["DELETE FROM orders WHERE id = 1"], "server-stored SQL must win"


def test_token_is_single_use(client, analyst, monkeypatch):
    exec_token = _plan_write(client, analyst, monkeypatch)
    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)

    first = client.post("/execute", json={**_CONN, "execution_token": exec_token}, headers=_auth(analyst))
    assert first.status_code == 200, first.text
    second = client.post("/execute", json={**_CONN, "execution_token": exec_token}, headers=_auth(analyst))
    assert second.status_code == 409  # already consumed


def test_token_context_mismatch_rejected_without_consuming(client, analyst, monkeypatch):
    exec_token = _plan_write(client, analyst, monkeypatch)
    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)

    # Redeem against a DIFFERENT database than the plan was reviewed against.
    bad = client.post("/execute", json={
        **{**_CONN, "database": "other"}, "execution_token": exec_token,
    }, headers=_auth(analyst))
    assert bad.status_code == 409
    assert not sql_seen, "mismatched-context redemption must not execute"

    # The token was not burned — it still works against the right target.
    good = client.post("/execute", json={**_CONN, "execution_token": exec_token}, headers=_auth(analyst))
    assert good.status_code == 200, good.text


def test_token_expired_rejected(client, analyst, monkeypatch):
    exec_token = _plan_write(client, analyst, monkeypatch)
    # Force the row to be already expired.
    from app_db import execution_tokens
    from app_db.models import ExecutionToken
    from datetime import datetime, timedelta, timezone
    token_id = execution_tokens._hash_token(exec_token)
    with SessionLocal() as db:
        row = db.get(ExecutionToken, token_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    res = client.post("/execute", json={**_CONN, "execution_token": exec_token}, headers=_auth(analyst))
    assert res.status_code == 409
    assert not sql_seen


def test_another_user_cannot_redeem(client, analyst, monkeypatch):
    exec_token = _plan_write(client, analyst, monkeypatch)
    _register(client, "intruder@tok.io")
    intruder = _login(client, "intruder@tok.io")

    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    res = client.post("/execute", json={**_CONN, "execution_token": exec_token}, headers=_auth(intruder))
    assert res.status_code == 400  # generic invalid — never confirms existence
    assert not sql_seen


# ── raw-SQL permission boundary ──────────────────────────────────────────────

def test_raw_write_requires_manual_permission(client, restricted, monkeypatch):
    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    res = client.post("/execute", json={
        **_CONN, "sql": "DELETE FROM orders WHERE id = 1",
    }, headers=_auth(restricted))
    assert res.status_code == 403
    assert not sql_seen


def test_raw_write_allowed_with_manual_permission(client, analyst, monkeypatch):
    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    res = client.post("/execute", json={
        **_CONN, "sql": "DELETE FROM orders WHERE id = 1",
    }, headers=_auth(analyst))
    assert res.status_code == 200, res.text
    assert sql_seen == ["DELETE FROM orders WHERE id = 1"]


def test_raw_read_allowed_without_manual_permission(client, restricted, monkeypatch):
    import dbbuddy_core.db as core_db
    import dbbuddy_core.query as core_query
    monkeypatch.setattr(core_db, "connect_db", lambda *a, **k: MagicMock())
    monkeypatch.setattr(core_query, "execute_query", lambda conn, sql: [{"id": 1}])

    res = client.post("/execute", json={
        **_CONN, "sql": "SELECT * FROM orders",
    }, headers=_auth(restricted))
    assert res.status_code == 200, res.text
    assert res.json()["write"] is False


def test_execute_without_sql_or_token_is_400(client, analyst):
    res = client.post("/execute", json={**_CONN}, headers=_auth(analyst))
    assert res.status_code == 400


# ── saved-connection path (what the frontend actually uses) ───────────────────

def test_token_round_trip_via_saved_connection(client, analyst, monkeypatch):
    """The real UI sends a connection_id (not inline creds) to both /query and
    /execute. Credentials resolve from the saved connection on both legs, so the
    context hash matches and the stored SQL runs."""
    conn = client.post("/connections", json={
        "name": "erp-prod", "engine": "mysql", "host": "erp-prod-host",
        "username": "svc", "password": "s3cret", "database": "erp",
    }, headers=_auth(analyst))
    assert conn.status_code == 201, conn.text
    conn_id = conn.json()["id"]

    _mock_planned_write(monkeypatch, sql="UPDATE orders SET status='void' WHERE id = 7")
    q = client.post("/query", json={
        "connection_id": conn_id, "question": "void order 7",
    }, headers=_auth(analyst))
    assert q.status_code == 200, q.text
    exec_token = q.json().get("execution_token")
    assert exec_token, "expected a token for the connection_id write path"

    sql_seen: list = []
    _mock_execution(monkeypatch, sql_seen)
    res = client.post("/execute", json={
        "connection_id": conn_id, "execution_token": exec_token,
    }, headers=_auth(analyst))
    assert res.status_code == 200, res.text
    assert sql_seen == ["UPDATE orders SET status='void' WHERE id = 7"]

    # A token minted for this connection cannot be redeemed against a different one.
    other = client.post("/connections", json={
        "name": "erp-staging", "engine": "mysql", "host": "staging-host",
        "username": "svc", "password": "s3cret", "database": "erp",
    }, headers=_auth(analyst))
    assert other.status_code == 201, other.text

    _mock_planned_write(monkeypatch, sql="DELETE FROM orders WHERE id = 8")
    q2 = client.post("/query", json={
        "connection_id": conn_id, "question": "delete order 8",
    }, headers=_auth(analyst))
    token2 = q2.json()["execution_token"]
    sql_seen.clear()
    bad = client.post("/execute", json={
        "connection_id": other.json()["id"], "execution_token": token2,
    }, headers=_auth(analyst))
    assert bad.status_code == 409
    assert not sql_seen
