"""Authentication endpoints: register, login, refresh, logout, me."""

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user, invalidate_revocation, write_audit
from ..login_guard import MAX_REGISTRATIONS, record_failure, reset as reset_login_guard, retry_after
from ..models import Organization, Role, User
from ..schemas import (
    LoginRequest, LoginResult, MfaDisableRequest, MfaEnableOut, MfaLoginRequest,
    MfaSetupOut, MfaVerifyRequest, RefreshRequest, RegisterRequest, TokenPair, UserOut,
)
from ..seed import PERMISSIONS
from ..security import (
    create_access_token, create_mfa_challenge_token, create_refresh_token, decode_token,
    decrypt_secret, encrypt_secret, generate_recovery_codes, generate_totp_secret,
    hash_password, hash_recovery_code, needs_rehash, qr_svg, totp_provisioning_uri,
    verify_password, verify_totp,
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


def _issue_pair(user: User, amr: list[str] | None = None) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(
            user_id=user.id, email=user.email, org_id=user.organization_id,
            roles=user.role_names(), permissions=user.permission_names(), amr=amr,
            token_version=user.token_version,
        ),
        refresh_token=create_refresh_token(user_id=user.id, token_version=user.token_version),
    )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, request: Request = None, db: Session = Depends(get_db)):
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
    write_audit(db, user_id=user.id, action="register", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id, ip_address=_client_ip(request))
    db.commit()
    db.refresh(user)
    return _issue_pair(user)


@router.post("/login", response_model=LoginResult)
def login(req: LoginRequest, request: Request = None, db: Session = Depends(get_db)):
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
    pair = _issue_pair(user, amr=["pwd"])
    return LoginResult(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/refresh", response_model=TokenPair)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_token(req.refresh_token, expected_type="refresh")
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
    return _issue_pair(user)


@router.post("/logout")
def logout(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Bump the user's token version so every outstanding refresh token stops
    # working immediately (stateless revocation — one integer, no denylist). The
    # short-lived access token still expires on its own ~15-minute clock.
    user.token_version += 1
    write_audit(db, user_id=user.id, action="logout", entity_type="user", entity_id=user.id,
                organization_id=user.organization_id)
    db.commit()
    invalidate_revocation(user.id)  # the access token stops working immediately too
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
def mfa_login(req: MfaLoginRequest, request: Request = None, db: Session = Depends(get_db)):
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
    return _issue_pair(user, amr=amr)


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
