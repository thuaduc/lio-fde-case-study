"""Add source highlights to requests extracted before a field had one.

    python -m app.backfill_field_sources

A request with a stored PDF that lacks a highlight for a filled field has its
quote extracted again, and only the highlights are kept. Stored values never
change and stored highlights stay. A highlight is added only where the new
extraction found the stored value, except the title and the total: they always
point to the rows naming the purchase and the quoted grand total. Needs
OPENAI_API_KEY; one model call per request that still lacks a highlight.
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement_request import ProcurementRequest, RequestDocument
from app.schemas.extraction import ExtractedVendorData
from app.services.extraction import extract_from_pdf

# Point to their source on the quote even after the request was renamed or edited.
_QUOTE_SOURCES = ("title", "totalCost")


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _stored_values(request: ProcurementRequest) -> dict[str, str]:
    """The request's header values by field-source key, normalised for comparison."""
    return {
        "requestorName": _text(request.requestor_name),
        "department": _text(request.department),
        "vendorName": _text(request.vendor_name),
        "vatId": _digits(request.vat_id),
        "quoteNumber": _text(request.quote_number),
        "validUntil": request.valid_until.isoformat() if request.valid_until else "",
    }


def _extracted_values(data: ExtractedVendorData) -> dict[str, str]:
    return {
        "requestorName": _text(data.requestor_name),
        "department": _text(data.department),
        "vendorName": _text(data.vendor_name),
        "vatId": _digits(data.vat_id),
        "quoteNumber": _text(data.quote_number),
        "validUntil": (data.valid_until or "")[:10],
    }


def _missing(request: ProcurementRequest) -> set[str]:
    """Keys of filled fields (plus title and total) without a stored highlight."""
    filled = {k for k, v in _stored_values(request).items() if v}
    return (filled | set(_QUOTE_SOURCES)) - set(request.field_sources or {})


def backfill(db: Session, extract=extract_from_pdf) -> int:
    """Add the missing highlights; returns how many requests gained one."""
    updated = 0
    pairs = db.execute(
        select(ProcurementRequest, RequestDocument).join(
            RequestDocument, RequestDocument.request_id == ProcurementRequest.id
        )
    ).all()
    for request, document in pairs:
        missing = _missing(request)
        if not missing:
            continue
        resp = extract(document.content, db, request.organization_id)
        if not resp.success or resp.data is None:
            continue
        found = resp.data.field_sources
        stored, extracted = _stored_values(request), _extracted_values(resp.data)
        added = {
            key: [box.model_dump() for box in found[key]]
            for key in sorted(missing)
            if found.get(key) and (key in _QUOTE_SOURCES or stored[key] == extracted[key])
        }
        if added:
            request.field_sources = {**(request.field_sources or {}), **added}
            updated += 1
    db.commit()
    return updated


def main() -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        print(f"Added highlights to {backfill(db)} request(s).")


if __name__ == "__main__":
    main()
