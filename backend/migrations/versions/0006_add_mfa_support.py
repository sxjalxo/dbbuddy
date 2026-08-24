"""add MFA support to users

Adds TOTP two-factor columns to ``users``: ``mfa_enabled`` (gates the login
challenge), ``mfa_secret`` (Fernet-encrypted TOTP secret), and
``mfa_recovery_codes`` (JSON list of hashed single-use recovery codes).

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("mfa_secret", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("mfa_recovery_codes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "mfa_recovery_codes")
    op.drop_column("users", "mfa_secret")
    op.drop_column("users", "mfa_enabled")
