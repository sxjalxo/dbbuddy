"""`/execute` must not report a write it did not persist.

The endpoint enables autocommit so a write does not sit holding locks, and it
wrapped that in a bare `except Exception: pass`. On PostgreSQL the call raised —
`connect_db` had left a transaction open — and the swallow turned a broken
connection state into silence: the statement ran inside a transaction nobody
committed, disappeared when the connection closed, and the response still said
"1 row(s) affected".

The root cause is fixed in the PostgreSQL dialect (it commits the `SET
statement_timeout` it issues). This is the second line: if autocommit cannot be
enabled for any reason, on any engine, a write is committed explicitly rather
than assumed, and the failure is logged instead of discarded.

A driver that cannot do autocommit at all is a real possibility — that is why the
call is defensive in the first place. What is not acceptable is not knowing.
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_execdur_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "execdur-test-secret-key-long-enough-1")
os.environ.setdefault("APP_SECRET_KEY", "execdur-test-app-secret")

main = importlib.import_module("main")


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    # Module-scoped: registering the same address once per test would 409 on the
    # second, and every test here wants the same ordinary account.
    res = client.post("/auth/register",
                      json={"email": "exec@durability.io", "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()["access_token"]


class StubConnection:
    """A connection whose autocommit either works or refuses, like psycopg2's."""

    def __init__(self, *, autocommit_raises=False):
        self._autocommit = False
        self._autocommit_raises = autocommit_raises
        self.commits = 0
        self.closed = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self._autocommit_raises:
            raise RuntimeError("set_session cannot be used inside a transaction")
        self._autocommit = value

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _run(client, token, monkeypatch, conn, *, rows_affected=1):
    import dbbuddy_core.db as core_db
    import dbbuddy_core.query as core_query

    monkeypatch.setattr(core_db, "connect_db", lambda *a, **k: conn)
    monkeypatch.setattr(core_query, "execute_query",
                        lambda _c, _sql: [{"rows_affected": rows_affected}]
                        if rows_affected is not None else [{"id": 1}])
    return client.post("/execute", json={
        "host": "h", "user": "u", "password": "p", "database": "d",
        "engine": "postgresql", "sql": "UPDATE orders SET total = 1 WHERE id = 1",
    }, headers={"Authorization": f"Bearer {token}"})


def test_a_write_is_committed_when_autocommit_cannot_be_enabled(client, token, monkeypatch):
    conn = StubConnection(autocommit_raises=True)
    res = _run(client, token, monkeypatch, conn)
    assert res.status_code == 200, res.text
    assert conn.commits >= 1, "reported a write it never committed"


def test_a_write_is_not_double_committed_when_autocommit_works(client, token, monkeypatch):
    # With autocommit on, the statement is already its own transaction; an extra
    # commit is noise at best and a second round trip per write at worst.
    conn = StubConnection()
    res = _run(client, token, monkeypatch, conn)
    assert res.status_code == 200, res.text
    assert conn.autocommit is True
    assert conn.commits == 0


def test_the_failure_to_enable_autocommit_is_logged(client, token, monkeypatch, caplog):
    conn = StubConnection(autocommit_raises=True)
    with caplog.at_level("WARNING"):
        _run(client, token, monkeypatch, conn)
    assert any("autocommit" in r.getMessage().lower() for r in caplog.records), \
        "the swallow is what hid a data-loss bug; it must at least say so"


def test_a_read_does_not_commit(client, token, monkeypatch):
    # Nothing to persist, and a commit on a read-only connection is a pointless
    # round trip on every query.
    conn = StubConnection(autocommit_raises=True)
    res = _run(client, token, monkeypatch, conn, rows_affected=None)
    assert res.status_code == 200, res.text
    assert conn.commits == 0
