"""Configuration for the application database and auth.

All values come from environment variables so nothing secret is committed.
Defaults are dev-friendly (SQLite, a throwaway JWT secret, localhost CORS) and,
in development, only *warn* when a default is in play. In **production**
(``DBBUDDY_ENV=production``) those same soft defaults become hard errors: the
process fails to start rather than boot with an insecure or accidental config.
Validation is aggregated, so one startup surfaces every problem at once.
"""

import logging
import os

logger = logging.getLogger(__name__)


def parse_previous_keys(raw: str | None) -> list[str]:
    """Split the comma-separated retired-key list, dropping blanks.

    Tolerant of stray whitespace and trailing commas: this is edited by hand in a
    deployment's environment, usually under time pressure, and a silently ignored
    key would look exactly like data loss.
    """
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


class Settings:
    # ── Deployment environment ────────────────────────────────────────────────
    # "development" (default), "production", or "test". Only "production" turns
    # the dev-friendly defaults below into fail-fast errors. An unrecognized
    # value is itself an error — a typo like "prod" must not silently disable the
    # production checks.
    ENV: str = os.getenv("DBBUDDY_ENV", "development").strip().lower()
    KNOWN_ENVS: set[str] = {"development", "production", "test"}

    # ── Application database ──────────────────────────────────────────────────
    # The single source of truth for platform state. SQLAlchemy makes the models
    # engine-agnostic; the *intended production target is PostgreSQL* — point
    # APP_DATABASE_URL at it, e.g.:
    #   postgresql+psycopg2://user:pass@localhost:5432/dbbuddy_app
    # Defaults to a local SQLite file so the backend runs with zero setup.
    APP_DATABASE_URL: str = os.getenv("APP_DATABASE_URL", "sqlite:///./dbbuddy_app.db")

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    _JWT_SECRET_FROM_ENV: bool = bool(os.getenv("JWT_SECRET"))
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15"))
    REFRESH_TOKEN_TTL_DAYS: int = int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "7"))

    # ── Encryption key for ERP connection secrets at rest (Fernet) ────────────
    # ERP database passwords are stored in the app DB encrypted, never plaintext.
    APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "")

    # Keys this deployment has retired but whose ciphertext may still be in the
    # database. Newest first, comma-separated. Everything is *encrypted* with
    # APP_SECRET_KEY and *decrypted* with whichever of these still fits, which is
    # what makes rotation a procedure rather than a data loss:
    #
    #   1. move the old value here, put the new one in APP_SECRET_KEY — nothing
    #      becomes unreadable, and new writes use the new key;
    #   2. run scripts/rotate_secrets.py to re-encrypt at rest;
    #   3. remove the old value from this list.
    #
    # Skipping step 2 and going straight to step 3 destroys every stored ERP
    # password, AI provider key and MFA secret.
    APP_SECRET_KEYS_PREVIOUS: list[str] = parse_previous_keys(
        os.getenv("APP_SECRET_KEYS_PREVIOUS")
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Explicit allow-list of browser origins permitted to call the API with
    # credentials. NEVER "*": a wildcard with credentials makes Starlette reflect
    # any Origin, letting any site issue authenticated cross-origin requests.
    # Comma-separated in the environment; defaults to the local dev frontend.
    #   ALLOWED_ORIGINS=https://erp.company.com,https://analytics.company.com
    _ALLOWED_ORIGINS_FROM_ENV: bool = os.getenv("ALLOWED_ORIGINS") is not None
    ALLOWED_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ]

    # ── Self-registration controls ────────────────────────────────────────────
    # Optional email-domain allow-list for POST /auth/register. Empty (default)
    # keeps open self-registration; set it to restrict new self-service accounts
    # to trusted domains for a hosted deployment. Comma-separated, "@" optional:
    #   REGISTRATION_ALLOWED_DOMAINS=example.org,corp.example.org
    REGISTRATION_ALLOWED_DOMAINS: list[str] = [
        d.strip().lower().lstrip("@")
        for d in os.getenv("REGISTRATION_ALLOWED_DOMAINS", "").split(",")
        if d.strip()
    ]

    # ── Email verification ────────────────────────────────────────────────────
    # Off by default, on purpose. Enforcing it would change what an existing
    # install, the Docker demo and every developer setup already do, and none of
    # them asked. A hosted deployment that wants proof of address opts in — the
    # same shape as every other production-only setting here.
    #
    # When on, an unverified account can be created but cannot sign in. Accounts
    # that predate the feature are backfilled as verified by migration 0017:
    # locking out an existing user base on upgrade is an outage, not a hardening.
    REQUIRE_EMAIL_VERIFICATION: bool = (
        os.getenv("REQUIRE_EMAIL_VERIFICATION", "").strip().lower() in {"1", "true", "yes", "on"}
    )

    # How long a verification link stays usable. Longer than a password reset: it
    # is normal to register and confirm the next morning, and the consequence of
    # an expired one is a resend rather than a locked account.
    EMAIL_VERIFICATION_TTL_HOURS: int = int(os.getenv("EMAIL_VERIFICATION_TTL_HOURS", "48"))

    # ── Password reset ────────────────────────────────────────────────────────
    # How long a reset link stays usable. Short on purpose: the link is a bearer
    # credential sitting in an inbox, and the whole flow is a minute's work.
    PASSWORD_RESET_TTL_MINUTES: int = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "30"))

    # HS256 signs with the raw secret bytes, so anything shorter than the hash
    # output (32 bytes) weakens the MAC (see RFC 7518 §3.2). Enforced at startup.
    MIN_JWT_SECRET_BYTES: int = 32

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    def __init__(self) -> None:
        # Collect every problem, then raise once — a misconfigured deploy sees the
        # full list in a single startup attempt instead of one error per restart.
        errors: list[str] = []

        if self.ENV not in self.KNOWN_ENVS:
            errors.append(
                f"DBBUDDY_ENV={self.ENV!r} is not recognized "
                f"(expected one of {sorted(self.KNOWN_ENVS)})."
            )
        prod = self.is_production

        # ── JWT secret ────────────────────────────────────────────────────────
        if not self.JWT_SECRET:
            if prod:
                errors.append("JWT_SECRET must be set in production (no ephemeral fallback).")
            else:
                import secrets as _secrets
                self.JWT_SECRET = _secrets.token_urlsafe(48)
                logger.warning(
                    "JWT_SECRET not set — using an ephemeral dev secret. Set JWT_SECRET "
                    "in the environment for any real deployment."
                )
        elif len(self.JWT_SECRET.encode("utf-8")) < self.MIN_JWT_SECRET_BYTES:
            # A secret was provided but is too short to be safe.
            errors.append(
                f"JWT_SECRET is too short: {len(self.JWT_SECRET.encode('utf-8'))} bytes "
                f"(minimum {self.MIN_JWT_SECRET_BYTES}). Generate one with, e.g., "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
            )

        # ── At-rest encryption key ────────────────────────────────────────────
        if not self.APP_SECRET_KEY:
            if prod:
                errors.append(
                    "APP_SECRET_KEY must be set in production — it derives the key that "
                    "encrypts ERP connection passwords and MFA secrets at rest."
                )
            else:
                logger.warning(
                    "APP_SECRET_KEY not set — deriving the at-rest encryption key from "
                    "JWT_SECRET (dev only)."
                )

        # ── Database URL ──────────────────────────────────────────────────────
        backend_name: str | None = None
        try:
            from sqlalchemy.engine import make_url
            backend_name = make_url(self.APP_DATABASE_URL).get_backend_name()
        except Exception as exc:
            errors.append(f"APP_DATABASE_URL is not a valid SQLAlchemy URL: {exc}")
        if backend_name == "sqlite":
            if prod:
                errors.append(
                    "APP_DATABASE_URL points at SQLite; production requires PostgreSQL "
                    "(postgresql+psycopg2://user:pass@host:5432/dbbuddy_app)."
                )
            else:
                logger.warning(
                    "APP_DATABASE_URL is using SQLite (dev default). Set it to a "
                    "PostgreSQL URL for production: postgresql+psycopg2://...:.../dbbuddy_app"
                )

        # ── CORS allow-list ───────────────────────────────────────────────────
        if any(o == "*" for o in self.ALLOWED_ORIGINS):
            errors.append(
                "ALLOWED_ORIGINS must not contain '*' — a credentialed wildcard lets any "
                "site make authenticated cross-origin requests. List explicit origins."
            )
        for o in self.ALLOWED_ORIGINS:
            if o != "*" and not (o.startswith("http://") or o.startswith("https://")):
                errors.append(
                    f"ALLOWED_ORIGINS entry {o!r} must include a scheme (http:// or https://)."
                )
        if prod and not self._ALLOWED_ORIGINS_FROM_ENV:
            errors.append(
                "ALLOWED_ORIGINS must be set in production — the localhost default is "
                "dev-only and would reject (or mis-scope) the real frontend."
            )

        if errors:
            raise ValueError(
                "Invalid configuration — refusing to start:\n  - " + "\n  - ".join(errors)
            )


settings = Settings()
