"""Idempotent database seeding: reference data, organizations, demo accounts.

Safe to run on every startup: it inserts only the rows that are missing, so it
never duplicates data and never touches anything users created. Run manually
with:

    python -m app.seed
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.data.articles import ARTICLES
from app.data.commodity_groups import COMMODITY_GROUPS
from app.data.suppliers import SUPPLIERS
from app.db.session import SessionLocal
from app.services.commodity_groups import copy_templates
from app.models import (
    Article,
    CommodityGroup,
    Organization,
    OrganizationMember,
    Profile,
    Supplier,
    User,
)

logger = logging.getLogger(__name__)

# Tenants + their seeded accounts (all share DEMO_PASSWORD). "Acme Corp" is the
# development organization — the only one with all three roles. The enterprise
# tenants each have a single admin so the multi-tenant setup is tangible.
DEMO_PASSWORD = "demo1234"

# (org_name, org_slug, [(email, full_name, role), ...])
ORG_ACCOUNTS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "Acme Corp",
        "acme",
        [
            ("admin@acme.com", "Alex Admin", "admin"),
            ("buyer@acme.com", "Bea Buyer", "buyer"),
            ("requester@acme.com", "Rene Requester", "requester"),
        ],
    ),
    ("Walmart", "walmart", [("admin@walmart.com", "Walmart Admin", "admin")]),
    ("Schaeffler", "schaeffler", [("admin@schaeffler.com", "Schaeffler Admin", "admin")]),
    ("Siemens", "siemens", [("admin@siemens.com", "Siemens Admin", "admin")]),
    ("BMW", "bmw", [("admin@bmw.com", "BMW Admin", "admin")]),
]


def seed_commodity_groups(db: Session) -> int:
    """Insert the global template groups that aren't present (keyed by id)."""
    existing = set(db.scalars(select(CommodityGroup.id)))
    new = [
        CommodityGroup(id=cg_id, category=category, name=name)
        for cg_id, category, name in COMMODITY_GROUPS
        if cg_id not in existing
    ]
    db.add_all(new)
    return len(new)


def seed_org_commodity_groups(db: Session) -> int:
    """Give every seeded org its copy of the base templates (idempotent)."""
    db.flush()  # templates added above must be visible to the copy
    added = 0
    for _name, slug, _accounts in ORG_ACCOUNTS:
        org = db.scalar(select(Organization).where(Organization.slug == slug))
        if org is not None:
            added += copy_templates(db, org.id)
    return added


# Acme's booking rules (customer brief, task 3), as editable seed data:
# (category, name, description). Each description states only the rule, so it
# doesn't pull unrelated items (laptops, copy paper) away from their own groups.
# The description is only filled in when the group has none, so an admin's
# edits are never overwritten.
ACME_BOOKING_GROUPS: list[tuple[str, str, str]] = [
    (
        "Facility Management",
        "Electrical Engineering",
        "Cable ties are always booked here.",
    ),
    (
        "Information Technology",
        "IT Hardware",
        "Printer toner is always booked here.",
    ),
    (
        "Office & Administration",
        "Office Supplies",
        "Small software subscriptions are always booked here.",
    ),
]

# Earlier seeded wording (too broad). A stored description equal to one of
# these is replaced once; any other text is an admin's and stays.
SUPERSEDED_ACME_DESCRIPTIONS: frozenset[str] = frozenset(
    {
        "Electrical installations, components and wiring material. Cable ties "
        "always have to be booked under Electrical Engineering.",
        "Computer hardware, peripherals and printer supplies. Printer toner "
        "always has to be booked under IT Hardware.",
        "Office consumables and everyday office needs. Small software "
        "subscriptions always have to be booked under Office Supplies.",
    }
)


def seed_acme_booking_groups(db: Session) -> int:
    """Add Acme's booking-rule groups / descriptions; returns rows changed."""
    org = db.scalar(select(Organization).where(Organization.slug == "acme"))
    if org is None:
        return 0
    db.flush()
    changed = 0
    for category, name, description in ACME_BOOKING_GROUPS:
        group = db.scalar(
            select(CommodityGroup).where(
                CommodityGroup.organization_id == org.id,
                CommodityGroup.category == category,
                CommodityGroup.name == name,
            )
        )
        if group is None:
            db.add(
                CommodityGroup(
                    organization_id=org.id,
                    category=category,
                    name=name,
                    description=description,
                )
            )
            changed += 1
        elif (
            group.description is None
            or group.description in SUPERSEDED_ACME_DESCRIPTIONS
        ):
            group.description = description
            changed += 1
    return changed


def seed_suppliers(db: Session, organization_id: uuid.UUID) -> int:
    """Insert the org's suppliers that aren't already present (keyed by name)."""
    existing = set(
        db.scalars(
            select(Supplier.name).where(Supplier.organization_id == organization_id)
        )
    )
    new = [
        Supplier(
            organization_id=organization_id,
            name=s["name"],
            description=s["description"],
            category=s["category"],
            country=s["country"],
            vat_id=s["vat_id"],
            website=s["website"],
            email=s["email"],
        )
        for s in SUPPLIERS
        if s["name"] not in existing
    ]
    db.add_all(new)
    return len(new)


def seed_articles(db: Session, organization_id: uuid.UUID) -> int:
    """Insert the org's catalog articles that aren't already present (by number).

    Each article resolves its supplier by name within the same organization;
    articles whose supplier is not seeded are skipped.
    """
    # Suppliers added earlier in this transaction are pending (the session is
    # not autoflushing), so flush before resolving their ids.
    db.flush()
    supplier_ids = {
        name: sid
        for sid, name in db.execute(
            select(Supplier.id, Supplier.name).where(
                Supplier.organization_id == organization_id
            )
        )
    }
    existing = set(
        db.scalars(
            select(Article.article_number).where(
                Article.organization_id == organization_id
            )
        )
    )
    new = []
    for a in ARTICLES:
        if a["article_number"] in existing:
            continue
        supplier_id = supplier_ids.get(a["supplier_name"])
        if supplier_id is None:
            continue
        new.append(
            Article(
                organization_id=organization_id,
                article_number=a["article_number"],
                supplier_id=supplier_id,
                description=a["description"],
                unit_price=a["unit_price"],
                currency=a["currency"],
                unit=a["unit"],
                quantity=a["quantity"],
            )
        )
    db.add_all(new)
    return len(new)


# Per-org settings applied when the key is unset (never overwrites an admin's
# choice). Acme asked for requests under $2,000 to skip buyer approval.
ORG_DEFAULT_SETTINGS: dict[str, dict] = {"acme": {"approval_threshold": 2000}}


def seed_org_settings(db: Session) -> int:
    """Fill missing default settings keys; returns the number of orgs updated."""
    updated = 0
    for slug, defaults in ORG_DEFAULT_SETTINGS.items():
        org = db.scalar(select(Organization).where(Organization.slug == slug))
        if org is None:
            continue
        current = dict(org.settings or {})
        missing = {k: v for k, v in defaults.items() if k not in current}
        if missing:
            org.settings = {**current, **missing}  # reassign so JSONB is dirtied
            updated += 1
    return updated


def seed_organizations_and_accounts(db: Session) -> tuple[int, int]:
    """Create the tenants and their accounts (keyed by slug / email)."""
    orgs_created = 0
    users_created = 0
    for org_name, org_slug, accounts in ORG_ACCOUNTS:
        org = db.scalar(select(Organization).where(Organization.slug == org_slug))
        if org is None:
            org = Organization(name=org_name, slug=org_slug)
            db.add(org)
            db.flush()
            orgs_created += 1
        for email, full_name, role in accounts:
            if db.scalar(select(User).where(User.email == email)) is not None:
                continue
            user = User(email=email, hashed_password=hash_password(DEMO_PASSWORD))
            db.add(user)
            db.flush()
            db.add(Profile(id=user.id, email=email, full_name=full_name))
            db.add(
                OrganizationMember(
                    organization_id=org.id, user_id=user.id, role=role
                )
            )
            users_created += 1
    return orgs_created, users_created


def seed(db: Session) -> dict[str, int]:
    """Seed everything. Returns counts of newly created rows per kind.

    Suppliers and articles are master data owned by the development
    organization (Acme Corp); other tenants start with an empty catalog.
    """
    orgs_created, users_created = seed_organizations_and_accounts(db)
    db.flush()
    dev_org = db.scalar(select(Organization).where(Organization.slug == "acme"))
    dev_org_id = dev_org.id if dev_org else None
    counts = {
        "commodity_groups": seed_commodity_groups(db),
        # Org copies need the templates above and the orgs created earlier.
        "org_commodity_groups": seed_org_commodity_groups(db),
        "acme_booking_groups": seed_acme_booking_groups(db),
        # Master data belongs to Acme; skip if that org somehow isn't seeded.
        "suppliers": seed_suppliers(db, dev_org_id) if dev_org_id else 0,
        # Articles reference suppliers by name, so this must run after suppliers.
        "articles": seed_articles(db, dev_org_id) if dev_org_id else 0,
        "organizations": orgs_created,
        "org_settings": seed_org_settings(db),
        "users": users_created,
    }
    db.commit()
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as db:
        counts = seed(db)
    summary = ", ".join(f"+{v} {k}" for k, v in counts.items())
    print(f"Seed complete: {summary}")


if __name__ == "__main__":
    main()
