"""The caller's organization's commodity groups. Reading is open to every
member; changes are admin-only. Another org's group is a 404."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import get_current_membership, require_org_admin
from app.db.session import get_db
from app.models import CommodityGroup, OrderLine, OrganizationMember, ProcurementRequest
from app.schemas.commodity_groups import CommodityGroupCreate, CommodityGroupUpdate
from app.schemas.requests import CommodityGroupOut

router = APIRouter(prefix="/commodity-groups", tags=["commodity-groups"])


def _get_org_group(db: Session, group_id: int, membership: OrganizationMember) -> CommodityGroup:
    group = db.get(CommodityGroup, group_id)
    if group is None or group.organization_id != membership.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Commodity group not found"
        )
    return group


def _check_unique(
    db: Session, membership: OrganizationMember, category: str, name: str,
    exclude_id: int | None = None,
) -> None:
    query = select(CommodityGroup.id).where(
        CommodityGroup.organization_id == membership.organization_id,
        CommodityGroup.category == category,
        CommodityGroup.name == name,
    )
    if exclude_id is not None:
        query = query.where(CommodityGroup.id != exclude_id)
    if db.scalar(query) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A commodity group {category} › {name} already exists",
        )


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@router.get("", response_model=list[CommodityGroupOut])
def list_commodity_groups(
    include_archived: bool = False,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[CommodityGroupOut]:
    query = select(CommodityGroup).where(
        CommodityGroup.organization_id == membership.organization_id
    )
    if not include_archived:
        query = query.where(CommodityGroup.archived_at.is_(None))
    groups = db.scalars(query.order_by(CommodityGroup.category, CommodityGroup.name))
    return [CommodityGroupOut.model_validate(g) for g in groups]


@router.post("", response_model=CommodityGroupOut, status_code=status.HTTP_201_CREATED)
def create_commodity_group(
    payload: CommodityGroupCreate,
    membership: OrganizationMember = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> CommodityGroupOut:
    category, name = payload.category.strip(), payload.name.strip()
    _check_unique(db, membership, category, name)
    group = CommodityGroup(
        organization_id=membership.organization_id,
        category=category,
        name=name,
        description=_clean(payload.description),
    )
    db.add(group)
    db.commit()
    return CommodityGroupOut.model_validate(group)


@router.patch("/{group_id}", response_model=CommodityGroupOut)
def update_commodity_group(
    group_id: int,
    payload: CommodityGroupUpdate,
    membership: OrganizationMember = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> CommodityGroupOut:
    group = _get_org_group(db, group_id, membership)
    sent = payload.model_fields_set
    category = payload.category.strip() if payload.category else group.category
    name = payload.name.strip() if payload.name else group.name
    if (category, name) != (group.category, group.name):
        _check_unique(db, membership, category, name, exclude_id=group.id)
    group.category, group.name = category, name
    if "description" in sent:
        group.description = _clean(payload.description)
    db.commit()
    return CommodityGroupOut.model_validate(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_commodity_group(
    group_id: int,
    membership: OrganizationMember = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> Response:
    group = _get_org_group(db, group_id, membership)
    using = (
        select(ProcurementRequest.id)
        .where(ProcurementRequest.commodity_group_id == group.id)
        .union(select(OrderLine.request_id).where(OrderLine.commodity_group_id == group.id))
    )
    in_use = db.scalar(select(func.count()).select_from(using.subquery()))
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"In use by {in_use} requests — archive it instead",
        )
    db.delete(group)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _set_archived(db: Session, group_id: int, membership, archived: bool) -> CommodityGroupOut:
    group = _get_org_group(db, group_id, membership)
    if archived and group.archived_at is None:
        group.archived_at = datetime.now(timezone.utc)
    elif not archived:
        group.archived_at = None
    db.commit()
    return CommodityGroupOut.model_validate(group)


@router.post("/{group_id}/archive", response_model=CommodityGroupOut)
def archive_commodity_group(
    group_id: int,
    membership: OrganizationMember = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> CommodityGroupOut:
    return _set_archived(db, group_id, membership, True)


@router.post("/{group_id}/unarchive", response_model=CommodityGroupOut)
def unarchive_commodity_group(
    group_id: int,
    membership: OrganizationMember = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> CommodityGroupOut:
    return _set_archived(db, group_id, membership, False)
