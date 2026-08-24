"""add chart visual config

Adds a nullable ``config`` JSON column to ``saved_charts`` holding the visual
customization set in the Infographics panel after a chart is saved: a named
palette plus per-series and per-category color overrides. Null means "use
defaults", so existing charts are unaffected.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("saved_charts", sa.Column("config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("saved_charts", "config")
