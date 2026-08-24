"""Password reset: request a link, use it once, and every old session dies.

Until now there was no reset at all. A user who forgot their password needed an
admin with database access to edit a hash by hand, which is why this was the
single biggest blocker to anyone self-hosting.

The properties that matter are all about what the flow must *not* do:

* **not confirm whether an email exists.** The request endpoint answers the same
  way either way. An unauthenticated enumeration oracle on the account list is a
  worse leak than the inconvenience of a silent failure.
* **not let a token be used twice**, or after it expires.
* **not leave the old sessions alive.** A reset usually means "someone else may
  have my password", so every outstanding access and refresh token must stop
  working — which `token_version` already does for the whole system.
* **not store the token.** Only its hash, like API keys and recovery codes.
"""

import importlib
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_reset_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "reset-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "reset-test-app-secret")

main = importlib.import_module("main")

from app_db import email as email_module  # noqa: E402
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import PasswordResetToken, User  # noqa: E402
from app_db.security import hash_reset_token, verify_password  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Capture what would have been sent instead of sending it."""
    sent = []
    monkeypatch.setattr(email_module, "send_password_reset",
                        lambda **kwargs: sent.append(kwargs) or True)
    return sent


def _register(client: TestClient, email: str, password: str = "Original#12345"):
    res = client.post("/auth/register", json={"email": email, "password": password})
    assert res.status_code == 201, res.text
    return res.json()


def _request_reset(client: TestClient, email: str):
    return client.post("/auth/password-reset/request", json={"email": email})


def _token_from(outbox) -> str:
    assert outbox, "no email was sent"
    token = outbox[-1].get("token")
    assert token, f"the sender was not given a token: {outbox[-1]}"
    return token


# ── The request endpoint ─────────────────────────────────────────────────────

def test_request_for_a_known_address_sends_a_link(client, outbox):
    _register(client, "reset-known@example.com")
    res = _request_reset(client, "reset-known@example.com")
    assert res.status_code == 202
    assert len(outbox) == 1
    assert outbox[0]["to"] == "reset-known@example.com"


def test_request_for_an_unknown_address_looks_identical(client, outbox):
    """No enumeration oracle: same status, same body, and nothing sent."""
    known = _request_reset(client, "reset-known@example.com")
    unknown = _request_reset(client, "definitely-not-a-user@example.com")

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert not any(m["to"] == "definitely-not-a-user@example.com" for m in outbox)


def test_request_is_case_insensitive_on_the_address(client, outbox):
    _register(client, "reset-case@example.com")
    assert _request_reset(client, "Reset-Case@Example.COM").status_code == 202
    assert outbox and outbox[-1]["to"] == "reset-case@example.com"


def test_only_the_hash_is_stored(client, outbox):
    _register(client, "reset-hash@example.com")
    _request_reset(client, "reset-hash@example.com")
    token = _token_from(outbox)

    with SessionLocal() as db:
        rows = db.query(PasswordResetToken).all()
        stored = [r.token_hash for r in rows]
    assert token not in stored
    assert hash_reset_token(token) in stored


# ── The confirm endpoint ─────────────────────────────────────────────────────

def test_a_valid_token_changes_the_password(client, outbox):
    _register(client, "reset-ok@example.com")
    _request_reset(client, "reset-ok@example.com")
    token = _token_from(outbox)

    res = client.post("/auth/password-reset/confirm",
                      json={"token": token, "new_password": "Replaced#12345"})
    assert res.status_code == 200, res.text

    assert client.post("/auth/login", json={
        "email": "reset-ok@example.com", "password": "Original#12345"}).status_code == 401
    assert client.post("/auth/login", json={
        "email": "reset-ok@example.com", "password": "Replaced#12345"}).status_code == 200

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "reset-ok@example.com").one()
        assert verify_password("Replaced#12345", user.password_hash)


def test_a_token_works_only_once(client, outbox):
    _register(client, "reset-once@example.com")
    _request_reset(client, "reset-once@example.com")
    token = _token_from(outbox)

    first = client.post("/auth/password-reset/confirm",
                        json={"token": token, "new_password": "First#12345"})
    assert first.status_code == 200
    second = client.post("/auth/password-reset/confirm",
                         json={"token": token, "new_password": "Second#12345"})
    assert second.status_code == 400

    # The second attempt must not have taken effect.
    assert client.post("/auth/login", json={
        "email": "reset-once@example.com", "password": "First#12345"}).status_code == 200


def test_an_expired_token_is_refused(client, outbox):
    _register(client, "reset-expired@example.com")
    _request_reset(client, "reset-expired@example.com")
    token = _token_from(outbox)

    with SessionLocal() as db:
        row = db.query(PasswordResetToken).filter(
            PasswordResetToken.token_hash == hash_reset_token(token)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    res = client.post("/auth/password-reset/confirm",
                      json={"token": token, "new_password": "TooLate#12345"})
    assert res.status_code == 400
    assert client.post("/auth/login", json={
        "email": "reset-expired@example.com", "password": "Original#12345"}).status_code == 200


def test_an_unknown_token_is_refused(client):
    res = client.post("/auth/password-reset/confirm",
                      json={"token": "not-a-real-token", "new_password": "Whatever#12345"})
    assert res.status_code == 400


def test_a_short_password_is_refused(client, outbox):
    _register(client, "reset-short@example.com")
    _request_reset(client, "reset-short@example.com")
    token = _token_from(outbox)
    res = client.post("/auth/password-reset/confirm",
                      json={"token": token, "new_password": "short"})
    assert res.status_code == 422


# ── Sessions ─────────────────────────────────────────────────────────────────

def test_reset_revokes_every_existing_session(client, outbox):
    """A reset usually means the old password may be known to someone else."""
    _register(client, "reset-sessions@example.com")
    login = client.post("/auth/login", json={
        "email": "reset-sessions@example.com", "password": "Original#12345"}).json()
    old_access, old_refresh = login["access_token"], login["refresh_token"]

    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {old_access}"}).status_code == 200

    _request_reset(client, "reset-sessions@example.com")
    client.post("/auth/password-reset/confirm",
                json={"token": _token_from(outbox), "new_password": "Rotated#12345"})

    assert client.get("/auth/me",
                      headers={"Authorization": f"Bearer {old_access}"}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401


def test_outstanding_reset_tokens_are_invalidated_by_a_reset(client, outbox):
    """Two requests, then one used — the other must die with it.

    Otherwise a link mailed to a compromised inbox stays live after the real
    owner has already recovered the account.
    """
    _register(client, "reset-two@example.com")
    _request_reset(client, "reset-two@example.com")
    first = _token_from(outbox)
    _request_reset(client, "reset-two@example.com")
    second = _token_from(outbox)
    assert first != second

    assert client.post("/auth/password-reset/confirm",
                       json={"token": second, "new_password": "Winner#12345"}).status_code == 200
    assert client.post("/auth/password-reset/confirm",
                       json={"token": first, "new_password": "Loser#12345"}).status_code == 400


def test_an_inactive_account_gets_no_link(client, outbox):
    """Deactivated accounts must not be recoverable by their former owner."""
    _register(client, "reset-inactive@example.com")
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "reset-inactive@example.com").one()
        user.is_active = False
        db.commit()

    res = _request_reset(client, "reset-inactive@example.com")
    assert res.status_code == 202          # still no enumeration signal
    assert not any(m["to"] == "reset-inactive@example.com" for m in outbox)


# ── Throttling ───────────────────────────────────────────────────────────────

def test_requests_are_throttled_per_address(client, outbox):
    """Unthrottled, this endpoint mails a third party on demand.

    That makes it a way to flood someone's inbox using this server's reputation,
    and to burn its SMTP quota. Counted per (ip, address) whether or not the
    account exists — throttling only real addresses would make the 429 an
    enumeration signal in itself.
    """
    from app_db.login_guard import MAX_RESET_REQUESTS, clear_all

    clear_all()
    _register(client, "reset-throttle@example.com")

    for _ in range(MAX_RESET_REQUESTS):
        assert _request_reset(client, "reset-throttle@example.com").status_code == 202

    blocked = _request_reset(client, "reset-throttle@example.com")
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    clear_all()


def test_throttling_does_not_reveal_whether_an_account_exists(client):
    """An unknown address hits the same wall, at the same count."""
    from app_db.login_guard import MAX_RESET_REQUESTS, clear_all

    clear_all()
    for _ in range(MAX_RESET_REQUESTS):
        assert _request_reset(client, "ghost@example.com").status_code == 202
    assert _request_reset(client, "ghost@example.com").status_code == 429
    clear_all()
