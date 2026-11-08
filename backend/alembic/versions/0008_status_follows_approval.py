"""Status follows approval: backfill requests decided while still open.

Revision ID: 0008
Revises: 0007

Approving a request now moves it in progress and rejecting it closes it.
Requests decided before that change are still open; this moves them the same
way. Requests someone already moved by hand keep their status. Data only, so
downgrade leaves the rows as they are.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE procurement_requests SET status = 'in_progress' "
            "WHERE status = 'open' AND approval_status = 'approved'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE procurement_requests SET status = 'closed' "
            "WHERE status = 'open' AND approval_status = 'rejected'"
        )
    )


def downgrade() -> None:
    pass
