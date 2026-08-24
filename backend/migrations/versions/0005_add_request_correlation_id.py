"""add request correlation id to audit_logs

Adds ``audit_logs.request_id`` so every event emitted during one request shares a
correlation id and a whole flow (login → query → publish → client run) can be
traced together.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("request_id", sa.String(length=36), nullable=True))
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_column("audit_logs", "request_id")
