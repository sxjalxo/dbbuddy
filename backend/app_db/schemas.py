"""Pydantic request/response schemas for the application-database APIs."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# C0 control characters that must not reach a text column. NUL (\x00) in
# particular is stored fine by SQLite (dev + test) but rejected outright by
# PostgreSQL (the production app DB), so unsanitized input that passes every
# SQLite-backed test 500s in prod. We strip NUL and the other control bytes,
# keeping the usual whitespace (tab, newline, carriage-return).
_CONTROL_TRANS = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}

# Fields whose raw bytes are meaningful and must not be silently altered.
# Passwords are hashed/encrypted before storage (never written as raw text), so
# there is no PostgreSQL-NUL hazard and rewriting them would corrupt the secret.
_UNSANITIZED_FIELDS = {"password"}


class SanitizedModel(BaseModel):
    """Base for request models: strips control characters from string fields.

    Runs before type coercion so it also cleans values on their way into
    non-``str`` fields that were supplied as strings. Normal input (no control
    bytes) is unchanged.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _strip_control_chars(cls, value, info):
        if info.field_name in _UNSANITIZED_FIELDS or not isinstance(value, str):
            return value
        return value.translate(_CONTROL_TRANS)


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(SanitizedModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginResult(BaseModel):
    """Either the token pair (no MFA) or an MFA challenge to exchange next."""
    mfa_required: bool = False
    challenge_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    full_name: str | None
    is_active: bool
    organization_id: str | None
    mfa_enabled: bool
    roles: list[str]
    permissions: list[str]


# ── MFA / 2FA (Milestone 5) ──────────────────────────────────────────────────

class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    qr_svg: str


class MfaVerifyRequest(BaseModel):
    code: str


class MfaEnableOut(BaseModel):
    enabled: bool = True
    recovery_codes: list[str]  # shown exactly once


class MfaLoginRequest(BaseModel):
    challenge_token: str
    code: str  # a TOTP code or a recovery code


class MfaDisableRequest(BaseModel):
    password: str | None = None
    code: str | None = None  # current TOTP or recovery code; either re-auths


# ── Personal API keys (CLI / automation) ─────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ApiKeyUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ApiKeyOut(BaseModel):
    """Metadata for a key — never includes the secret."""
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    token_prefix: str
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    """Returned only at creation time; ``api_key`` is shown exactly once."""
    api_key: str


class ApiKeyExchangeRequest(BaseModel):
    api_key: str


# ── Organizations & user administration ──────────────────────────────────────

class OrgIn(SanitizedModel):
    name: str = Field(min_length=1, max_length=200)


class OrgPatch(SanitizedModel):
    name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    is_default: bool
    member_count: int
    created_at: datetime


class AdminUserOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_active: bool
    organization_id: str
    roles: list[str]
    created_at: datetime


class AdminUserCreate(SanitizedModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = Field(default=None, max_length=200)
    role: str = "analyst"
    organization_id: str | None = None  # platform-admin only; ignored for org admins


class AdminUserPatch(BaseModel):
    roles: list[str] | None = None
    is_active: bool | None = None
    organization_id: str | None = None  # platform-admin only


# ── Database connections ──────────────────────────────────────────────────────

# Length caps mirror the widths of the columns these fields are stored in
# (``models.DatabaseConnection``). Without them an oversized value passes every
# SQLite-backed test and then 500s on PostgreSQL — the same dev/prod split the
# control-character stripping above exists for.
class ConnectionIn(SanitizedModel):
    name: str = Field(min_length=1, max_length=200)
    engine: str  # mysql | postgresql | sqlserver (normalized by the router)
    host: str = Field(max_length=255)
    port: int | None = None
    # Not min_length-constrained: an empty username is valid for trusted-auth
    # connections (e.g. SQL Server integrated auth).
    username: str = Field(max_length=255)
    password: str
    database: str = Field(max_length=255)


class ConnectionUpdate(SanitizedModel):
    # All fields optional — only what's provided changes. A blank/omitted
    # ``password`` keeps the stored one (so editing other fields never forces the
    # user to re-type it); a non-empty value re-encrypts under the current key.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    engine: str | None = None
    host: str | None = Field(default=None, max_length=255)
    port: int | None = None
    username: str | None = Field(default=None, max_length=255)
    password: str | None = None
    database: str | None = Field(default=None, max_length=255)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    engine: str
    host: str
    port: int | None
    username: str
    database: str
    created_at: datetime
    # password is intentionally never returned. ``credentials_ok`` is False when
    # the stored password can no longer be decrypted (the at-rest key changed) —
    # the UI uses it to flag the connection for re-entry before it's used.
    credentials_ok: bool = True


# ── Query history ─────────────────────────────────────────────────────────────

class HistoryIn(SanitizedModel):
    nl_query: str
    sql: str | None = None
    status: str = "success"
    confidence: str | None = None
    pinned: bool = False
    database_connection_id: str | None = None


class HistoryPatch(BaseModel):
    pinned: bool


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    nl_query: str
    sql: str | None
    status: str
    confidence: str | None
    pinned: bool
    database_connection_id: str | None
    created_at: datetime


# ── Saved charts ──────────────────────────────────────────────────────────────

# Chart types the frontend knows how to render (unknown types fall back to a
# table view). Rejecting anything outside this set keeps a value like "wat" from
# being persisted and silently breaking the renderer.
VALID_CHART_TYPES = {
    "bar", "column", "line", "area", "pie", "doughnut", "scatter", "combo", "table",
}


def _check_chart_type(v):
    if v is not None and v not in VALID_CHART_TYPES:
        raise ValueError(
            f"Unsupported chart_type {v!r}; expected one of {sorted(VALID_CHART_TYPES)}."
        )
    return v


# Schema version stamped onto every stored chart `config`. The server treats the
# config as an opaque blob (the renderer owns its meaning) and only owns this
# envelope: if the config schema is ever redesigned, bump this and migrate on
# read. A config with no `version` predates the stamp and is by definition v1.
CHART_CONFIG_VERSION = 1

# The visual `config` is a shallow object. Reject pathologically nested or huge
# configs at input: they are small enough to slip past the request-body limit,
# but once stored they crash Pydantic's response serialization ("Circular
# reference detected (depth exceeded)") — a 500 on every subsequent read of the
# chart list. Traversal is iterative so validating the hostile payload can't
# itself overflow the stack. Structural only — no knowledge of palettes/colors.
_CONFIG_MAX_DEPTH = 5
_CONFIG_MAX_NODES = 5000


def _check_chart_config(v):
    if v is None:
        return v
    if not isinstance(v, dict):
        raise ValueError("config must be an object.")
    stack = [(v, 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        if depth > _CONFIG_MAX_DEPTH:
            raise ValueError("config is too deeply nested.")
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            nodes += 1
            if nodes > _CONFIG_MAX_NODES:
                raise ValueError("config is too large.")
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    version = v.get("version", CHART_CONFIG_VERSION)
    # ``bool`` is an ``int`` subclass — exclude it so {"version": true} is not
    # stored as a version and later compared as 1.
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("config.version must be a positive integer.")
    if version > CHART_CONFIG_VERSION:
        raise ValueError(
            f"config.version {version} is newer than this server supports "
            f"(max {CHART_CONFIG_VERSION})."
        )
    return {**v, "version": version}


class ChartIn(SanitizedModel):
    # Capped to the width of ``models.SavedChart.title`` (see ConnectionIn).
    title: str = Field(min_length=1, max_length=300)
    nl_query: str | None = None
    sql: str
    chart_type: str = "bar"
    config: dict | None = None
    schema_fingerprint: str | None = None
    database_connection_id: str | None = None
    # ``status`` is deliberately ignored on create — a chart only becomes
    # "published" through the publish flow (which also creates a PublishedReport).
    # Kept in the schema for backward compatibility with older clients.
    status: str = "draft"

    @field_validator("chart_type")
    @classmethod
    def _validate_chart_type(cls, v):
        return _check_chart_type(v)

    @field_validator("config")
    @classmethod
    def _validate_config(cls, v):
        return _check_chart_config(v)


class ChartUpdate(BaseModel):
    """Partial update from the Infographics customizer. Only visual attributes
    (type, colors, title) are editable — SQL/connection are fixed at save."""
    title: str | None = Field(default=None, min_length=1, max_length=300)
    chart_type: str | None = None
    config: dict | None = None

    @field_validator("chart_type")
    @classmethod
    def _validate_chart_type(cls, v):
        return _check_chart_type(v)

    @field_validator("config")
    @classmethod
    def _validate_config(cls, v):
        return _check_chart_config(v)


class ChartOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    nl_query: str | None
    sql: str
    chart_type: str
    config: dict | None = None
    schema_fingerprint: str | None
    status: str
    database_connection_id: str | None
    created_at: datetime
    updated_at: datetime


# ── Publishing & client reports (Milestone 3) ────────────────────────────────

class PublishRequest(BaseModel):
    # "organization" → any report:view user in the org; "private" → only publisher.
    visibility: str = "organization"


class PublicationOut(BaseModel):
    """Analyst-facing view of a chart's publication record."""
    model_config = ConfigDict(from_attributes=True)
    id: str
    chart_id: str
    status: str
    visibility: str
    published_at: datetime


class ReportOut(BaseModel):
    """Client-facing report metadata — note: no SQL and no credentials."""
    id: str
    chart_id: str
    title: str
    chart_type: str
    nl_query: str | None
    visibility: str
    published_at: datetime


class ReportRunOut(BaseModel):
    """Result of a live report re-query. On failure → needs_attention (never stale)."""
    ok: bool
    columns: list[str] = []
    rows: list[dict] = []
    needs_attention: bool = False
    message: str | None = None
    chart_type: str = "bar"
    config: dict | None = None


# ── Dashboards (collections of charts + narrative) ───────────────────────────

# A per-chart description is a paragraph of narrative, not an essay. Capped so a
# hostile or accidental payload cannot bloat a dashboard's stored rows; ``Text``
# columns would accept it, but nothing downstream benefits from more.
MAX_DASHBOARD_DESCRIPTION = 4000


class DashboardIn(SanitizedModel):
    # Capped to the width of ``models.Dashboard.title``.
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=MAX_DASHBOARD_DESCRIPTION)


class DashboardPatch(SanitizedModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=MAX_DASHBOARD_DESCRIPTION)


class DashboardItemIn(SanitizedModel):
    chart_id: str
    description: str | None = Field(default=None, max_length=MAX_DASHBOARD_DESCRIPTION)


class DashboardItemPatch(SanitizedModel):
    description: str | None = Field(default=None, max_length=MAX_DASHBOARD_DESCRIPTION)


class DashboardReorderIn(SanitizedModel):
    item_ids: list[str]


class DashboardDescribeIn(SanitizedModel):
    # Bypass the short-TTL result cache when drafting a description, so the
    # narrative is written against data the analyst just looked at.
    refresh: bool = False


class DashboardItemOut(BaseModel):
    id: str
    chart_id: str
    title: str
    chart_type: str
    nl_query: str | None
    description: str | None
    description_source: str | None
    position: int


class DashboardOut(BaseModel):
    id: str
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    items: list[DashboardItemOut] = []


class DashboardRunOut(BaseModel):
    """A dashboard opened by a client: every chart re-run live.

    Each entry in ``charts`` carries its own ``ok`` / ``needs_attention``, so one
    failing chart is reported in place while the rest render. ``fetched_at`` and
    ``cached`` per chart let the UI state how current the data actually is rather
    than implying it is instantaneous.
    """
    id: str
    title: str
    description: str | None
    charts: list[dict] = []
    cache_ttl_seconds: int = 0


# ── Audit dashboard (Milestone 4) ────────────────────────────────────────────

class AuditEntryOut(BaseModel):
    id: str
    user_id: str | None
    actor_email: str | None
    organization_id: str | None
    entity_type: str | None
    action: str
    entity_id: str | None
    detail: dict | None
    ip_address: str | None
    request_id: str | None
    created_at: datetime


class AuditSummaryOut(BaseModel):
    window_days: int
    total: int
    by_action: dict[str, int]
    by_entity: dict[str, int]
    active_users: int
    recent_failures: int


# ── Background jobs & notifications (Milestone 6) ─────────────────────────────

class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    job_type: str  # report_refresh | context_rebuild
    target_ref: str
    schedule_kind: str = "manual"  # interval | daily | weekly | manual
    schedule_config: dict | None = None
    enabled: bool = True


class JobPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_kind: str | None = None
    schedule_config: dict | None = None
    enabled: bool | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    job_type: str
    target_ref: str | None
    schedule_kind: str
    schedule_config: dict | None
    enabled: bool
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    job_id: str
    status: str
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    body: str | None
    level: str
    read: bool
    created_at: datetime


# ── AI providers (unified provider management) ────────────────────────────────

# Registered transport adapters. Kept in sync with dbbuddy_core.ai_providers
# (openai_compatible covers OpenAI/NVIDIA/OpenRouter/Groq/Together/Azure/etc.).
VALID_ADAPTERS = {"openai_compatible", "ollama"}


class AIProviderIn(SanitizedModel):
    name: str = Field(min_length=1, max_length=200)
    adapter: str
    base_url: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = None          # write-only; never returned
    enabled: bool = True
    fallback_provider_id: str | None = None

    @field_validator("adapter")
    @classmethod
    def _validate_adapter(cls, v):
        if v not in VALID_ADAPTERS:
            raise ValueError(f"Unsupported adapter {v!r}; expected one of {sorted(VALID_ADAPTERS)}.")
        return v

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v):
        # The server itself makes requests to this URL with the org's API key, so
        # it is an egress target chosen by the caller — see app_db.url_guard.
        from .url_guard import validate_outbound_url

        return validate_outbound_url(v)

    @model_validator(mode="after")
    def _require_base_url_for_compatible(self):
        # An OpenAI-compatible provider is meaningless without an endpoint to call.
        if self.adapter == "openai_compatible" and not (self.base_url or "").strip():
            raise ValueError("openai_compatible providers require a base_url.")
        return self


class AIProviderUpdate(SanitizedModel):
    # All optional — only supplied fields change. A blank/omitted api_key keeps the
    # stored key (so editing other fields never forces re-entering the secret).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    adapter: str | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = None
    enabled: bool | None = None
    fallback_provider_id: str | None = None

    @field_validator("adapter")
    @classmethod
    def _validate_adapter(cls, v):
        if v is not None and v not in VALID_ADAPTERS:
            raise ValueError(f"Unsupported adapter {v!r}; expected one of {sorted(VALID_ADAPTERS)}.")
        return v

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v):
        # Same egress guard as creation — otherwise PATCH is a trivial bypass.
        from .url_guard import validate_outbound_url

        return validate_outbound_url(v)


class AIProviderOut(BaseModel):
    id: str
    name: str
    adapter: str
    base_url: str | None
    model: str
    enabled: bool
    priority: int
    is_active: bool           # priority == 1 (the org's default)
    has_key: bool             # a key is stored (value never returned)
    # False when the stored key can no longer be decrypted (at-rest key changed);
    # the UI flags it for re-entry, mirroring DatabaseConnection.credentials_ok.
    credentials_ok: bool
    fallback_provider_id: str | None
    created_at: datetime
    updated_at: datetime


class AIProviderTestResult(BaseModel):
    ok: bool
    adapter: str
    model: str
    error: str | None = None
