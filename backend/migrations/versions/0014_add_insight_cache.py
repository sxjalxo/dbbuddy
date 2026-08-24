"""add insight cache (Insights Engine)

Adds the ``insight_cache`` table: generated insight bundles keyed by a SHA-256
over sql · result_hash · connection_id · prompt_version · provider. Repeating a
query over unchanged data is served from here instead of costing another provider
call, and a prompt-version bump invalidates every entry by construction.

Rows are scoped to the owning user/organization and are removed with the
connection they describe.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "insight_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=200), nullable=True),
        sa.Column("bundle", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["database_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key", name="uq_insight_cache_key"),
    )
    op.create_index("ix_insight_cache_cache_key", "insight_cache", ["cache_key"])
    op.create_index("ix_insight_cache_connection_id", "insight_cache", ["connection_id"])
    op.create_index("ix_insight_cache_user_id", "insight_cache", ["user_id"])
    op.create_index("ix_insight_cache_organization_id", "insight_cache", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_insight_cache_organization_id", table_name="insight_cache")
    op.drop_index("ix_insight_cache_user_id", table_name="insight_cache")
    op.drop_index("ix_insight_cache_connection_id", table_name="insight_cache")
    op.drop_index("ix_insight_cache_cache_key", table_name="insight_cache")
    op.drop_table("insight_cache")
