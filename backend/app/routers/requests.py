import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.deps import get_current_membership, require_buyer_or_admin
from app.db.session import get_db
from app.models import (
    Article,
    CommodityGroup,
    OrderLine,
    Organization,
    OrganizationMember,
    ProcurementRequest,
    RequestActivity,
    RequestDocument,
)
from app.schemas.requests import (
    ApprovalDecision,
    OrderLineIn,
    OriginalQuote,
    RequestActivityOut,
    RequestCreate,
    RequestOut,
    RequestUpdate,
    StatusUpdate,
)

from app.services.line_links import (
    ADJUSTMENTS,
    active_keys,
    inherited_groups,
    parent_allowed,
    primary_group,
)

router = APIRouter(prefix="/requests", tags=["requests"])

MAX_PDF_BYTES = 10 * 1024 * 1024

# Human-readable labels for the fields we track in the activity log.
_FIELD_LABELS = {
    "requestor_name": "requestor",
    "title": "title",
    "vendor_name": "vendor",
    "vat_id": "tax ID",
    "commodity_group_id": "commodity group",
    "total_cost": "total cost",
    "department": "department",
    "order_lines": "order lines",
    "quote_number": "quote number",
    "valid_until": "valid until",
    "currency": "currency",
    "tax_total": "tax total",
}


def _load_request_options():
    return (
        selectinload(ProcurementRequest.order_lines),
        selectinload(ProcurementRequest.commodity_group),
    )


def _get_org_request(
    db: Session, request_id: uuid.UUID, organization_id: uuid.UUID
) -> ProcurementRequest:
    request = db.scalar(
        select(ProcurementRequest)
        .options(*_load_request_options())
        # Refresh already-loaded relationships (e.g. after an update commit).
        .execution_options(populate_existing=True)
        .where(
            ProcurementRequest.id == request_id,
            ProcurementRequest.organization_id == organization_id,
        )
    )
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found or you do not have permission to access it",
        )
    return request


def _validate_commodity_group(
    db: Session,
    organization_id: uuid.UUID,
    commodity_group_id: int,
    current: int | None = None,
) -> None:
    """A newly chosen group must be the caller's org's and active.

    The value already stored (``current``) is accepted unchanged, even if it
    was archived since or isn't the org's (a legacy request still pointing at
    a global template)."""
    if current is not None and commodity_group_id == current:
        return
    group = db.get(CommodityGroup, commodity_group_id)
    if group is None or group.organization_id != organization_id:
        raise _unprocessable(f"Unknown commodity group id: {commodity_group_id}")
    if group.archived_at is not None:
        raise _unprocessable(f"Commodity group {group.name!r} is archived")


def _assign_groups(
    db: Session,
    organization_id: uuid.UUID,
    lines: list[OrderLineIn],
    requested_group: int | None,
    request: ProcurementRequest | None = None,
) -> tuple[list[OrderLineIn], int]:
    """Validate and complete the line groups; return them with the primary group.

    - On update, a line that doesn't send ``commodityGroupId`` (an older
      client) keeps the stored group of the line with the same key, unless
      the client changed the request-level group: then it takes that one.
    - Line groups and the request group must be the org's and active; a value
      already stored on the same line / request may stay unchanged regardless.
    - Adjustments with a parent and no group take the parent's group.
    - Any other active line without a group takes the client's request-level
      ``commodityGroupId`` (older API clients send only that one).
    - Every active line must end up with a group: spend is booked per line.
    - The primary group is the largest active spend.
    """
    stored = (
        {row.line_key: row.commodity_group_id for row in request.order_lines}
        if request is not None
        else {}
    )
    current = request.commodity_group_id if request is not None else None
    changed = requested_group is not None and requested_group != current
    lines = [
        ln if "commodity_group_id" in ln.model_fields_set or ln.key not in stored
        else ln.model_copy(update={
            "commodity_group_id": requested_group if changed else stored[ln.key]
        })
        for ln in lines
    ]
    for ln in lines:
        if ln.commodity_group_id is not None:
            _validate_commodity_group(
                db, organization_id, ln.commodity_group_id, stored.get(ln.key)
            )
    lines = [
        ln if ln.commodity_group_id == gid
        else ln.model_copy(update={"commodity_group_id": gid})
        for ln, gid in zip(lines, inherited_groups(lines, override=False))
    ]

    if requested_group is not None:
        _validate_commodity_group(db, organization_id, requested_group, current)
    active = active_keys(lines)
    lines = [
        ln.model_copy(update={"commodity_group_id": requested_group})
        if ln.key in active and ln.commodity_group_id is None
        else ln
        for ln in lines
    ]
    missing = [
        ln.position_description.strip() or ln.position or ln.key
        for ln in lines
        if ln.key in active and ln.commodity_group_id is None
    ]
    if missing:
        raise _unprocessable(
            "Every order line needs a commodity group "
            f"(missing on: {', '.join(missing)})"
        )
    group_id = primary_group(lines)
    if group_id is None:
        raise _unprocessable("A commodity group is required")
    return lines, group_id


def _enforce_required_fields(
    db: Session, organization_id: uuid.UUID, payload: RequestCreate | RequestUpdate
) -> None:
    """Enforce the org's configurable required fields (see OrganizationSettings)."""
    org = db.get(Organization, organization_id)
    required = (org.settings or {}).get("required_fields", []) if org else []
    missing = [field for field in required if not getattr(payload, field, None)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing fields required by your organization: {', '.join(missing)}",
        )


def _approval_threshold(db: Session, organization_id: uuid.UUID) -> float | None:
    """The org's auto-approval threshold (see OrganizationSettings), or None."""
    org = db.get(Organization, organization_id)
    value = (org.settings or {}).get("approval_threshold") if org else None
    return None if value is None else float(value)


def _below_threshold(grand_total: float, threshold: float | None) -> bool:
    return threshold is not None and round(grand_total, 2) < round(threshold, 2)


def _auto_approve(
    db: Session, request: ProcurementRequest, grand_total: float, threshold: float
) -> None:
    """Approve a request on the org's threshold: no approver, logged as system."""
    request.approval_status = "approved"
    request.approved_by = None
    request.approved_at = datetime.now(timezone.utc)
    _log_activity(
        db,
        request.id,
        None,
        "auto_approved",
        f"Auto-approved: total ${grand_total:,.2f} is below the "
        f"${threshold:,.2f} threshold",
        {"total": round(grand_total, 2), "threshold": threshold},
    )
    _status_follows_approval(db, request, None)


def _set_status(
    db: Session,
    request: ProcurementRequest,
    new_status: str,
    actor_id: uuid.UUID | None,
) -> None:
    """Change the request's status and log it; a no-op when it already has it."""
    if request.status == new_status:
        return
    _log_activity(
        db,
        request.id,
        actor_id,
        "status_changed",
        f"Status changed from {request.status} to {new_status}",
        {"from": request.status, "to": new_status},
    )
    request.status = new_status


def _status_follows_approval(
    db: Session, request: ProcurementRequest, actor_id: uuid.UUID | None
) -> None:
    """Move the status along after an approval decision.

    Approval moves an open request in progress, rejection closes it, and a reset
    to pending reopens a request in progress. A request someone closed stays
    closed.
    """
    if request.approval_status == "approved" and request.status == "open":
        _set_status(db, request, "in_progress", actor_id)
    elif request.approval_status == "rejected":
        _set_status(db, request, "closed", actor_id)
    elif request.approval_status == "pending" and request.status == "in_progress":
        _set_status(db, request, "open", actor_id)


def _log_activity(
    db: Session,
    request_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    summary: str,
    detail: dict | None = None,
) -> None:
    db.add(
        RequestActivity(
            request_id=request_id,
            actor_id=actor_id,
            action=action,
            summary=summary,
            detail=detail,
        )
    )


def _dump_field_sources(fs) -> dict | None:
    if fs is None:
        return None
    return {k: [b.model_dump() for b in boxes] for k, boxes in fs.items()}


def _dump_warnings(warnings) -> list | None:
    if warnings is None:
        return None
    return [w.model_dump() for w in warnings]


def _json_sources(sources) -> str:
    return json.dumps(
        [b.model_dump() if hasattr(b, "model_dump") else b for b in sources or []],
        sort_keys=True,
    )


def _num(value, places: int):
    return None if value is None else round(float(value), places)


def _order_lines_signature(lines) -> list[tuple]:
    """Comparable view of lines; works for stored OrderLine rows and prepared
    OrderLineIn payloads (field names are mapped by the caller)."""
    return [
        (
            line.position_description,
            _num(line.unit_price, 2),
            _num(line.amount, 4),
            line.unit,
            _num(line.total_price, 2),
            line.key,
            line.parent_key,
            line.kind,
            line.position,
            line.vendor_item_number,
            _num(line.list_unit_price, 2),
            _num(line.adjustment_percent, 3),
            _json_sources(line.sources),
            line.source_text,
            line.original_quote.model_dump() if line.original_quote else None,
            str(line.article_id) if line.article_id else None,
            line.commodity_group_id,
        )
        for line in lines
    ]


class _StoredLine:
    """Adapter exposing a stored OrderLine under the OrderLineIn field names."""

    def __init__(self, row: OrderLine):
        self.__dict__.update(
            position_description=row.position_description,
            unit_price=row.unit_price,
            amount=row.amount,
            unit=row.unit,
            total_price=row.total_price,
            key=row.line_key,
            parent_key=row.parent_key,
            kind=row.line_type,
            position=row.position,
            vendor_item_number=row.vendor_item_number,
            list_unit_price=row.list_unit_price,
            adjustment_percent=row.adjustment_percent,
            sources=row.sources,
            source_text=row.source_text,
            original_quote=OriginalQuote.model_validate(row.original_quote) if row.original_quote else None,
            article_id=row.article_id,
            commodity_group_id=row.commodity_group_id,
        )


def _stored_signature(request: ProcurementRequest) -> list[tuple]:
    rows = sorted(request.order_lines, key=lambda line: line.line_order)
    return _order_lines_signature([_StoredLine(r) for r in rows])


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _validate_links(lines: list[OrderLineIn]) -> None:
    """Check keys and parent links (shared by create and update)."""
    by_key: dict[str, OrderLineIn] = {}
    for line in lines:
        if line.key in by_key:
            raise _unprocessable(f"Duplicate line key: {line.key!r}")
        by_key[line.key] = line
    for line in lines:
        if line.parent_key is None:
            continue
        label = f"Line {line.key!r} ({line.kind})"
        parent = by_key.get(line.parent_key)
        if parent is None:
            raise _unprocessable(
                f"{label} references unknown parent {line.parent_key!r}"
            )
        if not parent_allowed(line.kind, parent.kind):
            if line.kind == "item":
                raise _unprocessable(f"{label} cannot have a parent")
            if line.kind in ADJUSTMENTS:
                raise _unprocessable(
                    f"{label} cannot be attached to another adjustment ({parent.key!r})"
                )
            raise _unprocessable(
                f"{label} must belong to an item, but parent {parent.key!r} is a {parent.kind}"
            )


def _prepare_lines(lines: list[OrderLineIn]) -> list[OrderLineIn]:
    """Fill defaults, apply the server-side line math and validate links.

    - missing key -> str(line_order) (same convention as the 0007 backfill)
    - list price + adjustment % -> effective unit price
    - total = round(amount * unit price, 2); discounts are always negative
    """
    prepared: list[OrderLineIn] = []
    for index, line in enumerate(lines):
        # Normalise to the stored column precision up front, so what we compute
        # with is exactly what a round trip through the DB returns.
        amount = round(line.amount, 4)  # Numeric(12, 4)
        list_price = _num(line.list_unit_price, 2)  # Numeric(12, 2)
        adjustment = _num(line.adjustment_percent, 3)  # Numeric(7, 3)
        unit_price = round(line.unit_price, 2)
        if list_price is not None and adjustment is not None:
            unit_price = round(list_price * (1 + adjustment / 100), 2)
        total_price = round(amount * unit_price, 2)
        if line.kind == "discount" and total_price > 0:
            total_price = -total_price
            unit_price = -unit_price
        prepared.append(
            line.model_copy(
                update={
                    "key": line.key or str(index + 1),
                    "amount": amount,
                    "list_unit_price": list_price,
                    "adjustment_percent": adjustment,
                    "unit_price": unit_price,
                    "total_price": total_price,
                }
            )
        )
    _validate_links(prepared)
    return prepared


def compute_totals(
    lines: list[OrderLineIn], tax_total: float | None
) -> tuple[float, float]:
    """Return (net, grand): net sums the active lines; grand adds the tax."""
    keys = active_keys(lines)
    net = round(sum(ln.total_price for ln in lines if ln.key in keys), 2)
    return net, round(net + (tax_total or 0), 2)


def _validate_articles(
    db: Session, organization_id: uuid.UUID, lines: list[OrderLineIn]
) -> None:
    for article_id in {ln.article_id for ln in lines if ln.article_id}:
        article = db.get(Article, article_id)
        if article is None or article.organization_id != organization_id:
            raise _unprocessable(f"Unknown article id: {article_id}")


def _build_lines(lines: list[OrderLineIn]) -> list[OrderLine]:
    return [
        OrderLine(
            position_description=line.position_description,
            unit_price=line.unit_price,
            amount=line.amount,
            unit=line.unit,
            total_price=line.total_price,
            line_order=index + 1,
            line_type=line.kind,
            line_key=line.key,
            parent_key=line.parent_key,
            position=line.position,
            vendor_item_number=line.vendor_item_number,
            list_unit_price=line.list_unit_price,
            adjustment_percent=line.adjustment_percent,
            sources=[b.model_dump() for b in line.sources],
            source_text=line.source_text,
            original_quote=line.original_quote.model_dump() if line.original_quote else None,
            article_id=line.article_id,
            commodity_group_id=line.commodity_group_id,
        )
        for index, line in enumerate(lines)
    ]


def _changed_fields(
    request: ProcurementRequest,
    payload: RequestUpdate,
    lines: list[OrderLineIn],
    grand_total: float,
    tax_total: float | None,
    commodity_group_id: int,
) -> list[str]:
    """Return the field keys whose value differs between the request and payload."""
    changed: list[str] = []
    if request.requestor_name != payload.requestor_name:
        changed.append("requestor_name")
    if request.title != payload.title:
        changed.append("title")
    if request.vendor_name != payload.vendor_name:
        changed.append("vendor_name")
    if (request.vat_id or "") != (payload.vat_id or ""):
        changed.append("vat_id")
    if request.commodity_group_id != commodity_group_id:
        changed.append("commodity_group_id")
    if round(float(request.total_cost or 0), 2) != round(grand_total, 2):
        changed.append("total_cost")
    if (request.department or "") != (payload.department or ""):
        changed.append("department")
    sent = payload.model_fields_set
    if "quote_number" in sent and (request.quote_number or "") != (payload.quote_number or ""):
        changed.append("quote_number")
    if "valid_until" in sent and request.valid_until != payload.valid_until:
        changed.append("valid_until")
    if "currency" in sent and payload.currency and request.currency != payload.currency:
        changed.append("currency")
    if _num(request.tax_total, 2) != _num(tax_total, 2):
        changed.append("tax_total")
    if _stored_signature(request) != _order_lines_signature(lines):
        changed.append("order_lines")
    return changed


@router.get("", response_model=list[RequestOut])
def list_requests(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestOut]:
    requests = db.scalars(
        select(ProcurementRequest)
        .options(*_load_request_options())
        .where(ProcurementRequest.organization_id == membership.organization_id)
        .order_by(ProcurementRequest.created_at.desc())
    )
    return [RequestOut.model_validate(r) for r in requests]


@router.get("/{request_id}", response_model=RequestOut)
def get_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    return RequestOut.model_validate(
        _get_org_request(db, request_id, membership.organization_id)
    )


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    draft = None
    if payload.draft_id is not None:
        from app.routers.drafts import get_own_draft

        draft = get_own_draft(db, payload.draft_id, membership, light=False)
        if draft is None:
            raise _unprocessable("Draft not found")
    _enforce_required_fields(db, membership.organization_id, payload)
    lines = _prepare_lines(payload.order_lines)
    _validate_articles(db, membership.organization_id, lines)
    # The primary group comes first: everything below works with it.
    lines, group_id = _assign_groups(
        db, membership.organization_id, lines, payload.commodity_group_id
    )
    net_total, grand_total = compute_totals(lines, payload.tax_total)

    # Vendor fields are already cleaned up during extraction, so we can persist
    # them directly here.
    request = ProcurementRequest(
        user_id=membership.user_id,
        requestor_name=payload.requestor_name,
        title=payload.title,
        vendor_name=payload.vendor_name,
        vat_id=payload.vat_id,
        commodity_group_id=group_id,
        total_cost=grand_total,
        net_total=net_total,
        tax_total=payload.tax_total,
        quote_number=payload.quote_number,
        valid_until=payload.valid_until,
        currency=payload.currency or "USD",
        extraction_warnings=_dump_warnings(payload.extraction_warnings),
        field_sources=_dump_field_sources(payload.field_sources),
        department=payload.department,
        organization_id=membership.organization_id,
        created_by=membership.user_id,
        status="open",
    )
    request.order_lines = _build_lines(lines)
    db.add(request)
    db.flush()
    _log_activity(db, request.id, membership.user_id, "created", "Request created")
    threshold = _approval_threshold(db, membership.organization_id)
    if _below_threshold(grand_total, threshold):
        _auto_approve(db, request, grand_total, threshold)
    if draft is not None:
        if draft.document is not None:
            _attach_document(
                db,
                request.id,
                membership.user_id,
                draft.document_filename or "document.pdf",
                draft.document,
            )
        db.delete(draft)
    db.commit()

    request = _get_org_request(db, request.id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.put("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: uuid.UUID,
    payload: RequestUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)
    _enforce_required_fields(db, membership.organization_id, payload)

    lines = _prepare_lines(payload.order_lines)
    _validate_articles(db, membership.organization_id, lines)
    # The primary group comes first: change detection below compares it.
    lines, group_id = _assign_groups(
        db, membership.organization_id, lines, payload.commodity_group_id, request
    )
    sent = payload.model_fields_set
    # tax_total is never recalculated: keep the stored value unless the client sends one.
    tax_total = payload.tax_total if "tax_total" in sent else (
        None if request.tax_total is None else float(request.tax_total)
    )
    net_total, grand_total = compute_totals(lines, tax_total)

    # Order lines stay editable until the request is closed. Approval moves a
    # request in progress, so an approved request's lines remain editable.
    lines_changed = _stored_signature(request) != _order_lines_signature(lines)
    if request.status == "closed" and lines_changed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order lines can't be edited once the request is closed.",
        )

    changed = _changed_fields(
        request, payload, lines, grand_total, tax_total, group_id
    )

    # A changed grand total re-evaluates the approval: below the org's
    # threshold it is auto-approved, otherwise an earlier approval is reset.
    # Rejections stand either way.
    old_total = round(float(request.total_cost or 0), 2)
    total_changed = old_total != round(grand_total, 2)
    threshold = _approval_threshold(db, membership.organization_id)
    auto_approve = (
        total_changed
        and request.approval_status != "rejected"
        and _below_threshold(grand_total, threshold)
    )
    approval_reset = (
        total_changed and not auto_approve and request.approval_status == "approved"
    )

    request.requestor_name = payload.requestor_name
    request.title = payload.title
    request.vendor_name = payload.vendor_name
    request.vat_id = payload.vat_id
    request.commodity_group_id = group_id
    request.total_cost = grand_total
    request.net_total = net_total
    request.tax_total = tax_total
    request.department = payload.department
    if "quote_number" in sent:
        request.quote_number = payload.quote_number
    if "valid_until" in sent:
        request.valid_until = payload.valid_until
    if "currency" in sent and payload.currency:
        request.currency = payload.currency
    if "extraction_warnings" in sent:
        request.extraction_warnings = _dump_warnings(payload.extraction_warnings)
    if "field_sources" in sent:
        request.field_sources = _dump_field_sources(payload.field_sources)

    # Replace order lines only when they changed, so a header-only save keeps
    # the stored rows (and their ids) untouched.
    if lines_changed:
        # Delete the old rows first: inserting the new ones in the same flush
        # would hit the (request_id, line_key) unique constraint.
        request.order_lines.clear()
        db.flush()
        request.order_lines = _build_lines(lines)

    if auto_approve:
        _auto_approve(db, request, grand_total, threshold)
    elif approval_reset:
        request.approval_status = "pending"
        request.approved_by = None
        request.approved_at = None
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "approval_reset",
            "Approval reset: total changed",
            {"from": old_total, "to": grand_total},
        )
        _status_follows_approval(db, request, membership.user_id)

    if changed:
        labels = [_FIELD_LABELS.get(f, f) for f in changed]
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "updated",
            f"Updated {', '.join(labels)}",
            {"changed_fields": changed},
        )
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/status", response_model=RequestOut)
def update_status(
    request_id: uuid.UUID,
    payload: StatusUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.status != payload.status:
        _set_status(db, request, payload.status, membership.user_id)
        db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/approval", response_model=RequestOut)
def decide_approval(
    request_id: uuid.UUID,
    payload: ApprovalDecision,
    membership: OrganizationMember = Depends(require_buyer_or_admin),
    db: Session = Depends(get_db),
) -> RequestOut:
    """Approve or reject a pending request (buyers and admins only)."""
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.approval_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Request has already been {request.approval_status}.",
        )

    request.approval_status = payload.decision
    request.approved_by = membership.user_id
    request.approved_at = datetime.now(timezone.utc)
    _log_activity(
        db,
        request.id,
        membership.user_id,
        payload.decision,
        f"Request {payload.decision}",
    )
    _status_follows_approval(db, request, membership.user_id)
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.get("/{request_id}/activity", response_model=list[RequestActivityOut])
def get_activity(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestActivityOut]:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    activity = db.scalars(
        select(RequestActivity)
        .where(RequestActivity.request_id == request_id)
        .order_by(RequestActivity.created_at.desc())
    )
    return [RequestActivityOut.model_validate(a) for a in activity]


async def read_pdf_upload(file: UploadFile) -> bytes:
    """Validate an uploaded PDF (type and size) and return its bytes."""
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Please upload a PDF file"
        )
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF exceeds the 10MB limit",
        )
    return content


def _attach_document(
    db: Session,
    request_id: uuid.UUID,
    actor_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> None:
    """Create/replace the request's RequestDocument and log it (no commit)."""
    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request_id)
    )
    if document is None:
        document = RequestDocument(request_id=request_id)
        db.add(document)
    document.filename = filename
    document.content_type = "application/pdf"
    document.content = content
    _log_activity(
        db, request_id, actor_id, "document_attached", f"Attached document {filename}"
    )


@router.put("/{request_id}/document", status_code=status.HTTP_204_NO_CONTENT)
async def upload_document(
    request_id: uuid.UUID,
    file: UploadFile,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    """Attach (or replace) the original PDF for a request."""
    request = _get_org_request(db, request_id, membership.organization_id)

    content = await read_pdf_upload(file)
    _attach_document(
        db, request.id, membership.user_id, file.filename or "document.pdf", content
    )
    db.commit()


@router.get("/{request_id}/document")
def get_document(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> Response:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request_id)
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No document attached"
        )
    return Response(
        content=document.content,
        media_type=document.content_type,
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    request = _get_org_request(db, request_id, membership.organization_id)
    db.delete(request)  # order lines, activity, document cascade
    db.commit()
