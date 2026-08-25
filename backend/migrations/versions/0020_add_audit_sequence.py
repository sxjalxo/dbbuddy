"""add a monotonic sequence to audit rows, so deletion is visible

``audit_logs.entry_hash`` (migration ``0018``) proves a row was not edited. It
says nothing about how many rows there should be, so deleting one outright left no
trace. This adds ``audit_logs.seq``, assigned by the database, so the rows carry
1, 2, 3, … and a missing 2 shows up without the application coordinating anything.

That last part is the point. A hash chain would also catch deletion, and was
rejected because computing "the previous row's hash" at insert time forks under
concurrent workers and reports tampering on an honest system. A sequence is
assigned by the database, so concurrency is its problem and it has already solved
it.

**PostgreSQL** gets a real ``SEQUENCE`` owned by the column, so the counter is
dropped with the table and can be interrogated for the tail check. **SQLite** gets
the column and nothing to fill it: there is no sequence object, and an
application-assigned counter would reintroduce exactly the coordination problem
the chain was rejected for. Rows there stay NULL and the verifier reports gap
detection as unavailable rather than clean, which is the truth about it.

Existing rows are deliberately **not** back-filled, for the same reason ``0018``
did not back-fill signatures: numbering them now would be inventing an order
nothing recorded, and dressing a guess up as evidence. They are reported as
unsequenced.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEQUENCE_NAME = "audit_logs_seq"


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("audit_logs", sa.Column("seq", sa.BigInteger(), nullable=True))

    if bind.dialect.name == "postgresql":
        # OWNED BY ties the sequence's lifetime to the column: drop the table and
        # the counter goes with it, rather than surviving as an orphan that a
        # later re-create would silently continue from.
        op.execute(f"CREATE SEQUENCE {SEQUENCE_NAME} OWNED BY audit_logs.seq")
        op.execute(
            f"ALTER TABLE audit_logs ALTER COLUMN seq "
            f"SET DEFAULT nextval('{SEQUENCE_NAME}')"
        )

    # Unique, not merely indexed: two rows sharing a value cannot come from a
    # sequence, so it is worth refusing rather than reporting. NULLs are exempt
    # from uniqueness on both engines, which is what leaves the un-backfilled
    # rows alone.
    op.create_index("ix_audit_logs_seq", "audit_logs", ["seq"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("ix_audit_logs_seq", table_name="audit_logs")
    op.drop_column("audit_logs", "seq")
    if bind.dialect.name == "postgresql":
        # OWNED BY drops it with the column; belt and braces for a partial state.
        op.execute(f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME}")
