"""add email verification

Adds ``users.email_verified`` / ``users.email_verified_at`` and the
``email_verification_tokens`` table (same shape and rules as password-reset
tokens: hashed at rest, single use, short lived).

**Existing rows are backfilled as verified.** Enforcement is opt-in
(``REQUIRE_EMAIL_VERIFICATION``), but a deployment that turns it on later must not
discover that every account created before the upgrade is now locked out. Those
accounts were created under the rules that applied at the time; retroactively
invalidating them is an outage, not a hardening. New rows default to false.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        # sa.false() renders per dialect. A literal "0" is accepted by SQLite and
        # rejected by PostgreSQL, which types the column as boolean.
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Grandfather everything that already exists — see the module docstring.
    # `= true`, not `= 1`: PostgreSQL will not coerce an integer to boolean, and
    # SQLite accepts either. The portable spelling is the boolean one.
    op.execute("UPDATE users SET email_verified = true")

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_verification_tokens_user_id", "email_verification_tokens", ["user_id"],
    )
    op.create_index(
        "ix_email_verification_tokens_token_hash", "email_verification_tokens",
        ["token_hash"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_tokens_token_hash",
                  table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_user_id",
                  table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
