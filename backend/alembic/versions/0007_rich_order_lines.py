"""rich order lines

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08

Order lines gain structure (kind, key/parent link), vendor metadata, price
adjustment inputs and source boxes; requests gain quote metadata and
server-computed net/tax totals. Existing lines are backfilled as plain items
keyed by their ``line_order``.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINE_KINDS = "('item', 'surcharge', 'discount', 'shipping', 'fee', 'alternative', 'optional')"


def upgrade() -> None:
    op.add_column(
        "order_lines",
        sa.Column("line_type", sa.Text(), server_default="item", nullable=False),
    )
    op.add_column("order_lines", sa.Column("line_key", sa.Text(), nullable=True))
    op.add_column("order_lines", sa.Column("parent_key", sa.Text(), nullable=True))
    op.add_column("order_lines", sa.Column("position", sa.Text(), nullable=True))
    op.add_column("order_lines", sa.Column("vendor_item_number", sa.Text(), nullable=True))
    op.add_column(
        "order_lines", sa.Column("list_unit_price", sa.Numeric(12, 2), nullable=True)
    )
    op.add_column(
        "order_lines", sa.Column("adjustment_percent", sa.Numeric(7, 3), nullable=True)
    )
    op.add_column("order_lines", sa.Column("sources", sa.JSON(), nullable=True))
    op.add_column("order_lines", sa.Column("source_text", sa.Text(), nullable=True))
    op.add_column("order_lines", sa.Column("article_id", sa.Uuid(), nullable=True))

    # Backfill: every existing line is an unlinked item keyed by its position.
    op.execute(
        "UPDATE order_lines SET line_key = CAST(line_order AS TEXT), "
        "line_type = 'item', parent_key = NULL"
    )

    # batch_alter_table lets the constraint changes also run on SQLite.
    with op.batch_alter_table("order_lines") as batch:
        batch.alter_column("line_key", existing_type=sa.Text(), nullable=False)
        batch.create_unique_constraint(
            "uq_order_lines_request_key", ["request_id", "line_key"]
        )
        batch.create_check_constraint(
            "ck_order_lines_line_type", f"line_type IN {LINE_KINDS}"
        )
        batch.create_foreign_key(
            "fk_order_lines_article_id",
            "articles",
            ["article_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.add_column("procurement_requests", sa.Column("quote_number", sa.Text(), nullable=True))
    op.add_column("procurement_requests", sa.Column("valid_until", sa.Date(), nullable=True))
    op.add_column(
        "procurement_requests",
        sa.Column("currency", sa.Text(), server_default="USD", nullable=True),
    )
    op.add_column(
        "procurement_requests", sa.Column("net_total", sa.Numeric(12, 2), nullable=True)
    )
    op.add_column(
        "procurement_requests", sa.Column("tax_total", sa.Numeric(12, 2), nullable=True)
    )
    op.add_column(
        "procurement_requests", sa.Column("extraction_warnings", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("procurement_requests") as batch:
        batch.drop_column("extraction_warnings")
        batch.drop_column("tax_total")
        batch.drop_column("net_total")
        batch.drop_column("currency")
        batch.drop_column("valid_until")
        batch.drop_column("quote_number")

    with op.batch_alter_table("order_lines") as batch:
        batch.drop_constraint("fk_order_lines_article_id", type_="foreignkey")
        batch.drop_constraint("ck_order_lines_line_type", type_="check")
        batch.drop_constraint("uq_order_lines_request_key", type_="unique")
        batch.drop_column("article_id")
        batch.drop_column("source_text")
        batch.drop_column("sources")
        batch.drop_column("adjustment_percent")
        batch.drop_column("list_unit_price")
        batch.drop_column("vendor_item_number")
        batch.drop_column("position")
        batch.drop_column("parent_key")
        batch.drop_column("line_key")
        batch.drop_column("line_type")
