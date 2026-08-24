"""add AI provider configs

Adds the ``ai_provider_configs`` table backing unified, provider-agnostic AI
management. Each row is an organization's configured provider: an ``adapter``
(``openai_compatible`` | ``ollama``), ``base_url`` + ``model``, an optional
Fernet-encrypted ``api_key_encrypted`` (null for local/keyless), an ``enabled``
flag, a ``priority`` (1 = the org's active default; 0 = inactive), and an optional
``fallback_provider_id`` chaining to another record for infra-failure fallback.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("adapter", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_provider_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fallback_provider_id"], ["ai_provider_configs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_ai_provider_org_name"),
    )
    op.create_index("ix_ai_provider_configs_organization_id", "ai_provider_configs", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_provider_configs_organization_id", table_name="ai_provider_configs")
    op.drop_table("ai_provider_configs")
