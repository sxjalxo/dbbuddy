"""Seed RBAC roles/permissions and bootstrap the default organization. Idempotent."""

import logging
import os

from sqlalchemy.orm import Session

from .models import Organization, Permission, Role, User

logger = logging.getLogger(__name__)

# Permission catalogue (action:resource). Extend as features land.
#
# Settings are split by blast radius so an Analyst can manage AI providers
# without touching platform-wide config:
#   settings:ai       — AI providers / API keys           (admin, org_admin, analyst)
#   settings:system   — JWT, SMTP, feature flags          (platform admin only)
#   settings:security — auth policy, MFA enforcement       (platform admin only)
PERMISSIONS: dict[str, str] = {
    "query:run": "Run natural-language / SQL queries against a connection",
    "query:write:manual": "Execute hand-written (raw) write SQL directly, bypassing the reviewed-plan flow",
    "connection:manage": "Create, edit, and remove ERP database connections",
    "schema:analyze": "Analyze a database schema / semantic layer",
    "history:read": "View query history",
    "chart:save": "Save charts (drafts)",
    "chart:publish": "Publish charts as client-visible reports",
    "report:view": "View published reports (read-only)",
    "user:manage": "Create users and assign/revoke roles (scoped to one's org unless org:manage)",
    "audit:read": "View audit logs",
    "org:manage": "Create and manage organizations across the platform (platform admin)",
    "settings:ai": "Configure AI providers and API keys",
    "settings:system": "Manage platform-wide system settings (JWT, SMTP, feature flags)",
    "settings:security": "Manage security settings (auth policy, MFA enforcement)",
    "job:manage": "Create and manage scheduled background jobs (report refresh, context rebuild)",
}

# Role → permissions. Mirrors the Platform-Admin / Org-Admin / Analyst / Client tiers.
#
# Scope (global vs. own-org) is enforced in the routers, keyed off whether the
# caller holds ``org:manage`` (platform admin) — it is not a separate permission
# per resource. ``org_admin`` deliberately lacks org:manage and the system/
# security settings, so it can run a single org but not the platform.
ROLES: dict[str, dict] = {
    "admin": {
        "description": "Platform administration — orgs, users, audit, system & security settings",
        "permissions": [
            "org:manage", "user:manage", "audit:read", "connection:manage", "report:view",
            "settings:ai", "settings:system", "settings:security",
        ],
    },
    "org_admin": {
        "description": "Organization administration — team members, org AI settings, connections, audit",
        "permissions": [
            "user:manage", "connection:manage", "report:view", "settings:ai",
            "audit:read",  # scoped to their own org (they lack org:manage)
            "job:manage",
        ],
    },
    "analyst": {
        "description": "Authoring — connect, query, analyze, save & publish charts",
        "permissions": [
            # query:write:manual preserves today's power-user behavior (an Analyst
            # may run hand-written write SQL). It is now an explicit, revocable
            # grant, so a tighter deployment can create an Analyst role without it
            # and confine writes to the reviewed-plan (execution-token) flow.
            "query:run", "query:write:manual", "connection:manage", "schema:analyze",
            "history:read", "chart:save", "chart:publish", "report:view",
            "settings:ai", "job:manage",
        ],
    },
    "user": {
        "description": "Read-only consumption of published reports",
        "permissions": ["report:view"],
    },
}

DEFAULT_ORG_NAME = "Default Organization"


def seed_roles_and_permissions(db: Session) -> None:
    perm_by_name: dict[str, Permission] = {}
    for name, desc in PERMISSIONS.items():
        perm = db.query(Permission).filter_by(name=name).one_or_none()
        if perm is None:
            perm = Permission(name=name, description=desc)
            db.add(perm)
        else:
            perm.description = desc  # keep descriptions in sync with the catalogue
        perm_by_name[name] = perm
    db.flush()

    for role_name, cfg in ROLES.items():
        role = db.query(Role).filter_by(name=role_name).one_or_none()
        if role is None:
            role = Role(name=role_name, description=cfg["description"])
            db.add(role)
        else:
            role.description = cfg["description"]
        wanted = {perm_by_name[p] for p in cfg["permissions"]}
        role.permissions = list(wanted)  # keep in sync if the catalogue changes
    db.flush()


def bootstrap_default_org(db: Session) -> Organization:
    """Ensure exactly one default organization exists and that no user is org-less.

    This is a *data* migration (safe on any backend): it creates the default org
    if missing and back-fills any pre-tenancy users into it, upholding the
    non-null ``users.organization_id`` invariant without a schema rewrite.
    """
    org = db.query(Organization).filter_by(is_default=True).one_or_none()
    if org is None:
        from .slug import unique_slug
        org = Organization(name=DEFAULT_ORG_NAME, slug=unique_slug(db, "default"), is_default=True)
        db.add(org)
        db.flush()

    backfilled = (
        db.query(User)
        .filter(User.organization_id.is_(None))
        .update({User.organization_id: org.id}, synchronize_session=False)
    )
    if backfilled:
        logger.info("Backfilled %d org-less user(s) into the default organization.", backfilled)
    return org


def bootstrap_ai_providers(db: Session, org: Organization) -> None:
    """Migrate legacy provider configuration into per-org provider records.

    Idempotent and non-destructive (existing records/keys are never overwritten).
    For the default org: ensure a local Ollama record, and convert any API keys
    still held in the legacy ``secrets_store`` (nemotron / openai) into records.
    If the org has no active provider yet, activate Local Ollama with a fallback to
    Nemotron when present — reproducing the old ``hybrid`` (local-first → nemotron)
    default via the generic fallback chain instead of a special mode.
    """
    import os

    from dbbuddy_core import secrets_store
    from dbbuddy_core.ai import DEFAULT_NEMOTRON_MODEL, DEFAULT_OPENAI_MODEL, OLLAMA_URL

    from .models import AIProviderConfig
    from .security import encrypt_secret

    def _ensure(name: str, **fields) -> AIProviderConfig:
        row = (
            db.query(AIProviderConfig)
            .filter_by(organization_id=org.id, name=name)
            .one_or_none()
        )
        if row is None:
            row = AIProviderConfig(organization_id=org.id, name=name, **fields)
            db.add(row)
            db.flush()
        return row

    local = _ensure(
        "Local Ollama", adapter="ollama",
        model=os.getenv("LOCAL_MODEL", "qwen2.5-coder:7b"),
        base_url=OLLAMA_URL, api_key_encrypted=None,
    )

    nemotron = None
    if secrets_store.has_api_key("nemotron"):
        nemotron = _ensure(
            "Production Nemotron", adapter="openai_compatible",
            base_url="https://integrate.api.nvidia.com/v1",
            model=os.getenv("NEMOTRON_MODEL", DEFAULT_NEMOTRON_MODEL),
            api_key_encrypted=encrypt_secret(secrets_store.get_api_key("nemotron")),
        )
    if secrets_store.has_api_key("openai"):
        _ensure(
            "Production OpenAI", adapter="openai_compatible",
            base_url="https://api.openai.com/v1",
            model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
            api_key_encrypted=encrypt_secret(secrets_store.get_api_key("openai")),
        )

    has_active = (
        db.query(AIProviderConfig)
        .filter_by(organization_id=org.id, priority=1)
        .first()
    )
    if not has_active:
        local.priority = 1
        if nemotron is not None and not local.fallback_provider_id:
            local.fallback_provider_id = nemotron.id
    db.flush()


def bootstrap_admin(db: Session, org: Organization) -> None:
    """Create a platform-admin account from env, if configured and not present.

    Set ADMIN_EMAIL and ADMIN_PASSWORD to provision the first admin (there is no
    self-service path to the admin role). A no-op otherwise, and idempotent.
    """
    email = os.getenv("ADMIN_EMAIL")
    password = os.getenv("ADMIN_PASSWORD")
    if not email or not password:
        return

    from .security import hash_password

    email = email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        return

    admin_role = db.query(Role).filter_by(name="admin").one_or_none()
    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name="Administrator",
        organization_id=org.id,
    )
    if admin_role:
        user.roles.append(admin_role)
    db.add(user)
    logger.info("Provisioned platform admin %s from environment.", email)
