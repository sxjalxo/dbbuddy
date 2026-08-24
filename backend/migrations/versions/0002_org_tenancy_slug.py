"""org tenancy — is_default + slug, default-org backfill, required organization_id

Adds the tenancy columns, generates URL-safe slugs for existing orgs, ensures a
single default organization, back-fills every org-less user into it, and only
then makes ``users.organization_id`` NOT NULL (so the alter can't fail on
existing rows). Batch mode (configured in env.py) makes the ALTER/constraint
changes work on SQLite via table-rebuild.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-29
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORG_NAME = "Default Organization"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "org"


def _unique(base: str, used: set[str]) -> str:
    slug, i = base, 2
    while slug in used:
        slug, i = f"{base}-{i}", i + 1
    used.add(slug)
    return slug


def upgrade() -> None:
    # 1. Add columns in a state that's valid for existing rows (nullable / defaulted).
    op.add_column("organizations", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("organizations", sa.Column("slug", sa.String(length=100), nullable=True))

    bind = op.get_bind()

    # 2. Backfill slugs for existing organizations.
    used: set[str] = set()
    for oid, name in bind.execute(sa.text("SELECT id, name FROM organizations")).fetchall():
        bind.execute(
            sa.text("UPDATE organizations SET slug = :s WHERE id = :id"),
            {"s": _unique(_slugify(name), used), "id": oid},
        )

    # 3. Ensure exactly one default organization, then back-fill org-less users.
    default_id = bind.execute(
        sa.text("SELECT id FROM organizations WHERE is_default = :t"), {"t": True}
    ).scalar()
    if default_id is None:
        default_id = str(uuid.uuid4())
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, name, slug, is_default, created_at) "
                "VALUES (:id, :name, :slug, :is_default, :created_at)"
            ),
            {
                "id": default_id, "name": DEFAULT_ORG_NAME, "slug": _unique("default", used),
                "is_default": True, "created_at": datetime.now(timezone.utc),
            },
        )
    bind.execute(
        sa.text("UPDATE users SET organization_id = :oid WHERE organization_id IS NULL"),
        {"oid": default_id},
    )

    # 4. Enforce the new invariants now that data is clean.
    with op.batch_alter_table("organizations") as batch:
        batch.alter_column("slug", existing_type=sa.String(length=100), nullable=False)
        batch.create_unique_constraint("uq_organizations_slug", ["slug"])
    with op.batch_alter_table("users") as batch:
        batch.alter_column("organization_id", existing_type=sa.String(length=36), nullable=False)

    op.create_index("ix_users_organization_id", "users", ["organization_id"])

    # The baseline FK is ON DELETE SET NULL; with organization_id now NOT NULL that
    # already behaves as RESTRICT (a delete of a referenced org fails the NOT NULL
    # check). On Postgres we additionally retarget it to an explicit RESTRICT to
    # match the model; SQLite has no named-FK drop, so it keeps the equivalent
    # SET-NULL-on-NOT-NULL form.
    if bind.dialect.name == "postgresql":
        op.drop_constraint("users_organization_id_fkey", "users", type_="foreignkey")
        op.create_foreign_key(
            "users_organization_id_fkey", "users", "organizations",
            ["organization_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    op.drop_index("ix_users_organization_id", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("organization_id", existing_type=sa.String(length=36), nullable=True)
    with op.batch_alter_table("organizations") as batch:
        batch.drop_constraint("uq_organizations_slug", type_="unique")
    op.drop_column("organizations", "slug")
    op.drop_column("organizations", "is_default")
