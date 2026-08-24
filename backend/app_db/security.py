"""Password hashing (Argon2), JWT issuance/verification, and at-rest encryption
for ERP connection secrets."""

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet

from .config import settings

# ── Password hashing (Argon2) ────────────────────────────────────────────────

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:
        return False


# ── JWT (access + refresh) ────────────────────────────────────────────────────

def _create_token(claims: dict, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    *, user_id: str, email: str, roles: list[str], permissions: list[str],
    org_id: str | None = None, amr: list[str] | None = None,
    token_version: int = 0,
) -> str:
    # amr = "authentication methods references" (RFC 8176): how the session was
    # authenticated, e.g. ["pwd"] or ["pwd", "otp"]. Lets later policy require MFA.
    #
    # ``tv`` mirrors User.token_version, the same integer that revokes refresh
    # tokens. Access tokens carried no revocation signal at all, so the hot
    # endpoints that trust JWT claims without a DB read (/query, /execute,
    # /analyze — see deps.get_token_payload) kept serving a user who had just been
    # logged out, deactivated, or had their password reset, for the full token
    # lifetime. Those are precisely the endpoints that reach customer ERP data.
    return _create_token(
        {
            "sub": user_id, "email": email, "org_id": org_id,
            "roles": roles, "permissions": permissions, "amr": amr or ["pwd"],
            "tv": token_version,
        },
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
    )


def create_refresh_token(*, user_id: str, token_version: int = 0) -> str:
    # ``token_version`` ties the refresh token to the user row; a mismatch on
    # refresh (after logout / password change / MFA disable / deactivation)
    # rejects the token. See User.token_version.
    return _create_token(
        {"sub": user_id, "token_version": token_version},
        "refresh",
        timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS),
    )


def create_mfa_challenge_token(*, user_id: str) -> str:
    """A short-lived token issued after password success when MFA is enabled; it
    is exchanged (with a TOTP/recovery code) for the real token pair."""
    return _create_token({"sub": user_id}, "mfa_challenge", timedelta(minutes=5))


def decode_token(token: str, *, expected_type: str | None = None) -> dict:
    """Decode and validate a JWT. Raises jwt exceptions on failure."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"Expected {expected_type} token, got {payload.get('type')!r}")
    return payload


# ── Encryption for ERP connection secrets (Fernet) ───────────────────────────
# Derive a stable 32-byte Fernet key from APP_SECRET_KEY (or the JWT secret in
# dev). ERP passwords are encrypted here before being stored in the app DB.

def _fernet() -> Fernet:
    seed = (settings.APP_SECRET_KEY or settings.JWT_SECRET).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# ── MFA / 2FA (TOTP + recovery codes) ─────────────────────────────────────────

import io  # noqa: E402
import secrets as _secrets  # noqa: E402

import pyotp  # noqa: E402


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str, issuer: str = "DB Buddy") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code, allowing ±1 time-step for clock skew."""
    try:
        return pyotp.TOTP(secret).verify((code or "").strip(), valid_window=1)
    except Exception:
        return False


def qr_svg(data: str) -> str:
    """Render an SVG QR code for an otpauth URI (no PIL dependency)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    buf = io.BytesIO()
    qrcode.make(data, image_factory=SvgPathImage).save(buf)
    return buf.getvalue().decode("utf-8")


def generate_recovery_codes(n: int = 10) -> list[str]:
    """Plaintext single-use recovery codes — shown to the user exactly once."""
    return [f"{_secrets.token_hex(4)}-{_secrets.token_hex(4)}" for _ in range(n)]


def hash_recovery_code(code: str) -> str:
    """Recovery codes are high-entropy, so a fast SHA-256 hash is sufficient."""
    return hashlib.sha256((code or "").strip().lower().encode("utf-8")).hexdigest()


# ── Personal API keys (CLI / automation) ──────────────────────────────────────
# Format: ``dbk_<prefix>_<secret>``. The prefix is a public, non-secret handle
# used to locate the row; the secret is high-entropy. Only the SHA-256 hash of
# the full token is stored — the raw value is shown to the owner exactly once.

API_KEY_PREFIX = "dbk"


def generate_api_key() -> tuple[str, str, str]:
    """Mint a new API key. Returns ``(plaintext, token_prefix, token_hash)``.

    Only ``token_prefix`` and ``token_hash`` are persisted; ``plaintext`` is
    returned to the caller once and never stored.
    """
    prefix = _secrets.token_hex(4)              # 8 hex chars — public lookup handle
    secret = _secrets.token_urlsafe(32)         # the actual secret
    plaintext = f"{API_KEY_PREFIX}_{prefix}_{secret}"
    return plaintext, prefix, hash_api_key(plaintext)


def hash_api_key(token: str) -> str:
    """SHA-256 of the full token (high-entropy → a fast hash is sufficient)."""
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def parse_api_key_prefix(token: str) -> str | None:
    """Return the lookup prefix from a ``dbk_<prefix>_<secret>`` token, or None."""
    parts = (token or "").strip().split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or not parts[1]:
        return None
    return parts[1]


def verify_api_key(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    return _secrets.compare_digest(hash_api_key(token), token_hash or "")
