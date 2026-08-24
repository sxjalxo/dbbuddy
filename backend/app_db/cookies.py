"""Cookie-borne refresh sessions for the browser, opt-in per request.

Both tokens used to live in `localStorage`, so any XSS anywhere in the frontend
read a credential that can query connected business databases — and the refresh
token being there too meant the takeover outlived the access token's 15 minutes.

An httpOnly cookie fixes that specific hole: script cannot read it, so an XSS can
*use* the session while the page is open but cannot exfiltrate one that keeps
working afterwards. It is not a cure for XSS; it removes the durable prize.

**Opt-in, per request.** A client that sends ``X-Auth-Mode: cookie`` gets the
cookie and no ``refresh_token`` in the response body. Everything else — the CLI
above all, which has no cookie jar and stores tokens in a file — gets exactly the
response it got before. Sniffing the User-Agent to guess would be both fragile and
invisible; an explicit header says what the caller wants.

**Why a CSRF token as well.** A cookie is attached by the browser whether or not
the request was intended, which is the entire CSRF problem. ``SameSite=Strict``
already prevents the cross-site send, and the double-submit token here is defence
in depth for the cases it does not cover (a same-site subdomain, a browser being
lenient). It is cheap because exactly one endpoint reads the cookie:
``/auth/refresh``. Everything else authenticates from the ``Authorization``
header, which a cross-origin form cannot set.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Request, Response

REFRESH_COOKIE = "dbbuddy_refresh"
CSRF_COOKIE = "dbbuddy_csrf"
CSRF_HEADER = "X-CSRF-Token"
AUTH_MODE_HEADER = "X-Auth-Mode"

# The refresh cookie is scoped to the auth routes: it is only ever read by
# /auth/refresh, and a cookie that is not sent cannot be stolen from a request
# that had no business carrying it.
REFRESH_COOKIE_PATH = "/auth"

# The CSRF cookie is site-wide, and has to be. It is not a credential — it proves
# the caller could read this origin's cookies, which is exactly what a cross-site
# forgery cannot do — and the client must read it from whatever page it happens to
# be on. Scoped to /auth it was invisible at /app, so the refresh on page load
# sent an empty token, failed the check, and logged the user out on every reload.
CSRF_COOKIE_PATH = "/"


def wants_cookie_session(request: Request | None) -> bool:
    """Whether this caller asked for the cookie-borne session."""
    if request is None:
        return False
    return (request.headers.get(AUTH_MODE_HEADER) or "").strip().lower() == "cookie"


def _secure() -> bool:
    """``Secure`` everywhere except local development.

    Marking the cookie Secure in development would stop it working over plain
    http://localhost, which is where the whole stack runs before it runs anywhere
    else — and a developer who cannot log in disables the feature rather than the
    flag.
    """
    return (os.getenv("DBBUDDY_ENV", "development").strip().lower()) == "production"


def issue_session_cookies(response: Response, refresh_token: str, max_age_days: int) -> str:
    """Attach the refresh + CSRF cookies. Returns the CSRF token.

    The CSRF cookie is deliberately *not* httpOnly: the client has to read it to
    echo it back in a header, and that is the whole mechanism. It is not a
    credential on its own — it proves the request came from a page that could read
    this origin's cookies, which is precisely what a cross-site forgery cannot do.
    """
    max_age = max_age_days * 24 * 60 * 60
    response.set_cookie(
        REFRESH_COOKIE, refresh_token,
        max_age=max_age, path=REFRESH_COOKIE_PATH,
        httponly=True, secure=_secure(), samesite="strict",
    )
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE, csrf,
        max_age=max_age, path=CSRF_COOKIE_PATH,
        httponly=False, secure=_secure(), samesite="strict",
    )
    return csrf


def clear_session_cookies(response: Response) -> None:
    """Remove both cookies.

    Cleared rather than left to expire: a stale refresh cookie keeps being sent on
    every request to /auth for the rest of its lifetime, which is noise at best
    and a credential sitting in a shared browser at worst.
    """
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE, path=CSRF_COOKIE_PATH)


def csrf_ok(request: Request | None) -> bool:
    """Double-submit check: header must match the cookie, and both must exist."""
    if request is None:
        return False
    header = (request.headers.get(CSRF_HEADER) or "").strip()
    cookie = (request.cookies.get(CSRF_COOKIE) or "").strip()
    if not header or not cookie:
        return False
    return secrets.compare_digest(header, cookie)
