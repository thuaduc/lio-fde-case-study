"""Keep quoted values when a line is replaced from the catalog."""
import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("order_lines", sa.Column("original_quote", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("order_lines", "original_quote")
