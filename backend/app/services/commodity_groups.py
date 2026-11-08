"""Per-organization commodity groups: template copies and active-group lookups."""
import uuid

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.models import CommodityGroup

# Template names containing this separator are variants, not base templates.
VARIANT_SEP = " — "


def copy_templates(db: Session, organization_id: uuid.UUID) -> int:
    """Copy the base templates the org doesn't have yet (keyed by category+name).

    Returns the number of groups added. Idempotent.
    """
    have = {
        tuple(row)
        for row in db.execute(
            select(CommodityGroup.category, CommodityGroup.name).where(
                CommodityGroup.organization_id == organization_id
            )
        )
    }
    templates = db.execute(
        select(CommodityGroup.category, CommodityGroup.name)
        .where(
            CommodityGroup.organization_id.is_(None),
            CommodityGroup.name.not_like(f"%{VARIANT_SEP}%"),
        )
        .order_by(CommodityGroup.id)
    )
    rows = [
        {"organization_id": organization_id, "category": c, "name": n}
        for c, n in templates
        if (c, n) not in have
    ]
    if rows:
        db.execute(insert(CommodityGroup), rows)
    return len(rows)


def active_groups(db: Session, organization_id: uuid.UUID | None) -> list[CommodityGroup]:
    """The org's non-archived groups, by category then name ([] without an org)."""
    if organization_id is None:
        return []
    return list(
        db.scalars(
            select(CommodityGroup)
            .where(
                CommodityGroup.organization_id == organization_id,
                CommodityGroup.archived_at.is_(None),
            )
            .order_by(CommodityGroup.category, CommodityGroup.name)
        )
    )
