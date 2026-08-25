"""Application-database models — the first-class platform entities.

These intentionally mirror the shapes the frontend already uses (SavedChart,
QueryHistory, DatabaseConnection) so the localStorage → API migration is a
straight swap, and they carry RBAC/audit/publish fields so later phases need no
schema redesign.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, FetchedValue, ForeignKey, Integer, JSON, String, Table,
    Column, Text, UniqueConstraint,
    false as sa_false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Association tables (RBAC) ─────────────────────────────────────────────────

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", String(36), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


# ── Identity & RBAC ───────────────────────────────────────────────────────────

class Organization(Base):
    """Tenant boundary. Every user belongs to exactly one organization.

    Exactly one org is flagged ``is_default`` — the home for self-registered
    users and the backfill target for any pre-tenancy rows, so the
    ``users.organization_id`` invariant always holds.
    """
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # URL-safe stable handle (e.g. "acme") — preferred over the opaque id in
    # routes like /org/<slug>/reports. Unique and required.
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Required: every user belongs to an organization (tenancy invariant). The
    # default org is created and back-filled at startup so this never holds NULL.
    # RESTRICT: an org with members cannot be deleted out from under them.
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # ── MFA / 2FA (TOTP) ──────────────────────────────────────────────────────
    # The TOTP secret is Fernet-encrypted at rest (never plaintext); recovery
    # codes are stored only as hashes. ``mfa_enabled`` flips on after the user
    # verifies their first code, so a half-set-up secret never gates login.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_recovery_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ── Email verification ────────────────────────────────────────────────────
    # Whether the address has been proven reachable. Only *enforced* when
    # REQUIRE_EMAIL_VERIFICATION is on; the column is maintained either way so a
    # deployment can turn enforcement on later without a backfill of its own.
    #
    # Defaults to false for new rows, but migration 0017 backfills every existing
    # row as verified — those accounts were created under the old rules, and
    # locking them out on upgrade is an outage rather than a hardening.
    email_verified: Mapped[bool] = mapped_column(
        # sa.false() renders per dialect; a literal "0" is a boolean on SQLite and
        # a type error on PostgreSQL.
        Boolean, default=False, server_default=sa_false(), nullable=False
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Refresh-token revocation (stateless, one integer) ─────────────────────
    # Every refresh token carries the ``token_version`` it was minted with; a
    # refresh is honored only while the two still match. Bumping this (logout,
    # password change, MFA disable, account deactivation) instantly invalidates
    # every outstanding refresh token for the user — no denylist, no Redis, no
    # cleanup job. Access tokens remain stateless and short-lived by design.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    organization: Mapped["Organization"] = relationship(lazy="joined")
    roles: Mapped[list["Role"]] = relationship(secondary=user_roles, back_populates="users", lazy="selectin")

    def permission_names(self) -> list[str]:
        names: set[str] = set()
        for role in self.roles:
            for perm in role.permissions:
                names.add(perm.name)
        return sorted(names)

    def role_names(self) -> list[str]:
        return sorted(r.name for r in self.roles)


class EmailVerificationToken(Base):
    """Proof that whoever registered can read the address they claimed.

    Same shape and same rules as ``PasswordResetToken`` — hashed at rest, single
    use, short lived — because it is the same kind of object: a bearer credential
    mailed to an address, redeemable once.

    Kept as its own table rather than a ``purpose`` column on one shared table.
    The two have different lifetimes and different consequences, and a single
    token store invites a bug where a verification link is redeemed as a password
    reset.
    """
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PasswordResetToken(Base):
    """A single-use, short-lived permission to set a new password.

    Only the SHA-256 hash of the token is stored — the raw value exists in the
    email and nowhere else, matching the API-key and recovery-code convention
    (the token is high-entropy, so a fast hash is sufficient).

    Rows are kept after use rather than deleted: ``used_at`` is what makes a
    second attempt fail, and an audit trail of resets is worth more than the
    handful of bytes. ``token_version`` is bumped on the user at the same time,
    which is what actually ends their existing sessions.
    """
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Recorded for the audit trail: a reset request is a security event, and
    # "where from" is the first question asked about a suspicious one.
    requested_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApiKey(Base):
    """A personal access token that lets a user authenticate the CLI (and any
    automation) without their password.

    The raw token is shown to the owner exactly once at creation and never
    stored — only its SHA-256 hash is persisted (the token is high-entropy, so a
    fast hash is sufficient, matching the recovery-code convention). A short,
    non-secret ``token_prefix`` is stored in the clear so a presented token can
    be located without scanning every row, and so the UI can display which key
    is which. A key inherits its owner's roles/permissions at exchange time, so
    revoking a role immediately narrows every key the user holds.
    """
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Public, non-secret lookup handle (the ``dbk_<prefix>_…`` segment).
    token_prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A revoked key is kept (not deleted) so it stays visible in the audit trail.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User"] = relationship(lazy="joined")

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    users: Mapped[list["User"]] = relationship(secondary=user_roles, back_populates="roles")
    permissions: Mapped[list["Permission"]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    roles: Mapped[list["Role"]] = relationship(secondary=role_permissions, back_populates="permissions")


# ── Platform data (formerly localStorage) ─────────────────────────────────────

class DatabaseConnection(Base):
    """A customer ERP connection config, owned by a user. The password is stored
    encrypted at rest (Fernet) — never plaintext, and never in the ERP itself."""
    __tablename__ = "database_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    engine: Mapped[str] = mapped_column(String(30), nullable=False)  # mysql | postgresql | sqlserver
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    database: Mapped[str] = mapped_column(String(255), nullable=False)
    # A namespace *within* the database — PostgreSQL's schema, SQL Server's. NULL
    # means "follow the connection's search path", which is not the same as
    # "public": a role that already selects its schema must keep working, and
    # defaulting to public here would override it.
    db_schema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class QueryHistory(Base):
    __tablename__ = "query_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    database_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("database_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    nl_query: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="success")
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SavedChart(Base):
    __tablename__ = "saved_charts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    database_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("database_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    nl_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    chart_type: Mapped[str] = mapped_column(String(30), default="bar")
    # Visual customization set in the Infographics panel after save. Deliberately
    # an opaque JSON blob: the server only stamps/validates its envelope
    # (`version`, size/depth) and never interprets its contents — the frontend
    # ChartRenderer is the single owner of what the keys mean. Null means "use
    # defaults". Rendered identically in the analyst preview and the published
    # client report.
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    schema_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Draft/Publish lifecycle (Phase 4). Defaults keep today's "just saved" UX.
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class PublishedReport(Base):
    """A *publication record* for a chart — not a copy of it.

    The chart (``SavedChart``) stays the editable draft; this row records that a
    given chart is published to an org, by whom, and with what visibility. The
    report always renders from the live chart + a fresh re-query, so editing the
    draft updates the report. Keeping publication separate leaves room for
    versioning later (e.g. "Publish v2") without overwriting an existing record.
    """
    __tablename__ = "published_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    chart_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("saved_charts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    published_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | revoked
    visibility: Mapped[str] = mapped_column(String(20), default="organization", nullable=False)  # organization | private
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    chart: Mapped["SavedChart"] = relationship(lazy="joined")


class AuditLog(Base):
    """An audit event modelled as (actor, entity, action) within an organization.

    ``entity_type`` + ``action`` are a clean split (e.g. entity "report", action
    "publish") so the dashboard can filter each axis independently, rather than
    parsing a dotted "report.publish". ``detail`` holds event-specific metadata.
    """
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # bare verb: login, create, publish
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # Correlation id — ties together every event from one request/session so a
    # whole flow (login → query → publish → client run) can be traced as a unit.
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    # HMAC over this row's content, keyed by the server secret — see
    # app_db/audit_integrity.py. Nullable because rows written before signing
    # existed have none; the verifier reports those rather than passing them.
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Database-assigned monotonic counter, so a *deleted* row leaves a visible
    # hole — a per-row signature cannot say how many rows there should be.
    # ``FetchedValue`` means the application never supplies it: PostgreSQL fills
    # it from a sequence, and on an engine without one it stays NULL and the
    # verifier reports gap detection as unavailable rather than clean.
    seq: Mapped[int | None] = mapped_column(
        BigInteger, FetchedValue(), nullable=True, index=True,
    )


# ── Background jobs (Milestone 6) ─────────────────────────────────────────────

class ScheduledJob(Base):
    """A user-owned recurring task run off the request path by the in-process
    scheduler. ``job_type`` selects the executor; ``target_ref`` points at the
    thing it acts on (a published report or a connection)."""
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)  # report_refresh | context_rebuild
    target_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)  # report id / connection id
    schedule_kind: Mapped[str] = mapped_column(String(20), nullable=False)  # interval | daily | weekly | manual
    schedule_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # e.g. {"hour":6,"minute":0}
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # success | error | running
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class JobRun(Base):
    """One execution of a ScheduledJob — the job history."""
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("scheduled_jobs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success | error
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ExecutionToken(Base):
    """A single-use, short-lived grant to run one specific server-stored SQL
    statement — the binding between a reviewed ``/query`` plan and its ``/execute``.

    A write generated from a natural-language plan can only reach a customer
    database by redeeming one of these. The exact SQL is stored server-side when
    ``/query`` produces it, so the client cannot alter it after the user confirms.
    The row is bound to the issuing user and a hash of the execution context
    (engine/host/port/database/user) so a token cannot be replayed against a
    different target, and it is consumed atomically (``consumed_at``) on first use.

    Only the SHA-256 of the opaque token is stored as ``id`` (never the raw token,
    which is returned to the client once) — mirroring how ``api_keys`` are held.
    """
    __tablename__ = "execution_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token) hex
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    database_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("database_connections.id", ondelete="SET NULL"), nullable=True
    )
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    safety_category: Mapped[str] = mapped_column(String(20), nullable=False)  # read | write
    # sha256 of engine|host|port|database|user — the target the plan was reviewed
    # against; redemption must resolve to the same context.
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """An in-app notification for a user (job completion/failure, etc.)."""
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)  # info | success | error
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


# ── AI provider management ────────────────────────────────────────────────────

class AIProviderConfig(Base):
    """A configured AI provider for an organization — provider-as-record, so
    supporting a new provider is configuration, not code.

    ``adapter`` names the transport implementation (``openai_compatible`` for any
    OpenAI Chat Completions-compatible endpoint, ``ollama`` for a local server);
    vendor differences reduce to ``base_url`` + ``model``. The API key is stored
    Fernet-encrypted (``api_key_encrypted``), exactly like ``DatabaseConnection``
    passwords, and is never returned to clients.

    ``priority`` selects the org's active/default provider: 0 = inactive, 1 = the
    active default, 2+ reserved for future routing policies (an int rather than a
    bool so those never need a migration). ``fallback_provider_id`` optionally
    chains to another record, tried when the primary fails for infrastructure
    reasons — the generic replacement for the old hardcoded "hybrid" mode.

    (A future split into a separate ``ai_provider_secrets`` table — mirroring a
    ``DatabaseCredential`` split — is deliberately deferred; the key stays inline.)
    """
    __tablename__ = "ai_provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)  # openai_compatible | ollama
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet; null for keyless/local
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 inactive, 1 active default
    fallback_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ai_provider_configs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_ai_provider_org_name"),)


# ── Relation graph ────────────────────────────────────────────────────────────

class SchemaSnapshot(Base):
    """A cached, point-in-time introspection of a connection's schema — tables,
    columns, primary keys, and foreign keys — stored as JSON.

    The relation graph (Infographics → Relations) is built entirely from these
    snapshots, never from live connections, so the database-level overview scales
    to hundreds of databases: rendering it is one app-DB read plus an in-memory
    build, not hundreds of live introspections. Exactly one snapshot per
    connection (``connection_id`` unique); refreshing overwrites it. Ownership and
    org scoping are derived from the parent ``DatabaseConnection`` — deleting the
    connection removes its snapshot.
    """
    __tablename__ = "schema_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("database_connections.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # A hash of the snapshot's structural shape, so a refresh can tell whether the
    # schema actually changed without diffing the whole blob.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)  # {"tables": [...]}
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# ── Dashboards (a collection of charts + narrative) ───────────────────────────

class Dashboard(Base):
    """An analyst-authored collection of saved charts, each with an optional
    description — a report in everything but name (the client's term is
    "dashboard", so that is what it is called throughout).

    Like a chart, a dashboard holds **no data**: it references charts, and every
    chart re-runs live when the dashboard is opened. Editing a pinned chart
    therefore updates every dashboard it appears in, with no sync step.
    """
    __tablename__ = "dashboards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft | published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    items: Mapped[list["DashboardItem"]] = relationship(
        back_populates="dashboard", cascade="all, delete-orphan",
        order_by="DashboardItem.position", lazy="selectin",
    )


class DashboardItem(Base):
    """One pinned chart within a dashboard, with its optional description.

    The description belongs to the *pin*, not to the chart: the same chart can be
    pinned to two dashboards and carry a different narrative in each. Deleting the
    chart removes the pin (cascade) — a dashboard never points at a chart that no
    longer exists.
    """
    __tablename__ = "dashboard_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dashboard_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chart_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("saved_charts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "manual" | "ai" — provenance, so the UI can mark an AI-written description
    # as such. An analyst editing an AI description flips this back to manual.
    description_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    dashboard: Mapped["Dashboard"] = relationship(back_populates="items")
    chart: Mapped["SavedChart"] = relationship(lazy="joined")

    # A chart is pinned to a given dashboard at most once; re-pinning updates the
    # existing item rather than stacking duplicates.
    __table_args__ = (UniqueConstraint("dashboard_id", "chart_id", name="uq_dashboard_item_chart"),)


class PublishedDashboard(Base):
    """A *publication record* for a dashboard — deliberately the same shape as
    ``PublishedReport``.

    The ``Dashboard`` stays the editable draft; this row records that it is
    published to an org, by whom, and with what visibility. Clients always render
    from the live dashboard plus a fresh per-chart re-query, so editing the draft
    updates what they see and re-publishing never duplicates a record.
    """
    __tablename__ = "published_dashboards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dashboard_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    published_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=False)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | revoked
    visibility: Mapped[str] = mapped_column(String(20), default="organization", nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    dashboard: Mapped["Dashboard"] = relationship(lazy="joined")


# ── Insights Engine ───────────────────────────────────────────────────────────

class InsightCache(Base):
    """A generated insight bundle, keyed by what produced it (Phase 7).

    ``cache_key`` is a SHA-256 over ``sql · result_hash · connection_id ·
    prompt_version · provider_label``, so:

    * the same query over unchanged data is a hit, and re-opening a result costs
      nothing;
    * changed data is a miss (``result_hash`` covers the shaped context, including
      its statistics);
    * a prompt-version bump invalidates every entry by construction — no manual
      purge step is ever needed when the guardrails change.

    Rows are scoped to the owning user and organization so one tenant's insights
    are never served to another, and they are removed with the connection.
    """
    __tablename__ = "insight_cache"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("database_connections.id", ondelete="CASCADE"),
        index=True, nullable=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The serialized InsightBundle — rebuilt via InsightBundle.from_dict.
    bundle: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
