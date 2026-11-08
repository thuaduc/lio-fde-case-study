"""per-organization commodity groups, assigned per order line

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08

Commodity groups gain an owning organization (NULL = global template), a
booking-rule description and an archive flag; new rows draw ids from a
sequence starting at 10001 so the fixed template ids stay untouched. Order
lines gain their own commodity group.

Data steps (plain functions, so the test can run them on SQLite):
- copy the base templates (names without " — ") into every organization;
- re-point each request to its org's copy, matched on category and on the name
  with any " — suffix" stripped (requests without an org, or without a match,
  keep their id);
- backfill every order line with its request's group.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def downgrade() -> None:
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
