"""header field source highlights

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08

Requests store the source boxes of the extracted header fields (vendor name,
tax id, department, quote number, valid-until) as JSON, keyed by field.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procurement_requests", sa.Column("field_sources", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("procurement_requests") as batch:
        batch.drop_column("field_sources")
