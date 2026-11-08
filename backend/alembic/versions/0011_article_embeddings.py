"""article embedding cache

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-08

Embeddings keyed by the SHA-256 of the normalised description (lowercase,
whitespace collapsed), shared across organizations and suppliers.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_embeddings",
        sa.Column("text_hash", sa.Text(), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("article_embeddings")
