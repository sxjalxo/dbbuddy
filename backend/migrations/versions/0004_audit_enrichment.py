"""audit enrichment — organization_id, ip_address, entity/action split

Adds ``organization_id`` (so the dashboard can scope by tenant) and
``ip_address`` to audit_logs, back-fills org from each event's actor, and
normalizes legacy dotted actions ("chart.publish") into a bare ``action``
("publish") + ``entity_type`` ("chart"). Adds indexes for the common filters.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("organization_id", sa.String(length=36), nullable=True))
    op.add_column("audit_logs", sa.Column("ip_address", sa.String(length=45), nullable=True))

    bind = op.get_bind()

    # Back-fill organization from the actor's org for existing events.
    bind.execute(
        sa.text(
            "UPDATE audit_logs SET organization_id = ("
            "  SELECT u.organization_id FROM users u WHERE u.id = audit_logs.user_id"
            ") WHERE organization_id IS NULL AND user_id IS NOT NULL"
        )
    )

    # Normalize legacy dotted actions: "chart.publish" → entity_type="chart" (if
    # unset), action="publish". Only rows that still contain a dot.
    rows = bind.execute(
        sa.text("SELECT id, action, entity_type FROM audit_logs WHERE action LIKE '%.%'")
    ).fetchall()
    for rid, action, entity_type in rows:
        prefix, _, verb = action.partition(".")
        bind.execute(
            sa.text("UPDATE audit_logs SET action = :a, entity_type = :e WHERE id = :id"),
            {"a": verb or action, "e": entity_type or prefix, "id": rid},
        )

    # On Postgres add a real FK; SQLite can't ADD a named FK post-hoc, and org
    # scoping is enforced in the app layer regardless.
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_audit_logs_organization_id", "audit_logs", "organizations",
            ["organization_id"], ["id"], ondelete="SET NULL",
        )

    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization_id", table_name="audit_logs")
    op.drop_column("audit_logs", "ip_address")
    op.drop_column("audit_logs", "organization_id")
