"""Email verification — off by default, and never retroactive.

Two decisions carry this feature, and both are about not breaking people:

**Off unless asked for.** `REQUIRE_EMAIL_VERIFICATION` defaults to false, so an
existing install, the Docker demo, and every developer keep working exactly as
before. A hosted deployment that wants proof of address turns it on. This matches
the rest of the config: dev-friendly defaults, production opts in explicitly.

**Existing accounts count as verified.** The migration backfills every row that
predates the column. Retroactively locking out an entire user base on upgrade is
not a security improvement, it is an outage — the accounts were created under the
rules that applied at the time, and changing that on their behalf is not this
feature's job.

What verification gates, when it is on, is *signing in*. Registration still
creates the account (silently refusing would be indistinguishable from a broken
form), it just does not hand back a session.
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

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_verify_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "verify-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "verify-test-app-secret")

main = importlib.import_module("main")

from app_db import email as email_module  # noqa: E402
from app_db.config import settings as app_settings  # noqa: E402
from app_db.database import SessionLocal  # noqa: E402
from app_db.models import EmailVerificationToken, User  # noqa: E402
from app_db.security import hash_reset_token  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(email_module, "send_email_verification",
                        lambda **kwargs: sent.append(kwargs) or True)
    return sent


@pytest.fixture
def verification_required(monkeypatch):
    monkeypatch.setattr(app_settings, "REQUIRE_EMAIL_VERIFICATION", True)


def _register(client, email: str, password: str = "Original#12345"):
    return client.post("/auth/register", json={"email": email, "password": password})


def _login(client, email: str, password: str = "Original#12345"):
    return client.post("/auth/login", json={"email": email, "password": password})


def _token_from(outbox) -> str:
    assert outbox, "no verification email was sent"
    return outbox[-1]["token"]


# ── Default: the feature is invisible ────────────────────────────────────────

def test_registration_still_returns_a_session_by_default(client):
    res = _register(client, "verify-off@example.com")
    assert res.status_code == 201
    assert res.json()["access_token"]


def test_login_is_not_gated_by_default(client):
    _register(client, "verify-off-login@example.com")
    assert _login(client, "verify-off-login@example.com").status_code == 200


def test_a_link_is_still_sent_so_addresses_can_be_confirmed_later(client, outbox):
    """Off means "not enforced", not "not offered"."""
    _register(client, "verify-off-mail@example.com")
    assert any(m["to"] == "verify-off-mail@example.com" for m in outbox)


# ── Enforced ─────────────────────────────────────────────────────────────────

def test_registration_creates_the_account_but_withholds_the_session(
    client, verification_required, outbox
):
    res = _register(client, "verify-on@example.com")
    assert res.status_code == 202
    assert "access_token" not in res.json()

    with SessionLocal() as db:
        assert db.query(User).filter(User.email == "verify-on@example.com").one_or_none()


def test_login_is_refused_until_verified(client, verification_required, outbox):
    _register(client, "verify-gate@example.com")
    res = _login(client, "verify-gate@example.com")
    assert res.status_code == 403
    # The message has to point at the fix, not just refuse.
    detail = res.json()["detail"].lower()
    assert "confirm" in detail and "email" in detail


def test_confirming_the_token_unlocks_sign_in(client, verification_required, outbox):
    _register(client, "verify-flow@example.com")
    token = _token_from(outbox)

    confirmed = client.post("/auth/verify-email/confirm", json={"token": token})
    assert confirmed.status_code == 200
    assert _login(client, "verify-flow@example.com").status_code == 200

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "verify-flow@example.com").one()
        assert user.email_verified is True
        assert user.email_verified_at is not None


def test_a_verification_token_works_only_once(client, verification_required, outbox):
    _register(client, "verify-once@example.com")
    token = _token_from(outbox)
    assert client.post("/auth/verify-email/confirm", json={"token": token}).status_code == 200
    assert client.post("/auth/verify-email/confirm", json={"token": token}).status_code == 400


def test_an_expired_token_is_refused(client, verification_required, outbox):
    _register(client, "verify-expired@example.com")
    token = _token_from(outbox)

    with SessionLocal() as db:
        row = db.query(EmailVerificationToken).filter(
            EmailVerificationToken.token_hash == hash_reset_token(token)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    assert client.post("/auth/verify-email/confirm", json={"token": token}).status_code == 400
    assert _login(client, "verify-expired@example.com").status_code == 403


def test_an_unknown_token_is_refused(client):
    res = client.post("/auth/verify-email/confirm", json={"token": "nope"})
    assert res.status_code == 400


# ── Resending ────────────────────────────────────────────────────────────────

def test_a_link_can_be_resent(client, verification_required, outbox):
    _register(client, "verify-resend@example.com")
    first = _token_from(outbox)

    res = client.post("/auth/verify-email/request",
                      json={"email": "verify-resend@example.com"})
    assert res.status_code == 202
    second = _token_from(outbox)
    assert second != first

    # The older link is spent, exactly as password reset does it.
    assert client.post("/auth/verify-email/confirm", json={"token": first}).status_code == 400
    assert client.post("/auth/verify-email/confirm", json={"token": second}).status_code == 200


def test_resend_does_not_reveal_whether_an_account_exists(client, outbox):
    known = client.post("/auth/verify-email/request", json={"email": "verify-off@example.com"})
    unknown = client.post("/auth/verify-email/request", json={"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert not any(m["to"] == "ghost@example.com" for m in outbox)


def test_resending_for_an_already_verified_account_sends_nothing(
    client, verification_required, outbox
):
    _register(client, "verify-done@example.com")
    client.post("/auth/verify-email/confirm", json={"token": _token_from(outbox)})

    before = len(outbox)
    res = client.post("/auth/verify-email/request", json={"email": "verify-done@example.com"})
    assert res.status_code == 202          # still no signal
    assert len(outbox) == before


# ── Upgrade safety ───────────────────────────────────────────────────────────

def test_an_account_predating_the_feature_can_still_sign_in(
    client, verification_required, outbox
):
    """The migration backfills existing rows; simulate one that was never verified.

    Turning enforcement on must not lock out a user base that registered under the
    old rules — that is an outage, not a security improvement.
    """
    _register(client, "verify-legacy@example.com")
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "verify-legacy@example.com").one()
        user.email_verified = True          # what the migration does to old rows
        db.commit()

    assert _login(client, "verify-legacy@example.com").status_code == 200
