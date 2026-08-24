"""publication record — PublishedReport becomes a publication, not a chart copy

Renames ``saved_chart_id`` → ``chart_id``, adds ``status`` (active|revoked) and
``visibility`` (organization|private), back-fills ``organization_id`` from the
chart owner's org, and makes it NOT NULL. The chart itself (SavedChart) remains
the editable draft.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("published_reports", sa.Column("status", sa.String(length=20), nullable=False, server_default="active"))
    op.add_column("published_reports", sa.Column("visibility", sa.String(length=20), nullable=False, server_default="organization"))

    # Back-fill organization from the chart owner before enforcing NOT NULL.
    op.get_bind().execute(
        sa.text(
            "UPDATE published_reports SET organization_id = ("
            "  SELECT u.organization_id FROM saved_charts sc"
            "  JOIN users u ON u.id = sc.user_id"
            "  WHERE sc.id = published_reports.saved_chart_id"
            ") WHERE organization_id IS NULL"
        )
    )

    with op.batch_alter_table("published_reports") as batch:
        batch.alter_column("saved_chart_id", new_column_name="chart_id", existing_type=sa.String(length=36))
        batch.alter_column("organization_id", existing_type=sa.String(length=36), nullable=False)

    op.create_index("ix_published_reports_chart_id", "published_reports", ["chart_id"])
    op.create_index("ix_published_reports_organization_id", "published_reports", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_published_reports_organization_id", table_name="published_reports")
    op.drop_index("ix_published_reports_chart_id", table_name="published_reports")
    with op.batch_alter_table("published_reports") as batch:
        batch.alter_column("organization_id", existing_type=sa.String(length=36), nullable=True)
        batch.alter_column("chart_id", new_column_name="saved_chart_id", existing_type=sa.String(length=36))
    op.drop_column("published_reports", "visibility")
    op.drop_column("published_reports", "status")
