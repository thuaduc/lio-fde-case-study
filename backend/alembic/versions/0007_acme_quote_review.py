"""Acme quote review: rich lines, sources, drafts, org groups and catalog cache.

Revision ID: 0007
Revises: 0006

Existing lines become plain items and inherit their request's group. Global
commodity templates are copied per organization and existing requests are
repointed to their organization's copy.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINE_KINDS = "('item', 'surcharge', 'discount', 'shipping', 'fee', 'alternative', 'optional')"
SEQ = "commodity_groups_id_seq"
VARIANT_SEP = " — "


def _base_name(name: str) -> str:
    return name.split(VARIANT_SEP, 1)[0]


def copy_templates_to_orgs(conn) -> None:
    """Copy each base template into every org that lacks it (idempotent)."""
    conn.execute(
        sa.text(
            "INSERT INTO commodity_groups (organization_id, category, name) "
            "SELECT o.id, t.category, t.name "
            "FROM organizations o CROSS JOIN commodity_groups t "
            "WHERE t.organization_id IS NULL AND t.name NOT LIKE :variant "
            "AND NOT EXISTS (SELECT 1 FROM commodity_groups c "
            "  WHERE c.organization_id = o.id AND c.category = t.category "
            "  AND c.name = t.name) "
            "ORDER BY o.id, t.id"
        ),
        {"variant": f"%{VARIANT_SEP}%"},
    )


def _copies(conn) -> dict[tuple, int]:
    rows = conn.execute(
        sa.text(
            "SELECT id, organization_id, category, name FROM commodity_groups "
            "WHERE organization_id IS NOT NULL"
        )
    )
    return {(org, cat, name): gid for gid, org, cat, name in rows}


def repoint_requests(conn) -> None:
    """Point requests at their org's copy of the template they used."""
    templates = {
        gid: (cat, _base_name(name))
        for gid, cat, name in conn.execute(
            sa.text(
                "SELECT id, category, name FROM commodity_groups "
                "WHERE organization_id IS NULL"
            )
        )
    }
    copies = _copies(conn)
    requests = conn.execute(
        sa.text(
            "SELECT id, organization_id, commodity_group_id FROM procurement_requests "
            "WHERE organization_id IS NOT NULL AND commodity_group_id IS NOT NULL"
        )
    ).all()
    for rid, org, gid in requests:
        if gid not in templates:
            continue
        target = copies.get((org, *templates[gid]))
        if target is not None:
            conn.execute(
                sa.text(
                    "UPDATE procurement_requests SET commodity_group_id = :g WHERE id = :r"
                ),
                {"g": target, "r": rid},
            )


def backfill_line_groups(conn) -> None:
    conn.execute(
        sa.text(
            "UPDATE order_lines SET commodity_group_id = ("
            "SELECT r.commodity_group_id FROM procurement_requests r "
            "WHERE r.id = order_lines.request_id) "
            "WHERE commodity_group_id IS NULL"
        )
    )


def repoint_requests_to_templates(conn) -> None:
    """Downgrade: org groups -> the base template of the same category and name,
    else NULL (an admin-created group has no template)."""
    templates = {
        (cat, name): gid
        for gid, cat, name in conn.execute(
            sa.text(
                "SELECT id, category, name FROM commodity_groups "
                "WHERE organization_id IS NULL AND name NOT LIKE :variant"
            ),
            {"variant": f"%{VARIANT_SEP}%"},
        )
    }
    org_groups = {
        gid: (cat, name) for (_org, cat, name), gid in _copies(conn).items()
    }
    requests = conn.execute(
        sa.text(
            "SELECT id, commodity_group_id FROM procurement_requests "
            "WHERE commodity_group_id IS NOT NULL"
        )
    ).all()
    for rid, gid in requests:
        if gid not in org_groups:
            continue
        conn.execute(
            sa.text("UPDATE procurement_requests SET commodity_group_id = :g WHERE id = :r"),
            {"g": templates.get(org_groups[gid]), "r": rid},
        )


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

    op.add_column(
        "procurement_requests", sa.Column("field_sources", sa.JSON(), nullable=True)
    )

    op.create_table(
        "request_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("document", sa.LargeBinary(), nullable=True),
        sa.Column("document_filename", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_request_drafts_user_org_updated",
        "request_drafts",
        ["user_id", "organization_id", "updated_at"],
    )

    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    with op.batch_alter_table("commodity_groups") as batch:
        batch.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_commodity_groups_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_commodity_groups_organization_id", ["organization_id"])

    if is_pg:
        op.execute(f"CREATE SEQUENCE {SEQ} START WITH 10001 OWNED BY commodity_groups.id")
        op.execute(
            f"ALTER TABLE commodity_groups ALTER COLUMN id SET DEFAULT nextval('{SEQ}')"
        )

    with op.batch_alter_table("order_lines") as batch:
        batch.add_column(sa.Column("commodity_group_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_order_lines_commodity_group_id",
            "commodity_groups",
            ["commodity_group_id"],
            ["id"],
            ondelete="SET NULL",
        )

    copy_templates_to_orgs(conn)
    repoint_requests(conn)
    backfill_line_groups(conn)

    op.create_table(
        "article_embeddings",
        sa.Column("text_hash", sa.Text(), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("article_embeddings")

    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    with op.batch_alter_table("order_lines") as batch:
        batch.drop_constraint("fk_order_lines_commodity_group_id", type_="foreignkey")
        batch.drop_column("commodity_group_id")

    repoint_requests_to_templates(conn)
    conn.execute(sa.text("DELETE FROM commodity_groups WHERE organization_id IS NOT NULL"))

    if is_pg:
        op.execute("ALTER TABLE commodity_groups ALTER COLUMN id DROP DEFAULT")
        op.execute(f"DROP SEQUENCE IF EXISTS {SEQ}")

    with op.batch_alter_table("commodity_groups") as batch:
        batch.drop_index("ix_commodity_groups_organization_id")
        batch.drop_constraint("fk_commodity_groups_organization_id", type_="foreignkey")
        batch.drop_column("archived_at")
        batch.drop_column("description")
        batch.drop_column("organization_id")

    op.drop_index("ix_request_drafts_user_org_updated", table_name="request_drafts")
    op.drop_table("request_drafts")

    with op.batch_alter_table("procurement_requests") as batch:
        batch.drop_column("field_sources")
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
