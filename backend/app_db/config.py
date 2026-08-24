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
