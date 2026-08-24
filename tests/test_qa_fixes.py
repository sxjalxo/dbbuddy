"""Regression tests for QA fixes:

* #2 — DELETE dry-run estimate preserves the WHERE clause (accurate blast radius).
* #7 — the auto-execute read path rejects stacked statements.
* #6 — repeated failed logins are throttled (HTTP 429).
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


# ── Fake DB primitives (no live database needed) ──────────────────────────────

class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchall(self):
        return self.rows

    def fetchmany(self, size):
        # Reads are bounded at the cursor now (dbbuddy_core.query._fetch_bounded),
        # so a double has to honour the DB-API's fetchmany like a real driver.
        taken, self.rows = self.rows[:size], self.rows[size:]
        return taken


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self, dictionary=False):
        return self._cur

    def commit(self):
        pass


# ── #2: DELETE dry-run keeps the WHERE clause ─────────────────────────────────

def test_delete_dry_run_preserves_where_clause():
    from dbbuddy_core.execution import get_dry_run_estimate

    conn = _FakeConn([{"COUNT(*)": 3}])
    est = get_dry_run_estimate("DELETE FROM orders WHERE user_id = 5;", conn)

    assert est is not None
    assert est["estimated_rows"] == 3
    executed = conn._cur.executed[0].upper()
    # The count query must be filtered — not a whole-table COUNT(*).
    assert executed.startswith("SELECT COUNT(*) FROM ORDERS")
    assert "WHERE" in executed


def test_delete_dry_run_count_column_name_is_engine_agnostic():
    """A driver that names the count column 'count' (Postgres/SQL Server) still works."""
    from dbbuddy_core.execution import get_dry_run_estimate

    conn = _FakeConn([{"count": 7}])
    est = get_dry_run_estimate("DELETE FROM orders WHERE status = 'x';", conn)
    assert est is not None
    assert est["estimated_rows"] == 7


# ── #7: read path rejects stacked statements ──────────────────────────────────

def test_read_rejects_stacked_statement():
    from dbbuddy_core.query import execute_query

    conn = _FakeConn([])
    with pytest.raises(ValueError):
        execute_query(conn, "SELECT * FROM users; DELETE FROM users;")
    # The dangerous statement must never have been executed.
    assert conn._cur.executed == []


def test_read_allows_single_statement_with_trailing_semicolon():
    from dbbuddy_core.query import execute_query

    conn = _FakeConn([{"id": 1}])
    rows = execute_query(conn, "SELECT * FROM users;")
    assert rows == [{"id": 1}]


# ── WHERE operators: AND / LIKE / BETWEEN ─────────────────────────────────────

def test_and_joins_multiple_conditions():
    from dbbuddy_core.execution import compile_sql, compile_parameterized_sql

    plan = {"base_table": "users", "select": [{"column": "*"}], "where": [
        {"column": "users.country", "operator": "=", "value": "India"},
        {"column": "users.status", "operator": "=", "value": "active"},
    ]}
    # ``status`` is reserved and therefore quoted; ``country`` is not and is left alone.
    assert compile_sql(plan) == (
        "SELECT * FROM users WHERE users.country = 'India' AND users.`status` = 'active'"
    )
    sql, params = compile_parameterized_sql(plan)
    assert sql == "SELECT * FROM users WHERE users.country = %s AND users.`status` = %s"
    assert params == ["India", "active"]


def test_like_operator_compiles():
    from dbbuddy_core.execution import compile_sql, compile_parameterized_sql

    plan = {"base_table": "users", "select": [{"column": "*"}], "where": [
        {"column": "users.name", "operator": "LIKE", "value": "%john%"},
    ]}
    assert compile_sql(plan) == "SELECT * FROM users WHERE users.name LIKE '%john%'"
    sql, params = compile_parameterized_sql(plan)
    assert sql == "SELECT * FROM users WHERE users.name LIKE %s"
    assert params == ["%john%"]


def test_between_operator_compiles_with_two_bounds():
    from dbbuddy_core.execution import compile_sql, compile_parameterized_sql

    plan = {"base_table": "orders", "select": [{"column": "*"}], "where": [
        {"column": "orders.amount", "operator": "BETWEEN", "value": [20, 30]},
        {"column": "orders.status", "operator": "=", "value": "active"},
    ]}
    # Inline: valid "BETWEEN low AND high", not "BETWEEN [20, 30]".
    # ``status`` is in _RESERVED_IDENTIFIERS, so the WHERE column is quoted;
    # ``orders`` and ``amount`` are not, and stay byte-identical. That asymmetry
    # is the point of targeted quoting — see sql/compiler.py:_needs_quoting.
    assert compile_sql(plan) == (
        "SELECT * FROM orders WHERE orders.amount BETWEEN 20 AND 30 "
        "AND orders.`status` = 'active'"
    )
    # Parameterized: two placeholders and two bound params (not a list-in-one-%s).
    sql, params = compile_parameterized_sql(plan)
    assert sql == (
        "SELECT * FROM orders WHERE orders.amount BETWEEN %s AND %s AND orders.`status` = %s"
    )
    assert params == [20, 30, "active"]


def test_between_with_bad_value_aborts_compilation():
    from dbbuddy_core.execution import compile_sql
    from dbbuddy_core.sql import InvalidConditionError

    plan = {"base_table": "orders", "select": [{"column": "*"}], "where": [
        {"column": "orders.amount", "operator": "BETWEEN", "value": 42},  # not a pair
    ]}
    # Fail-closed: rather than silently dropping the predicate and broadening the
    # query, compilation aborts with a typed error.
    with pytest.raises(InvalidConditionError):
        compile_sql(plan)


# ── #6: login throttling ──────────────────────────────────────────────────────

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_qa_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "qa-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "qa-test-app-secret")

main = importlib.import_module("main")


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def test_login_locks_out_after_repeated_failures(client):
    from app_db import login_guard

    email = "throttle@qa.io"
    client.post("/auth/register", json={"email": email, "password": "password123"})

    key = f"testclient::{email}"
    login_guard.reset(key)  # start from a clean slate

    # The allowed number of wrong attempts still return 401 (not throttled yet).
    for _ in range(login_guard.MAX_FAILURES):
        r = client.post("/auth/login", json={"email": email, "password": "wrong"})
        assert r.status_code == 401

    # The next attempt is locked out — before the password is even checked.
    r = client.post("/auth/login", json={"email": email, "password": "wrong"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")

    # Even the correct password is refused while the lockout is in effect.
    r = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 429

    login_guard.reset(key)  # don't leak lockout state into other tests


def test_successful_login_clears_failure_counter(client):
    from app_db import login_guard

    email = "recover@qa.io"
    client.post("/auth/register", json={"email": email, "password": "password123"})
    key = f"testclient::{email}"
    login_guard.reset(key)

    # A few failures, then a success, must reset the counter so we never lock out.
    for _ in range(login_guard.MAX_FAILURES - 1):
        assert client.post("/auth/login", json={"email": email, "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "password123"}).status_code == 200

    # Counter cleared → another full round of failures is needed before lockout.
    for _ in range(login_guard.MAX_FAILURES - 1):
        assert client.post("/auth/login", json={"email": email, "password": "wrong"}).status_code == 401

    login_guard.reset(key)


# ── #7: refresh-token versioning (logout / deactivation revokes refresh) ──────

def test_logout_revokes_refresh_token(client):
    reg = client.post("/auth/register", json={"email": "revoke@qa.io", "password": "password123"})
    tokens = reg.json()
    refresh = tokens["refresh_token"]

    # Before logout the refresh token mints a new pair.
    assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 200

    # Logout bumps the user's token_version → the old refresh token is now dead.
    assert client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ).status_code == 200
    revoked = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert revoked.status_code == 401
    assert "revoked" in revoked.json()["detail"].lower()


def test_deactivation_revokes_refresh_token(client):
    from app_db.database import SessionLocal
    from app_db.models import User

    email = "deact@qa.io"
    refresh = client.post("/auth/register", json={"email": email, "password": "password123"}).json()["refresh_token"]
    assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 200

    # Mimic an admin deactivating the account (is_active off + token_version bump).
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        u.is_active = False
        u.token_version += 1
        db.commit()

    assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401


# ── #8: disabled-account response is the generic 401 (no enumeration oracle) ───

def test_disabled_account_login_is_generic_401(client):
    from app_db.database import SessionLocal
    from app_db.models import User

    email = "disabled@qa.io"
    client.post("/auth/register", json={"email": email, "password": "password123"})
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        u.is_active = False
        db.commit()

    # Correct password on a disabled account must look identical to a bad password:
    # same 401 status and the same generic message — no "account disabled" tell.
    disabled = client.post("/auth/login", json={"email": email, "password": "password123"})
    wrongpw = client.post("/auth/login", json={"email": email, "password": "nope-wrong-xyz"})
    assert disabled.status_code == 401
    assert disabled.json()["detail"] == "Invalid email or password."
    assert disabled.json()["detail"] == wrongpw.json()["detail"]


# ── #9: AI / metrics endpoints require authentication ─────────────────────────

def test_ai_and_metrics_endpoints_require_auth(client):
    # Unauthenticated callers can no longer trigger backend work or read internals.
    assert client.get("/ai-health").status_code == 401
    assert client.get("/api-key").status_code == 401
    assert client.get("/context-metrics").status_code == 401

    # An analyst (has settings:ai) may read the key status.
    access = client.post("/auth/register", json={"email": "aihealth@qa.io", "password": "password123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {access}"}
    assert client.get("/api-key", headers=h).status_code == 200
    assert client.get("/context-metrics", headers=h).status_code == 200


# ── #10: CORS is an allow-list, never a credentialed wildcard ─────────────────

def test_cors_does_not_reflect_arbitrary_origin(client):
    evil = client.get("/", headers={"Origin": "https://evil.example.com"})
    # A non-allow-listed origin must NOT be echoed back (no credentialed wildcard).
    assert evil.headers.get("access-control-allow-origin") != "https://evil.example.com"

    allowed = client.get("/", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ── #11: a too-short JWT secret fails fast at startup ─────────────────────────

def test_short_jwt_secret_rejected(monkeypatch):
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "JWT_SECRET", "too-short")
    with pytest.raises(ValueError, match="too short"):
        Settings()

    # A 32+ byte secret is accepted.
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 32)
    Settings()


# ── #12: environment-aware fail-fast configuration validation ─────────────────

def _prod_config(monkeypatch):
    """Point Settings at a valid production config; individual tests break one axis."""
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "ENV", "production")
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 48)
    monkeypatch.setattr(Settings, "_JWT_SECRET_FROM_ENV", True)
    monkeypatch.setattr(Settings, "APP_SECRET_KEY", "prod-app-secret")
    monkeypatch.setattr(Settings, "APP_DATABASE_URL", "postgresql+psycopg2://u:p@h:5432/db")
    monkeypatch.setattr(Settings, "ALLOWED_ORIGINS", ["https://erp.company.com"])
    monkeypatch.setattr(Settings, "_ALLOWED_ORIGINS_FROM_ENV", True)
    return Settings


def test_valid_production_config_boots(monkeypatch):
    Settings = _prod_config(monkeypatch)
    Settings()  # no raise


def test_production_rejects_dev_defaults_all_at_once(monkeypatch):
    Settings = _prod_config(monkeypatch)
    # Strip every production requirement back to its dev default / unset state.
    monkeypatch.setattr(Settings, "JWT_SECRET", "")
    monkeypatch.setattr(Settings, "_JWT_SECRET_FROM_ENV", False)
    monkeypatch.setattr(Settings, "APP_SECRET_KEY", "")
    monkeypatch.setattr(Settings, "APP_DATABASE_URL", "sqlite:///./dbbuddy_app.db")
    monkeypatch.setattr(Settings, "ALLOWED_ORIGINS", ["http://localhost:5173"])
    monkeypatch.setattr(Settings, "_ALLOWED_ORIGINS_FROM_ENV", False)

    with pytest.raises(ValueError) as exc:
        Settings()
    msg = str(exc.value)
    # Aggregated: every problem is reported in one shot.
    assert "JWT_SECRET must be set" in msg
    assert "APP_SECRET_KEY must be set" in msg
    assert "SQLite" in msg
    assert "ALLOWED_ORIGINS must be set" in msg


def test_unrecognized_env_is_rejected(monkeypatch):
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "ENV", "prod")  # typo → must not silently run as dev
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 48)
    with pytest.raises(ValueError, match="not recognized"):
        Settings()


def test_wildcard_origin_rejected_in_any_env(monkeypatch):
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "ENV", "development")
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 48)
    monkeypatch.setattr(Settings, "ALLOWED_ORIGINS", ["*"])
    with pytest.raises(ValueError, match="must not contain"):
        Settings()


def test_scheme_less_origin_rejected(monkeypatch):
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "ENV", "development")
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 48)
    monkeypatch.setattr(Settings, "ALLOWED_ORIGINS", ["erp.company.com"])  # no scheme
    with pytest.raises(ValueError, match="must include a scheme"):
        Settings()


def test_malformed_database_url_rejected(monkeypatch):
    from app_db.config import Settings

    monkeypatch.setattr(Settings, "ENV", "development")
    monkeypatch.setattr(Settings, "JWT_SECRET", "x" * 48)
    monkeypatch.setattr(Settings, "APP_DATABASE_URL", "not a url")
    with pytest.raises(ValueError, match="not a valid SQLAlchemy URL"):
        Settings()


# ── #13: undecryptable stored connection → clear 409, never a blank 500 ───────

def test_err_detail_is_never_empty():
    from cryptography.fernet import InvalidToken

    # Fernet's InvalidToken stringifies to '' — the helper must not pass that through.
    assert main._err_detail(InvalidToken()) != ""
    assert "InvalidToken" in main._err_detail(InvalidToken())
    # A normal exception keeps its own message.
    assert main._err_detail(ValueError("boom")) == "boom"


def test_analyze_reports_undecryptable_connection_clearly(client, monkeypatch):
    from app_db import config

    access = client.post(
        "/auth/register", json={"email": "decrypt@qa.io", "password": "password123"}
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {access}"}

    # Save a connection — its password is encrypted with the current at-rest key.
    created = client.post(
        "/connections", headers=h,
        json={"name": "c", "engine": "mysql", "host": "127.0.0.1",
              "username": "u", "password": "secret", "database": "d"},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]

    # Rotate the at-rest key (as an ephemeral dev key does on restart): the stored
    # secret can no longer be decrypted.
    monkeypatch.setattr(config.settings, "APP_SECRET_KEY", "a-totally-different-key-value-xx")

    res = client.post("/analyze", headers=h, json={"connection_id": cid, "ai": False})
    # Actionable 409 instead of an opaque, empty-bodied 500.
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail  # never blank
    assert "encryption key" in detail.lower()


# ── #14: connections advertise decryptability; editing can heal a stale one ───

def test_connection_flags_and_edit_heals_stale_credentials(client, monkeypatch):
    from app_db import config

    access = client.post(
        "/auth/register", json={"email": "heal@qa.io", "password": "password123"}
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {access}"}

    created = client.post(
        "/connections", headers=h,
        json={"name": "c", "engine": "mysql", "host": "127.0.0.1",
              "username": "u", "password": "secret", "database": "d"},
    )
    assert created.status_code == 201
    cid = created.json()["id"]
    assert created.json()["credentials_ok"] is True

    # Rotate the at-rest key → the stored secret is now undecryptable, and the
    # list endpoint surfaces that up front (no need to run a query to discover it).
    monkeypatch.setattr(config.settings, "APP_SECRET_KEY", "rotated-key-value-abcdefghij")
    listed = client.get("/connections", headers=h).json()
    row = next(c for c in listed if c["id"] == cid)
    assert row["credentials_ok"] is False

    # Editing a non-password field with a blank password leaves the secret stale.
    patched = client.patch(f"/connections/{cid}", headers=h, json={"name": "renamed"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "renamed"
    assert patched.json()["credentials_ok"] is False

    # Re-entering the password re-encrypts under the current key → healed.
    healed = client.patch(f"/connections/{cid}", headers=h, json={"password": "new-secret"})
    assert healed.status_code == 200
    assert healed.json()["credentials_ok"] is True
