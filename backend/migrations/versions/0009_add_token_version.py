"""add refresh-token version to users

Adds ``users.token_version`` — the monotonic counter that backs stateless
refresh-token revocation. A refresh token embeds the version it was minted with
and is accepted only while it still matches the user's current value; bumping the
column (logout, password change, MFA disable, account deactivation) invalidates
every outstanding refresh token at once, with no denylist or external store.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
