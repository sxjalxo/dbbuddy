"""The refresh token belongs in an httpOnly cookie, not in `localStorage`.

Both tokens used to live in `localStorage`, which meant any XSS anywhere in 16k
lines of frontend was a full account takeover — and, because the refresh token was
there too, a takeover that outlived the 15-minute access token. The account can
query connected business databases, so that is the highest-value thing the
frontend holds.

The fix has to keep two clients happy at once:

* the **browser**, which should never be able to read the refresh token from
  script — it goes in an httpOnly cookie, and the access token lives in memory;
* the **CLI**, which has no cookie jar and stores tokens in a file. It must keep
  working exactly as before.

So the mode is *opt-in per request*: a client that sends `X-Auth-Mode: cookie`
gets the cookie and no `refresh_token` in the body. Everything else gets today's
response, unchanged. Explicit beats sniffing the User-Agent.

Cookie mode needs CSRF protection, because a cookie is attached by the browser
whether or not the request was intended. `SameSite=Strict` already blocks the
cross-site case; the double-submit token is the belt to that pair of braces, and
it is cheap here because exactly one endpoint reads the cookie — `/auth/refresh`.
Everything else authenticates from the `Authorization` header, which a
cross-origin form cannot set.
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
for _p in (str(_REPO_ROOT), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_cookie_test.db")
os.close(_DB_FD)
os.environ.setdefault("APP_DATABASE_URL", "sqlite:///" + _DB_PATH.replace("\\", "/"))
os.environ.setdefault("JWT_SECRET", "cookie-test-secret-key-long-enough-1234")
os.environ.setdefault("APP_SECRET_KEY", "cookie-test-app-secret")

main = importlib.import_module("main")

from app_db.cookies import CSRF_COOKIE, REFRESH_COOKIE  # noqa: E402

COOKIE_MODE = {"X-Auth-Mode": "cookie"}


@pytest.fixture
def client():
    # Function-scoped: each test needs its own cookie jar.
    with TestClient(main.app) as c:
        yield c


def _register(client, email, password="Original#12345", headers=None):
    return client.post("/auth/register", json={"email": email, "password": password},
                       headers=headers or {})


def _login(client, email, password="Original#12345", headers=None):
    return client.post("/auth/login", json={"email": email, "password": password},
                       headers=headers or {})


# ── The CLI's contract is unchanged ──────────────────────────────────────────

def test_without_the_header_the_body_still_carries_the_refresh_token(client):
    """The CLI has no cookie jar. Its response must not change at all."""
    res = _register(client, "cookie-cli@example.com")
    assert res.status_code == 201
    assert res.json()["refresh_token"]
    assert REFRESH_COOKIE not in res.cookies


def test_refresh_still_accepts_a_token_in_the_body(client):
    refresh = _register(client, "cookie-cli-refresh@example.com").json()["refresh_token"]
    res = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert res.status_code == 200
    assert res.json()["access_token"]


# ── Cookie mode ──────────────────────────────────────────────────────────────

def test_cookie_mode_withholds_the_refresh_token_from_the_body(client):
    res = _register(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    assert res.status_code == 201
    body = res.json()
    assert body["access_token"], "the access token still comes back — it lives in memory"
    assert not body.get("refresh_token"), "script must never see the refresh token"
    assert res.cookies.get(REFRESH_COOKIE)


def test_the_refresh_cookie_is_not_readable_by_script(client):
    res = _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    raw = res.headers.get("set-cookie", "")
    assert "httponly" in raw.lower()
    assert "samesite=strict" in raw.lower().replace(" ", "")


def test_the_csrf_cookie_is_readable_by_script(client):
    """The double-submit token has to be readable, or the client cannot echo it."""
    _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    assert client.cookies.get(CSRF_COOKIE)


def test_refresh_works_from_the_cookie_with_the_csrf_header(client):
    _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    csrf = client.cookies.get(CSRF_COOKIE)

    res = client.post("/auth/refresh", json={},
                      headers={**COOKIE_MODE, "X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    assert res.json()["access_token"]


def test_refresh_from_the_cookie_is_refused_without_the_csrf_header(client):
    """The whole point of the cookie: the browser attaches it uninvited."""
    _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    res = client.post("/auth/refresh", json={})
    assert res.status_code == 403


def test_refresh_from_the_cookie_is_refused_when_the_csrf_header_does_not_match(client):
    _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    res = client.post("/auth/refresh", json={}, headers={"X-CSRF-Token": "not-the-one"})
    assert res.status_code == 403


def test_a_body_token_does_not_need_a_csrf_header(client):
    """CSRF is about credentials the browser sends on its own.

    A token in the body was put there deliberately by the caller, which is the
    definition of not-forged.
    """
    refresh = _register(client, "cookie-mixed@example.com").json()["refresh_token"]
    res = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert res.status_code == 200


def test_refresh_with_no_credential_at_all_is_unauthorized(client):
    res = client.post("/auth/refresh", json={})
    assert res.status_code == 401


# ── Ending the session ───────────────────────────────────────────────────────

def test_logout_clears_the_cookies(client):
    login = _login(client, "cookie-browser@example.com", headers=COOKIE_MODE)
    access = login.json()["access_token"]

    res = client.post("/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert res.status_code == 200

    # Cleared, not merely expired server-side: a stale cookie would keep being
    # sent on every request to /auth for the rest of its lifetime.
    assert not client.cookies.get(REFRESH_COOKIE)


def test_a_revoked_cookie_session_cannot_refresh(client):
    login = _login(client, "cookie-revoke@example.com", headers=COOKIE_MODE) \
        if _register(client, "cookie-revoke@example.com", headers=COOKIE_MODE) else None
    assert login is not None
    access = login.json()["access_token"]
    csrf = client.cookies.get(CSRF_COOKIE)

    client.post("/auth/logout", headers={"Authorization": f"Bearer {access}"})
    res = client.post("/auth/refresh", json={}, headers={"X-CSRF-Token": csrf or ""})
    assert res.status_code in (401, 403)


def test_a_cookie_refresh_returns_a_pair_that_belongs_together(client):
    """The rotated cookie and the returned access token must be one pair.

    Minting two pairs and returning halves of each works — both are valid — but it
    issues twice the credentials for one request and makes "which session is
    this?" unanswerable in the audit trail.
    """
    import jwt as _jwt
    from app_db.config import settings as app_settings

    _register(client, "cookie-pairing@example.com", headers=COOKIE_MODE)
    csrf = client.cookies.get(CSRF_COOKIE)
    res = client.post("/auth/refresh", json={}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200

    access = _jwt.decode(res.json()["access_token"], app_settings.JWT_SECRET,
                         algorithms=[app_settings.JWT_ALGORITHM])
    rotated = _jwt.decode(client.cookies.get(REFRESH_COOKIE), app_settings.JWT_SECRET,
                          algorithms=[app_settings.JWT_ALGORITHM])
    assert access["sub"] == rotated["sub"]
    assert access["iat"] == rotated["iat"], "issued at the same moment = one pair"


def test_the_csrf_cookie_is_readable_from_any_page(client):
    """Scope matters: `Path=/auth` made it invisible to the app at `/app`.

    The client reads this cookie to echo it back on refresh. Scoped to the auth
    routes it could not be read from the page the app actually runs on, so the
    refresh on page load sent an empty token, failed the CSRF check, and logged
    the user out on every single reload. Caught in a browser, not by this suite —
    hence the test.

    The httpOnly refresh cookie keeps its narrow scope; only the non-secret half
    is site-wide.
    """
    from app_db.cookies import CSRF_COOKIE_PATH, REFRESH_COOKIE_PATH

    assert CSRF_COOKIE_PATH == "/"
    assert REFRESH_COOKIE_PATH == "/auth"

    res = _register(client, "cookie-paths@example.com", headers=COOKIE_MODE)
    raw = res.headers.get_list("set-cookie")
    refresh_header = next(h for h in raw if h.startswith(f"{REFRESH_COOKIE}="))
    csrf_header = next(h for h in raw if h.startswith(f"{CSRF_COOKIE}="))
    assert "Path=/auth" in refresh_header
    assert "Path=/" in csrf_header and "Path=/auth" not in csrf_header
