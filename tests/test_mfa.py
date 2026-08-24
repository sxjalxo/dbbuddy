"""Milestone 5 — MFA / 2FA (TOTP + recovery codes + challenge flow + amr claim).

Drives the real enrollment → challenge → second-factor flow using live pyotp
codes: setup, verify-to-enable, login returns a challenge, exchange a TOTP code
(amr=["pwd","otp"]), single-use recovery codes, disable, and wrong-code rejection.
"""

import importlib
import os
import pathlib
import sys
import tempfile

import jwt
import pyotp
import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_mfa_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "mfa-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "mfa-test-app-secret")

main = importlib.import_module("main")
from app_db.config import settings as app_settings  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _new_user(client, email: str) -> str:
    """Register a fresh user, return its access token (password-only session)."""
    res = client.post("/auth/register", json={"email": email, "password": "password123"})
    assert res.status_code == 201, res.text
    return res.json()["access_token"]


def _enroll(client, token: str) -> tuple[str, list[str]]:
    """Run setup + verify; return (totp_secret, recovery_codes)."""
    setup = client.post("/auth/mfa/setup", headers=_auth(token))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://")
    assert "<svg" in setup.json()["qr_svg"]

    code = pyotp.TOTP(secret).now()
    verify = client.post("/auth/mfa/verify", json={"code": code}, headers=_auth(token))
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["enabled"] is True and len(body["recovery_codes"]) == 10
    return secret, body["recovery_codes"]


# ── Enrollment ───────────────────────────────────────────────────────────────

def test_setup_and_verify_enables_mfa(client):
    token = _new_user(client, "enroll@mfa.io")
    assert client.get("/auth/me", headers=_auth(token)).json()["mfa_enabled"] is False
    _enroll(client, token)
    assert client.get("/auth/me", headers=_auth(token)).json()["mfa_enabled"] is True


def test_verify_rejects_wrong_code(client):
    token = _new_user(client, "badcode@mfa.io")
    assert client.post("/auth/mfa/setup", headers=_auth(token)).status_code == 200
    res = client.post("/auth/mfa/verify", json={"code": "000000"}, headers=_auth(token))
    assert res.status_code == 400
    assert client.get("/auth/me", headers=_auth(token)).json()["mfa_enabled"] is False


# ── Login challenge flow ─────────────────────────────────────────────────────

def test_login_returns_challenge_when_mfa_enabled(client):
    token = _new_user(client, "challenge@mfa.io")
    _enroll(client, token)
    res = client.post("/auth/login", json={"email": "challenge@mfa.io", "password": "password123"})
    assert res.status_code == 200
    body = res.json()
    assert body["mfa_required"] is True
    assert body["challenge_token"] and body["access_token"] is None


def test_mfa_login_with_totp_sets_amr(client):
    token = _new_user(client, "totp@mfa.io")
    secret, _ = _enroll(client, token)
    challenge = client.post("/auth/login", json={"email": "totp@mfa.io", "password": "password123"}).json()["challenge_token"]
    res = client.post("/auth/mfa/login", json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 200, res.text
    access = res.json()["access_token"]
    payload = jwt.decode(access, app_settings.JWT_SECRET, algorithms=[app_settings.JWT_ALGORITHM])
    assert payload["amr"] == ["pwd", "otp"]


def test_mfa_login_rejects_bad_code(client):
    token = _new_user(client, "wrong@mfa.io")
    _enroll(client, token)
    challenge = client.post("/auth/login", json={"email": "wrong@mfa.io", "password": "password123"}).json()["challenge_token"]
    res = client.post("/auth/mfa/login", json={"challenge_token": challenge, "code": "000000"})
    assert res.status_code == 401


# ── Recovery codes (single use) ──────────────────────────────────────────────

def test_recovery_code_logs_in_and_is_single_use(client):
    token = _new_user(client, "recovery@mfa.io")
    _secret, codes = _enroll(client, token)
    one = codes[0]

    def login_with(code):
        ch = client.post("/auth/login", json={"email": "recovery@mfa.io", "password": "password123"}).json()["challenge_token"]
        return client.post("/auth/mfa/login", json={"challenge_token": ch, "code": code})

    first = login_with(one)
    assert first.status_code == 200
    payload = jwt.decode(first.json()["access_token"], app_settings.JWT_SECRET, algorithms=[app_settings.JWT_ALGORITHM])
    assert payload["amr"] == ["pwd", "recovery"]
    # the same code cannot be reused
    assert login_with(one).status_code == 401


# ── Disable ──────────────────────────────────────────────────────────────────

def test_disable_requires_reauth_then_clears_mfa(client):
    token = _new_user(client, "disable@mfa.io")
    _enroll(client, token)
    # wrong password is rejected
    assert client.post("/auth/mfa/disable", json={"password": "nope"}, headers=_auth(token)).status_code == 401
    # correct password disables
    ok = client.post("/auth/mfa/disable", json={"password": "password123"}, headers=_auth(token))
    assert ok.status_code == 200
    # Disabling a second factor is a security event: it revokes every token minted
    # before it, including the one that made this call. The response carries a
    # fresh pair so the caller who just re-authenticated stays signed in.
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401
    token = ok.json()["access_token"]
    assert client.get("/auth/me", headers=_auth(token)).json()["mfa_enabled"] is False
    # login no longer challenges
    res = client.post("/auth/login", json={"email": "disable@mfa.io", "password": "password123"})
    assert res.json()["mfa_required"] is False and res.json()["access_token"]


def test_password_only_login_has_pwd_amr(client):
    _new_user(client, "plain@mfa.io")
    res = client.post("/auth/login", json={"email": "plain@mfa.io", "password": "password123"})
    payload = jwt.decode(res.json()["access_token"], app_settings.JWT_SECRET, algorithms=[app_settings.JWT_ALGORITHM])
    assert payload["amr"] == ["pwd"]
