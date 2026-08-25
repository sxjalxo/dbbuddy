"""add a schema name to saved connections

Adds ``database_connections.db_schema``: a namespace within the database, for
engines that have one. The engine and the CLI already accept it
(``DBConfig.db_schema``, ``dbbuddy … --schema``); the platform had nowhere to
store it, so a hosted user could not point at a schema at all.

Nullable, and deliberately **not** defaulted to ``public``. NULL means "follow the
connection's search path", which is the behaviour every existing connection has
today — writing ``public`` into existing rows would change what a role that
selects its own schema resolves to, which is a silent behaviour change dressed up
as a migration.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "database_connections",
        sa.Column("db_schema", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("database_connections", "db_schema")
