"""Authentication endpoints: register, login, refresh, logout, me, password reset."""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..cookies import (
    REFRESH_COOKIE, clear_session_cookies, csrf_ok, issue_session_cookies,
    wants_cookie_session,
)
from ..database import get_db
from ..deps import get_current_user, invalidate_revocation, write_audit
from ..login_guard import (
    MAX_REGISTRATIONS, MAX_RESET_REQUESTS, record_failure, reset as reset_login_guard,
    retry_after,
)
# Imported as a module, not by name: the reset flow's only test seam is
# ``email.send_password_reset``, and a from-import would bind the original
# function here where monkeypatching the module attribute cannot reach it.
from .. import email as email_module
from ..models import EmailVerificationToken, Organization, PasswordResetToken, Role, User
from ..rate_limit import REFRESH_BUDGET, rate_limit
from ..schemas import (
    EmailVerificationConfirm, EmailVerificationRequest, LoginRequest, LoginResult,
    MfaDisableRequest, MfaEnableOut, MfaLoginRequest, MfaSetupOut, MfaVerifyRequest,
    PasswordResetConfirm, PasswordResetRequest, RefreshRequest, RegisterRequest,
    TokenPair, UserOut,
)
from ..seed import PERMISSIONS
from ..security import (
    create_access_token, create_mfa_challenge_token, create_refresh_token, decode_token,
    decrypt_secret, encrypt_secret, generate_recovery_codes, generate_reset_token,
    generate_totp_secret, hash_password, hash_recovery_code, hash_reset_token, needs_rehash,
    qr_svg, totp_provisioning_uri, verify_password, verify_totp,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request | None) -> str | None:
    return request.client.host if request and request.client else None


# New self-registered users get the Analyst role — today's DB Buddy experience.
# Admins promote/demote in a later phase.
DEFAULT_ROLE = "analyst"


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, email=user.email, full_name=user.full_name,
        is_active=user.is_active, organization_id=user.organization_id,
        mfa_enabled=user.mfa_enabled, roles=user.role_names(), permissions=user.permission_names(),
    )


def _session_response(user: User, request: Request | None, response: Response | None,
                      amr: list[str] | None = None) -> TokenPair:
    """Issue a token pair, and put the refresh half where the caller asked for it.

    Cookie mode (``X-Auth-Mode: cookie``) sets an httpOnly cookie and blanks the
    ``refresh_token`` field, so script never sees it. Anything else — the CLI
    above all — gets the field, exactly as before.

    One function rather than four, because "which callers remembered to set the
    cookie?" is the kind of question that eventually has a wrong answer: login,
    register, the MFA exchange and refresh itself all go through here.
    """
    pair = _issue_pair(user, amr=amr)
    if not wants_cookie_session(request) or response is None:
        return pair

    issue_session_cookies(response, pair.refresh_token, settings.REFRESH_TOKEN_TTL_DAYS)
    # Pydantic model, so build a new one rather than mutating in place.
    return TokenPair(access_token=pair.access_token, refresh_token="")


def _issue_verification(db: Session, user: User) -> str:
    """Spend any outstanding link and mint a fresh one. Returns the raw token."""
    now = datetime.now(timezone.utc)
    (db.query(EmailVerificationToken)
       .filter(EmailVerificationToken.user_id == user.id,
               EmailVerificationToken.used_at.is_(None))
       .update({"used_at": now}, synchronize_session=False))

    token = generate_reset_token()
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_reset_token(token),
        expires_at=now + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS),
    ))
    return token


def _require_verified(user: User) -> None:
    """Refuse a session when the address has not been proven, if that is enforced.

    Only checked at sign-in. Registration still creates the account — silently
    refusing to create one would be indistinguishable from a broken form — it just
    does not hand back a session.
    """
    if settings.REQUIRE_EMAIL_VERIFICATION and not user.email_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Confirm your email address before signing in. "
            "Check your inbox, or request a new link.",
        )


def _issue_pair(user: User, amr: list[str] | None = None) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(
            user_id=user.id, email=user.email, org_id=user.organization_id,
            roles=user.role_names(), permissions=user.permission_names(), amr=amr,
            token_version=user.token_version,
        ),
        refresh_token=create_refresh_token(user_id=user.id, token_version=user.token_version),
    )


# ``response_model=None``: the successful shape depends on configuration. With
# verification enforced there is no session to return, so the response is a 202
# and a message instead of a 201 and a token pair. Declaring one model would make
# the other a lie.
@router.post("/register", response_model=None, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, request: Request = None, response: Response = None,
             db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    ip = _client_ip(request)

    # Throttle self-registration per source IP — unlike /login and /mfa/login it
    # was previously unguarded, so anyone reaching the API could mint Analyst
    # accounts (each granting query:run and billable AI calls) in a tight loop.
    guard_key = f"register::{ip or '-'}"
    locked = retry_after(guard_key, max_attempts=MAX_REGISTRATIONS)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many registration attempts. Try again later.",
            headers={"Retry-After": str(locked)},
        )
    record_failure(guard_key)  # count every attempt toward the window

    # Optional email-domain allow-list for internal deployments (empty = open).
    allowed = settings.REGISTRATION_ALLOWED_DOMAINS
    if allowed:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Self-registration is not permitted for this email domain.",
            )

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    # Self-registered users join the default organization (an admin can move
    # them later). The default org is guaranteed by startup bootstrap.
    default_org = db.query(Organization).filter_by(is_default=True).one_or_none()
    user = User(
        email=email, password_hash=hash_password(req.password), full_name=req.full_name,
        organization_id=default_org.id if default_org else None,
    )
    role = db.query(Role).filter_by(name=DEFAULT_ROLE).one_or_none()
    if role:
        user.roles.append(role)
    db.add(user)
    db.flush()
    token = _issue_verification(db, user)
    write_audit(db, user_id=user.id, action="register", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id, ip_address=_client_ip(request))
    db.commit()
    db.refresh(user)

    # Sent whether or not verification is enforced: "off" means not *required*,
    # not unavailable, and an address confirmed early costs nothing.
    email_module.send_email_verification(
        to=user.email, token=token,
        ttl_hours=settings.EMAIL_VERIFICATION_TTL_HOURS,
    )

    if settings.REQUIRE_EMAIL_VERIFICATION:
        # The account exists; the session does not. Returning tokens here would
        # walk straight past the gate that /login enforces.
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"detail": "Check your email to confirm your address, then sign in."},
        )
    return _session_response(user, request, response)


@router.post("/login", response_model=LoginResult)
def login(req: LoginRequest, request: Request = None, response: Response = None,
          db: Session = Depends(get_db)):
    email = req.email.lower().strip()
    ip = _client_ip(request)
    guard_key = f"{ip or '-'}::{email}"

    locked = retry_after(guard_key)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(locked)},
        )

    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(req.password, user.password_hash):
        record_failure(guard_key)
        # Record the failed attempt (no user_id when the email is unknown).
        write_audit(db, user_id=user.id if user else None, action="login_failed", entity_type="user",
                    entity_id=user.id if user else None,
                    organization_id=user.organization_id if user else None,
                    detail={"email": email}, ip_address=ip)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")

    if not user.is_active:
        # Record the disabled-login attempt internally, but return the SAME
        # generic 401 as a bad password so the response never confirms that the
        # email exists or that the supplied password was correct. The failure
        # counter is intentionally NOT cleared here — a disabled account should
        # not be able to reset its own lockout by supplying the right password.
        write_audit(db, user_id=user.id, action="login_denied", entity_type="user", entity_id=user.id,
                    organization_id=user.organization_id, detail={"reason": "inactive"}, ip_address=ip)
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")

    _require_verified(user)

    # Fully successful auth — clear the failure counter for this ip/email.
    reset_login_guard(guard_key)

    # Transparently upgrade the hash if Argon2 parameters changed.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(req.password)

    # Password verified. If MFA is on, hand back a short-lived challenge instead
    # of the token pair — the second factor is required to complete login.
    if user.mfa_enabled:
        write_audit(db, user_id=user.id, action="mfa_challenge", entity_type="user", entity_id=user.id,
                    organization_id=user.organization_id, ip_address=ip)
        db.commit()
        return LoginResult(mfa_required=True, challenge_token=create_mfa_challenge_token(user_id=user.id))

    write_audit(db, user_id=user.id, action="login", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id, ip_address=ip)
    db.commit()
    pair = _session_response(user, request, response, amr=["pwd"])
    return LoginResult(access_token=pair.access_token, refresh_token=pair.refresh_token)


# fail_open=False: this endpoint mints sessions, so it follows the login_guard
# rule rather than the throughput one — a control protecting authentication
# must not disappear when Redis does. Without a shared window it falls back to
# the per-process window, which is stricter, never absent.
@router.post("/refresh", response_model=TokenPair,
             dependencies=[Depends(rate_limit("refresh", REFRESH_BUDGET,
                                              fail_open=False))])
def refresh(req: RefreshRequest, request: Request = None, response: Response = None,
            db: Session = Depends(get_db)):
    # A token in the body was put there deliberately by the caller — that is the
    # definition of not-forged, so it needs no CSRF check. A token in a cookie was
    # attached by the browser whether or not the request was intended, so it does.
    token = req.refresh_token
    from_cookie = False
    if not token and request is not None:
        token = request.cookies.get(REFRESH_COOKIE)
        from_cookie = bool(token)

    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token supplied")

    if from_cookie and not csrf_ok(request):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Missing or invalid CSRF token.",
        )

    try:
        payload = decode_token(token, expected_type="refresh")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None

    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    # Reject refresh tokens minted before the user's version was bumped (logout,
    # password change, MFA disable, deactivation) — stateless revocation.
    if payload.get("token_version") != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token has been revoked")

    # Rotate the cookie too when that is how the session is carried, so a refresh
    # extends the session rather than leaving the original cookie to expire under
    # a freshly-issued token.
    if from_cookie and response is not None:
        rotated = _issue_pair(user)
        issue_session_cookies(response, rotated.refresh_token,
                              settings.REFRESH_TOKEN_TTL_DAYS)
        return TokenPair(access_token=rotated.access_token, refresh_token="")
    return _session_response(user, request, response)


@router.post("/logout")
def logout(response: Response = None, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    # Bump the user's token version so every outstanding refresh token stops
    # working immediately (stateless revocation — one integer, no denylist). The
    # short-lived access token still expires on its own ~15-minute clock.
    user.token_version += 1
    write_audit(db, user_id=user.id, action="logout", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id)
    db.commit()
    invalidate_revocation(user.id)  # the access token stops working immediately too
    # Unconditionally: clearing a cookie that was never set is a no-op, and
    # checking first would leave a stale cookie behind for anyone whose session
    # started in a different mode.
    if response is not None:
        clear_session_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _user_out(user)


# ── MFA / 2FA ─────────────────────────────────────────────────────────────────

@router.post("/mfa/setup", response_model=MfaSetupOut)
def mfa_setup(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Begin TOTP enrollment: store a pending (encrypted) secret and return the
    provisioning URI + QR. MFA is not active until /mfa/verify succeeds."""
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is already enabled. Disable it first to re-enroll.")
    secret = generate_totp_secret()
    user.mfa_secret = encrypt_secret(secret)
    db.commit()
    uri = totp_provisioning_uri(secret, user.email)
    return MfaSetupOut(secret=secret, otpauth_uri=uri, qr_svg=qr_svg(uri))


@router.post("/mfa/verify", response_model=MfaEnableOut)
def mfa_verify(req: MfaVerifyRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Confirm the first TOTP code, enable MFA, and return one-time recovery codes."""
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is already enabled.")
    if not user.mfa_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Start enrollment with /auth/mfa/setup first.")
    if not verify_totp(decrypt_secret(user.mfa_secret), req.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code didn't match. Try again.")

    codes = generate_recovery_codes()
    user.mfa_recovery_codes = [hash_recovery_code(c) for c in codes]
    user.mfa_enabled = True
    write_audit(db, user_id=user.id, action="enable", entity_type="mfa", entity_id=user.id,
                organization_id=user.organization_id)
    db.commit()
    return MfaEnableOut(enabled=True, recovery_codes=codes)


@router.post("/mfa/login", response_model=TokenPair)
def mfa_login(req: MfaLoginRequest, request: Request = None, response: Response = None,
              db: Session = Depends(get_db)):
    """Exchange an MFA challenge + a TOTP (or recovery) code for the token pair."""
    ip = _client_ip(request)
    try:
        payload = decode_token(req.challenge_token, expected_type="mfa_challenge")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your verification window expired. Sign in again.") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid verification request.") from None

    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Verification is not available for this account.")

    # Throttle second-factor guessing (TOTP is only 6 digits) per ip/account.
    guard_key = f"{ip or '-'}::mfa::{user.id}"
    locked = retry_after(guard_key)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed verification attempts. Try again later.",
            headers={"Retry-After": str(locked)},
        )

    amr = ["pwd", "otp"]
    if not verify_totp(decrypt_secret(user.mfa_secret), req.code):
        # Fall back to a single-use recovery code.
        digest = hash_recovery_code(req.code)
        codes = list(user.mfa_recovery_codes or [])
        if digest in codes:
            codes.remove(digest)  # consume it
            user.mfa_recovery_codes = codes
            amr = ["pwd", "recovery"]
        else:
            record_failure(guard_key)
            write_audit(db, user_id=user.id, action="mfa_failed", entity_type="user", entity_id=user.id,
                        organization_id=user.organization_id, ip_address=ip)
            db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code didn't match.")

    reset_login_guard(guard_key)
    write_audit(db, user_id=user.id, action="login", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id, detail={"amr": amr}, ip_address=ip)
    db.commit()
    return _session_response(user, request, response, amr=amr)


@router.post("/mfa/disable")
def mfa_disable(req: MfaDisableRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Turn off MFA. Requires re-auth: the current password OR a current code."""
    if not user.mfa_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is not enabled.")
    reauthed = (req.password and verify_password(req.password, user.password_hash)) or (
        req.code and user.mfa_secret and verify_totp(decrypt_secret(user.mfa_secret), req.code)
    )
    if not reauthed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Confirm your password or a current code to disable MFA.")

    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_recovery_codes = None
    # A second-factor change is a security event → cut existing refresh tokens.
    user.token_version += 1
    write_audit(db, user_id=user.id, action="disable", entity_type="mfa", entity_id=user.id,
                organization_id=user.organization_id)
    db.commit()
    invalidate_revocation(user.id)
    db.refresh(user)
    # Bumping token_version now kills the *access* token as well as the refresh
    # token (the ``tv`` claim), which is the point — but the caller just proved who
    # they are, so log them out of every other session and hand this one a fresh
    # pair rather than bouncing them to the sign-in screen.
    pair = _issue_pair(user, amr=["pwd"])
    return {"ok": True, "access_token": pair.access_token, "refresh_token": pair.refresh_token}


@router.get("/permissions")
def list_permission_catalogue(_: User = Depends(get_current_user)):
    """The full permission catalogue (name + description).

    Lets the frontend build menus/gates declaratively against a single source of
    truth instead of hardcoding permission strings. The caller's own grants are
    on ``/auth/me``; this is the catalogue, so any authenticated user may read it.
    """
    return {"permissions": [{"name": name, "description": desc} for name, desc in PERMISSIONS.items()]}


# ── Password reset ────────────────────────────────────────────────────────────
# Two endpoints, and most of the design is in what they refuse to reveal.
#
# The request endpoint answers **identically** for a known address, an unknown
# one, and a deactivated account. Anything else — a different status, a different
# body, even a noticeably different response time — turns an unauthenticated
# endpoint into a list of who has an account here. The cost is that a typo fails
# silently; that trade is the standard one and it is the right way round.

# Same shape for every outcome, so the response body cannot be compared either.
_RESET_REQUESTED = {
    "detail": "If that address has an account, a reset link is on its way.",
}


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
def request_password_reset(req: PasswordResetRequest, request: Request = None,
                           db: Session = Depends(get_db)):
    """Start a reset. Always 202 — see the note above."""
    email = req.email.lower().strip()
    ip = _client_ip(request)

    # Throttled *before* the account is looked up, and counted whether or not one
    # exists. Doing it after would make the 429 itself an enumeration signal —
    # "this address is rate-limited" would mean "this address is real".
    #
    # The endpoint mails a third party on request, so unthrottled it is a way to
    # flood someone's inbox from this server's reputation and burn its SMTP quota.
    guard_key = f"reset::{ip or '-'}::{email}"
    locked = retry_after(guard_key, max_attempts=MAX_RESET_REQUESTS)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many reset requests. Try again later.",
            headers={"Retry-After": str(locked)},
        )
    record_failure(guard_key)

    user = db.query(User).filter(User.email == email).first()

    # A deactivated account is not recoverable by its former owner: whoever
    # deactivated it made that call, and a reset link would undo it.
    if user is None or not user.is_active:
        write_audit(db, user_id=user.id if user else None, action="password_reset_requested",
                    entity_type="user", entity_id=user.id if user else None,
                    organization_id=user.organization_id if user else None,
                    detail={"email": email,
                            "outcome": "no_active_account"}, ip_address=ip)
        db.commit()
        return _RESET_REQUESTED

    # Any link already outstanding is spent. Two live links means the older one
    # keeps working after the user has recovered the account — exactly the
    # window a reset is supposed to close.
    now = datetime.now(timezone.utc)
    (db.query(PasswordResetToken)
       .filter(PasswordResetToken.user_id == user.id,
               PasswordResetToken.used_at.is_(None))
       .update({"used_at": now}, synchronize_session=False))

    token = generate_reset_token()
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=hash_reset_token(token),
        expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
        requested_ip=ip,
    ))
    write_audit(db, user_id=user.id, action="password_reset_requested", entity_type="user",
                entity_id=user.id, organization_id=user.organization_id,
                detail={"outcome": "sent"}, ip_address=ip)
    db.commit()

    # Delivery failure is logged inside the sender and never surfaced: raising
    # here would answer "does this address exist?" with a 500.
    email_module.send_password_reset(
        to=user.email, token=token,
        ttl_minutes=settings.PASSWORD_RESET_TTL_MINUTES,
    )
    return _RESET_REQUESTED


@router.post("/password-reset/confirm")
def confirm_password_reset(req: PasswordResetConfirm, request: Request = None,
                           db: Session = Depends(get_db)):
    """Redeem a token and set the new password.

    Every failure is the same 400. Distinguishing "unknown", "expired" and
    "already used" would tell a holder of a stale token which kind of stale it
    is, which is only useful to someone who should not have it.
    """
    ip = _client_ip(request)
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That reset link is invalid or has expired.")

    row = (db.query(PasswordResetToken)
             .filter(PasswordResetToken.token_hash == hash_reset_token(req.token))
             .first())
    if row is None or row.used_at is not None:
        raise invalid

    # SQLite hands back naive datetimes even for timezone=True columns, so
    # compare on a common footing rather than trusting the driver.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise invalid

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid

    row.used_at = datetime.now(timezone.utc)
    user.password_hash = hash_password(req.new_password)
    # Ends every outstanding session. A reset usually means the old password may
    # be known to someone else, so leaving their tokens alive would defeat it.
    # The in-process cache has to be told as well, or the change waits out its TTL
    # on the endpoints that trust JWT claims without a DB read.
    user.token_version = (user.token_version or 0) + 1

    write_audit(db, user_id=user.id, action="password_reset_completed", entity_type="user",
                entity_id=user.id, organization_id=user.organization_id, ip_address=ip)
    db.commit()
    invalidate_revocation(user.id)

    return {"detail": "Password updated. Sign in with your new password."}


# ── Email verification ────────────────────────────────────────────────────────
# Same privacy rules as password reset: the request endpoint answers identically
# for an address that exists, one that does not, and one already verified. See
# the note above /password-reset/request for why that matters.

_VERIFICATION_REQUESTED = {
    "detail": "If that address needs confirming, a link is on its way.",
}


@router.post("/verify-email/request", status_code=status.HTTP_202_ACCEPTED)
def request_email_verification(req: EmailVerificationRequest, request: Request = None,
                               db: Session = Depends(get_db)):
    """Resend the confirmation link."""
    email = req.email.lower().strip()
    ip = _client_ip(request)

    # Throttled before the lookup and counted regardless, so the 429 cannot be
    # read as "this address exists". Shares the reset budget's shape.
    guard_key = f"verify::{ip or '-'}::{email}"
    locked = retry_after(guard_key, max_attempts=MAX_RESET_REQUESTS)
    if locked:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Try again later.",
            headers={"Retry-After": str(locked)},
        )
    record_failure(guard_key)

    user = db.query(User).filter(User.email == email).first()
    # Nothing to do for an unknown address, an inactive account, or one already
    # confirmed — and all three answer the same as success.
    if user is None or not user.is_active or user.email_verified:
        return _VERIFICATION_REQUESTED

    token = _issue_verification(db, user)
    db.commit()
    email_module.send_email_verification(
        to=user.email, token=token,
        ttl_hours=settings.EMAIL_VERIFICATION_TTL_HOURS,
    )
    return _VERIFICATION_REQUESTED


@router.post("/verify-email/confirm")
def confirm_email_verification(req: EmailVerificationConfirm, request: Request = None,
                               db: Session = Depends(get_db)):
    """Redeem a confirmation link.

    One 400 for every failure, for the same reason the reset endpoint does it:
    telling the holder of a stale token which kind of stale only helps someone who
    should not have it.
    """
    ip = _client_ip(request)
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That confirmation link is invalid or has expired.")

    row = (db.query(EmailVerificationToken)
             .filter(EmailVerificationToken.token_hash == hash_reset_token(req.token))
             .first())
    if row is None or row.used_at is not None:
        raise invalid

    # SQLite returns naive datetimes even for timezone=True columns.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise invalid

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid

    now = datetime.now(timezone.utc)
    row.used_at = now
    user.email_verified = True
    user.email_verified_at = now

    write_audit(db, user_id=user.id, action="email_verified", entity_type="user",
                entity_id=user.id, organization_id=user.organization_id, ip_address=ip)
    db.commit()

    return {"detail": "Address confirmed. You can sign in now."}
