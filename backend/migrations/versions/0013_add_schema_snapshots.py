"""add schema snapshots (relation graph)

Adds the ``schema_snapshots`` table: a cached, point-in-time introspection of a
connection's schema (tables/columns/PKs/FKs) stored as JSON. The Infographics
relation graph is built from these snapshots rather than from live connections,
so the database-level overview scales to hundreds of databases. One snapshot per
connection (unique ``connection_id``); it is removed when the connection is.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schema_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("table_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["database_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", name="uq_schema_snapshot_connection"),
    )
    op.create_index("ix_schema_snapshots_connection_id", "schema_snapshots", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_schema_snapshots_connection_id", table_name="schema_snapshots")
    op.drop_table("schema_snapshots")
