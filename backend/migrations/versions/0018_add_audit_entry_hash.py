"""add audit-log entry signatures

Adds ``audit_logs.entry_hash``: an HMAC over the row's content, keyed by the
server secret (see ``app_db/audit_integrity.py``). Database access alone is no
longer enough to alter an audit row undetectably.

Nullable, and existing rows are deliberately **not** back-filled. Signing them now
would be signing whatever they currently say — which is exactly the claim the
signature is supposed to support, and cannot, because nothing verified them in the
meantime. A row that predates signing is reported as unverified, which is the
truth about it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("entry_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_logs", "entry_hash")
