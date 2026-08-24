"""add dashboards (collections of charts + narrative)

Adds three tables:

* ``dashboards`` — an analyst-authored collection of charts. Holds no data.
* ``dashboard_items`` — one pinned chart per row, with the optional description
  that belongs to the *pin* (the same chart can carry a different narrative in
  each dashboard it appears in). Unique on (dashboard_id, chart_id), so
  re-pinning updates rather than duplicating.
* ``published_dashboards`` — a publication record, the same shape as
  ``published_reports``: the dashboard stays the editable draft and clients
  render from it live.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dashboards_user_id", "dashboards", ["user_id"])
    op.create_index("ix_dashboards_organization_id", "dashboards", ["organization_id"])

    op.create_table(
        "dashboard_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dashboard_id", sa.String(length=36), nullable=False),
        sa.Column("chart_id", sa.String(length=36), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_source", sa.String(length=20), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chart_id"], ["saved_charts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dashboard_id", "chart_id", name="uq_dashboard_item_chart"),
    )
    op.create_index("ix_dashboard_items_dashboard_id", "dashboard_items", ["dashboard_id"])
    op.create_index("ix_dashboard_items_chart_id", "dashboard_items", ["chart_id"])

    op.create_table(
        "published_dashboards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dashboard_id", sa.String(length=36), nullable=False),
        sa.Column("published_by", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="organization"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_published_dashboards_dashboard_id", "published_dashboards", ["dashboard_id"])
    op.create_index("ix_published_dashboards_organization_id", "published_dashboards", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_published_dashboards_organization_id", table_name="published_dashboards")
    op.drop_index("ix_published_dashboards_dashboard_id", table_name="published_dashboards")
    op.drop_table("published_dashboards")
    op.drop_index("ix_dashboard_items_chart_id", table_name="dashboard_items")
    op.drop_index("ix_dashboard_items_dashboard_id", table_name="dashboard_items")
    op.drop_table("dashboard_items")
    op.drop_index("ix_dashboards_organization_id", table_name="dashboards")
    op.drop_index("ix_dashboards_user_id", table_name="dashboards")
    op.drop_table("dashboards")
