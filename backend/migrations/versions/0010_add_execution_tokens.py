"""add execution tokens

Adds the ``execution_tokens`` table backing the confirmed-write flow: a
planner-generated write from ``/query`` mints a single-use, short-lived token
bound to the exact server-stored SQL, the issuing user, and a hash of the
execution context. ``/execute`` redeems the token and runs the stored SQL, so a
client cannot modify the statement after the user confirmed it. Only the SHA-256
of the token is stored (``id``); the raw token is returned to the client once.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),  # sha256(token)
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("database_connection_id", sa.String(length=36), nullable=True),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("safety_category", sa.String(length=20), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["database_connection_id"], ["database_connections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_execution_tokens_user_id", "execution_tokens", ["user_id"])
    op.create_index("ix_execution_tokens_expires_at", "execution_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_execution_tokens_expires_at", table_name="execution_tokens")
    op.drop_index("ix_execution_tokens_user_id", table_name="execution_tokens")
    op.drop_table("execution_tokens")
