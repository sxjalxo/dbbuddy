"""Personal API keys — create (shown once), list, revoke, and the exchange that
turns a key into a normal JWT pair carrying the owner's live permissions.

Uses the same TestClient bootstrap as the MFA suite (temp SQLite, real app).
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_apikey_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "apikey-test-secret-key-long-enough-1")
os.environ.setdefault("APP_SECRET_KEY", "apikey-test-app-secret")

main = importlib.import_module("main")
from app_db.config import settings as app_settings  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _register(client, email: str) -> str:
    """Register a fresh analyst (default role), return its access token."""
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()["access_token"]


# ── Create / list ─────────────────────────────────────────────────────────────

def test_create_returns_key_once_with_metadata(client):
    token = _register(client, "create@keys.io")
    res = client.post("/auth/keys", json={"name": "laptop"}, headers=_auth(token))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "laptop"
    assert body["api_key"].startswith("dbk_")
    assert body["revoked_at"] is None and body["last_used_at"] is None
    assert body["token_prefix"] and body["token_prefix"] in body["api_key"]


def test_list_never_exposes_the_secret(client):
    token = _register(client, "list@keys.io")
    client.post("/auth/keys", json={"name": "one"}, headers=_auth(token))
    client.post("/auth/keys", json={"name": "two"}, headers=_auth(token))
    res = client.get("/auth/keys", headers=_auth(token))
    assert res.status_code == 200
    keys = res.json()
    assert {k["name"] for k in keys} == {"one", "two"}
    assert all("api_key" not in k and "token_hash" not in k for k in keys)


def test_rename_changes_only_the_label(client):
    token = _register(client, "rename@keys.io")
    created = client.post("/auth/keys", json={"name": "old"}, headers=_auth(token)).json()
    key_id, prefix = created["id"], created["token_prefix"]

    res = client.patch(f"/auth/keys/{key_id}", json={"name": "renamed"}, headers=_auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "renamed"
    # prefix (and thus the secret) is unchanged — only the label moved
    listed = {k["id"]: k for k in client.get("/auth/keys", headers=_auth(token)).json()}
    assert listed[key_id]["token_prefix"] == prefix


def test_rename_is_owner_scoped(client):
    a = _register(client, "rn-a@keys.io")
    b = _register(client, "rn-b@keys.io")
    key_id = client.post("/auth/keys", json={"name": "k"}, headers=_auth(a)).json()["id"]
    assert client.patch(f"/auth/keys/{key_id}", json={"name": "x"}, headers=_auth(b)).status_code == 404


def test_keys_are_scoped_to_the_owner(client):
    a = _register(client, "owner-a@keys.io")
    b = _register(client, "owner-b@keys.io")
    client.post("/auth/keys", json={"name": "a-key"}, headers=_auth(a))
    # b sees none of a's keys
    assert client.get("/auth/keys", headers=_auth(b)).json() == []


# ── Exchange ──────────────────────────────────────────────────────────────────

def test_exchange_returns_tokens_with_owner_permissions(client):
    token = _register(client, "exchange@keys.io")
    raw = client.post("/auth/keys", json={"name": "ci"}, headers=_auth(token)).json()["api_key"]

    res = client.post("/auth/keys/exchange", json={"api_key": raw})
    assert res.status_code == 200, res.text
    access = res.json()["access_token"]
    payload = jwt.decode(access, app_settings.JWT_SECRET, algorithms=[app_settings.JWT_ALGORITHM])
    # Analyst (the default role) carries query:run, and the amr marks the method.
    assert "query:run" in payload["permissions"]
    assert payload["amr"] == ["apikey"]

    # The exchanged token works on a permission-gated endpoint (query:run).
    q = client.post("/query", json={"question": "show users", "host": "localhost",
                                    "user": "x", "password": "y", "database": "z"},
                    headers=_auth(access))
    # It gets past auth/permission (may 500 with no real DB, but never 401/403).
    assert q.status_code not in (401, 403)


def test_exchange_updates_last_used(client):
    token = _register(client, "lastused@keys.io")
    raw = client.post("/auth/keys", json={"name": "k"}, headers=_auth(token)).json()["api_key"]
    assert client.get("/auth/keys", headers=_auth(token)).json()[0]["last_used_at"] is None
    client.post("/auth/keys/exchange", json={"api_key": raw})
    assert client.get("/auth/keys", headers=_auth(token)).json()[0]["last_used_at"] is not None


def test_revoked_key_cannot_be_exchanged(client):
    token = _register(client, "revoke@keys.io")
    created = client.post("/auth/keys", json={"name": "temp"}, headers=_auth(token)).json()
    raw, key_id = created["api_key"], created["id"]
    assert client.post("/auth/keys/exchange", json={"api_key": raw}).status_code == 200

    assert client.delete(f"/auth/keys/{key_id}", headers=_auth(token)).status_code == 204
    assert client.post("/auth/keys/exchange", json={"api_key": raw}).status_code == 401
    # revoked key remains listed, marked revoked
    listed = {k["id"]: k for k in client.get("/auth/keys", headers=_auth(token)).json()}
    assert listed[key_id]["revoked_at"] is not None


def test_revoke_is_idempotent_and_owner_scoped(client):
    a = _register(client, "revk-a@keys.io")
    b = _register(client, "revk-b@keys.io")
    key_id = client.post("/auth/keys", json={"name": "k"}, headers=_auth(a)).json()["id"]
    # b cannot revoke a's key
    assert client.delete(f"/auth/keys/{key_id}", headers=_auth(b)).status_code == 404
    # a revokes twice — both succeed (idempotent)
    assert client.delete(f"/auth/keys/{key_id}", headers=_auth(a)).status_code == 204
    assert client.delete(f"/auth/keys/{key_id}", headers=_auth(a)).status_code == 204


@pytest.mark.parametrize("bad", ["", "not-a-key", "dbk_deadbeef_wrongsecret", "dbk__x", "abc_def_ghi"])
def test_malformed_or_unknown_keys_are_rejected(client, bad):
    assert client.post("/auth/keys/exchange", json={"api_key": bad}).status_code == 401
