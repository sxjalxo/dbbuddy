"""Regression tests for the pre-deployment QA hardening pass.

Covers:
* #1/#2 — /execute classifies query safety and writes a server-side audit row.
* #3    — chart create forces status=draft and rejects unknown chart_type.
* #4    — free-text fields are stripped of NUL / control bytes (Postgres-safe).
* #5    — self-registration is throttled per IP and honors a domain allow-list.
* #9    — text inputs are length-capped to their column width (Postgres-safe).
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_qa_hardening.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "qa-hardening-secret-key-long-enough-123456")
os.environ.setdefault("APP_SECRET_KEY", "qa-hardening-app-secret")

main = importlib.import_module("main")
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import AuditLog  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str) -> None:
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code in (201, 409), res.text


def _login(client, email: str) -> str:
    res = client.post("/auth/login", json={"email": email, "password": "password123"})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    _register(client, "hardening@qa.io")  # analyst → chart:save, query:run
    return _login(client, "hardening@qa.io")


# ── #3 chart status / chart_type validation ───────────────────────────────────

def test_chart_status_forced_to_draft(client, token):
    """A client-supplied status="published" must not bypass the publish flow."""
    res = client.post("/charts", json={
        "title": "Sneaky", "sql": "SELECT 1", "status": "published",
    }, headers=_auth(token))
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "draft"


def test_chart_type_rejects_unknown_value(client, token):
    res = client.post("/charts", json={
        "title": "Bad type", "sql": "SELECT 1", "chart_type": "wat",
    }, headers=_auth(token))
    assert res.status_code == 422, res.text


def test_chart_type_accepts_known_value(client, token):
    res = client.post("/charts", json={
        "title": "Good type", "sql": "SELECT 1", "chart_type": "line",
    }, headers=_auth(token))
    assert res.status_code == 201, res.text
    assert res.json()["chart_type"] == "line"


# ── #4 NUL / control-byte sanitation ──────────────────────────────────────────

def test_nul_bytes_stripped_from_text_fields(client, token):
    res = client.post("/charts", json={
        "title": "Q1\x00 Revenue", "nl_query": "show\x00 sales", "sql": "SELECT 1",
    }, headers=_auth(token))
    assert res.status_code == 201, res.text
    body = res.json()
    assert "\x00" not in body["title"] and body["title"] == "Q1 Revenue"
    assert "\x00" not in (body["nl_query"] or "")


# ── #5 registration throttle + domain allow-list ──────────────────────────────

def test_registration_is_throttled_per_ip(client):
    from app_db import login_guard

    login_guard.clear_all()
    # The first MAX_REGISTRATIONS attempts are allowed; the next is locked out.
    for i in range(login_guard.MAX_REGISTRATIONS):
        r = client.post("/auth/register", json={"email": f"flood{i}@qa.io", "password": "password123"})
        assert r.status_code in (201, 409), r.text
    blocked = client.post("/auth/register", json={"email": "flood-extra@qa.io", "password": "password123"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    login_guard.clear_all()


def test_registration_domain_allowlist(client, monkeypatch):
    from app_db import login_guard
    from app_db.config import settings

    login_guard.clear_all()
    monkeypatch.setattr(settings, "REGISTRATION_ALLOWED_DOMAINS", ["allowed.io"])
    rejected = client.post("/auth/register", json={"email": "user@blocked.io", "password": "password123"})
    assert rejected.status_code == 403
    accepted = client.post("/auth/register", json={"email": "user@allowed.io", "password": "password123"})
    assert accepted.status_code in (201, 409), accepted.text
    login_guard.clear_all()


# ── #1/#2 /execute audits and classifies ──────────────────────────────────────

def test_execute_audits_and_classifies_write(client, token, monkeypatch):
    """A raw /execute write is recorded with its safety category and row count."""
    import dbbuddy_core.db as core_db
    import dbbuddy_core.query as core_query

    fake_conn = MagicMock()
    monkeypatch.setattr(core_db, "connect_db", lambda *a, **k: fake_conn)
    monkeypatch.setattr(core_query, "execute_query", lambda conn, sql: [{"rows_affected": 3}])

    res = client.post("/execute", json={
        "host": "db", "user": "u", "password": "p", "database": "d",
        "engine": "mysql", "sql": "DELETE FROM orders WHERE id = 1",
    }, headers=_auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["write"] is True and res.json()["rows_affected"] == 3

    with SessionLocal() as db:
        row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "execute", AuditLog.entity_type == "db_query")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        assert row is not None, "expected an audit row for the /execute call"
        assert row.detail.get("safety_category") == "write"
        assert row.detail.get("write") is True
        assert row.detail.get("rows_affected") == 3
        assert "DELETE FROM orders" in row.detail.get("sql", "")


# ── #9 length caps match column widths ────────────────────────────────────────
#
# A value longer than its column is stored happily by SQLite (dev + test) and
# rejected by PostgreSQL (the production app DB) with StringDataRightTruncation
# — a 500 that no SQLite-backed test can see. This is the same dev/prod split
# the NUL stripping above exists for, so the caps are asserted the same way:
# against the column widths themselves, not a hand-copied list that can drift.

_LONG = "A" * 100_000


@pytest.mark.parametrize("path, body, field", [
    ("/charts", {"sql": "SELECT 1"}, "title"),
    ("/connections", {"engine": "mysql", "host": "h", "username": "u",
                      "password": "p", "database": "d"}, "name"),
    ("/connections", {"name": "n", "engine": "mysql", "username": "u",
                      "password": "p", "database": "d"}, "host"),
    ("/connections", {"name": "n", "engine": "mysql", "host": "h",
                      "password": "p", "database": "d"}, "username"),
    ("/connections", {"name": "n", "engine": "mysql", "host": "h",
                      "username": "u", "password": "p"}, "database"),
])
def test_oversized_text_is_rejected_not_500(client, token, path, body, field):
    res = client.post(path, json={**body, field: _LONG}, headers=_auth(token))
    assert res.status_code == 422, f"{path}.{field} -> {res.status_code}: {res.text[:200]}"


def test_oversized_full_name_is_rejected_not_500(client):
    res = client.post("/auth/register", json={
        "email": "toolong@dbbuddy.io", "password": "password123", "full_name": _LONG,
    })
    assert res.status_code == 422, res.text


def test_chart_title_boundary_is_allowed(client, token):
    """The cap is the column width, not narrower — a title that fits must save."""
    from app_db.models import SavedChart

    width = SavedChart.__table__.c.title.type.length
    res = client.post("/charts", json={"title": "A" * width, "sql": "SELECT 1"},
                      headers=_auth(token))
    assert res.status_code == 201, res.text
    assert len(res.json()["title"]) == width

    over = client.post("/charts", json={"title": "A" * (width + 1), "sql": "SELECT 1"},
                       headers=_auth(token))
    assert over.status_code == 422, over.text


def test_input_caps_cover_every_stored_text_column():
    """Guard against drift: any str field written straight to a bounded column
    must declare a max_length no wider than that column."""
    from annotated_types import MaxLen

    from app_db import models, schemas

    # (schema, model, fields whose value is NOT stored verbatim in a like-named
    # column — hashed/encrypted secrets, enum-normalized values, and ids).
    pairs = [
        (schemas.ChartIn, models.SavedChart, {"sql", "nl_query", "status", "chart_type",
                                              "schema_fingerprint", "database_connection_id"}),
        (schemas.ChartUpdate, models.SavedChart, {"chart_type"}),
        (schemas.ConnectionIn, models.DatabaseConnection, {"password", "engine"}),
        (schemas.ConnectionUpdate, models.DatabaseConnection, {"password", "engine"}),
        (schemas.RegisterRequest, models.User, {"password", "email"}),
        (schemas.AdminUserCreate, models.User, {"password", "email", "role", "organization_id"}),
        (schemas.JobIn, models.ScheduledJob, {"job_type", "target_ref", "schedule_kind"}),
        (schemas.JobPatch, models.ScheduledJob, {"schedule_kind"}),
        (schemas.AIProviderIn, models.AIProviderConfig, {"adapter", "api_key", "fallback_provider_id"}),
        (schemas.AIProviderUpdate, models.AIProviderConfig, {"adapter", "api_key", "fallback_provider_id"}),
    ]

    checked, problems = 0, []
    for schema, model, skip in pairs:
        columns = model.__table__.c
        for name, field in schema.model_fields.items():
            if name in skip or name not in columns:
                continue
            width = getattr(columns[name].type, "length", None)
            if width is None:  # Text column — unbounded, nothing to cap.
                continue
            declared = next((m.max_length for m in field.metadata if isinstance(m, MaxLen)), None)
            if declared is None or declared > width:
                problems.append(
                    f"{schema.__name__}.{name}: max_length={declared} but "
                    f"{model.__name__}.{name} is {width} chars"
                )
            checked += 1

    assert checked, "guard matched no fields — the schema/model pairing has drifted"
    assert not problems, "unbounded input into a bounded column:\n  " + "\n  ".join(problems)
