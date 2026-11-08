import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app.core.deps import get_current_membership
from app.db.session import get_db
from app.models import OrganizationMember
from app.models.request_draft import RequestDraft
from app.routers.requests import read_pdf_upload
from app.schemas.drafts import DraftIn, DraftOut, DraftSummary

router = APIRouter(prefix="/drafts", tags=["drafts"])

MAX_DRAFT_DATA_BYTES = 2 * 1024 * 1024


def _check_size(data: dict) -> None:
    size = len(json.dumps(data, separators=(",", ":")).encode())
    if size > MAX_DRAFT_DATA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Draft exceeds the 2MB limit",
        )


def get_own_draft(
    db: Session, draft_id: uuid.UUID, membership: OrganizationMember, *, light: bool = True
) -> RequestDraft | None:
    """The caller's own draft in their org, else None. ``light`` skips the PDF bytes."""
    stmt = select(RequestDraft).where(
        RequestDraft.id == draft_id,
        RequestDraft.user_id == membership.user_id,
        RequestDraft.organization_id == membership.organization_id,
    )
    if light:
        stmt = stmt.options(defer(RequestDraft.document))
    return db.scalar(stmt)


def _get_or_404(db, draft_id, membership, *, light: bool = True) -> RequestDraft:
    draft = get_own_draft(db, draft_id, membership, light=light)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")
    return draft


def _title_str(value):
    return value if isinstance(value, str) else None


def _summary_fields(draft: RequestDraft, has_document: bool) -> dict:
    data = draft.data or {}
    cost = data.get("totalCost")
    return {
        "id": str(draft.id),
        "title": _title_str(data.get("title")),
        "vendorName": _title_str(data.get("vendorName")),
        "totalCost": cost
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else None,
        "fileName": draft.document_filename,
        "hasDocument": has_document,
        "updatedAt": draft.updated_at,
    }


def _has_document(db: Session, draft: RequestDraft) -> bool:
    return (
        db.scalar(
            select(RequestDraft.document.is_not(None)).where(RequestDraft.id == draft.id)
        )
        is True
    )


@router.get("", response_model=list[DraftSummary])
def list_drafts(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[DraftSummary]:
    rows = db.execute(
        select(RequestDraft, RequestDraft.document.is_not(None))
        .options(defer(RequestDraft.document))
        .where(
            RequestDraft.user_id == membership.user_id,
            RequestDraft.organization_id == membership.organization_id,
        )
        .order_by(RequestDraft.updated_at.desc(), RequestDraft.created_at.desc())
    ).all()
    return [DraftSummary.model_validate(_summary_fields(d, bool(h))) for d, h in rows]


@router.post("", response_model=DraftOut, status_code=status.HTTP_201_CREATED)
def create_draft(
    payload: DraftIn,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> DraftOut:
    _check_size(payload.data)
    draft = RequestDraft(
        organization_id=membership.organization_id,
        user_id=membership.user_id,
        data=payload.data,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return DraftOut.model_validate(
        {**_summary_fields(draft, False), "data": draft.data}
    )


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(
    draft_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> DraftOut:
    draft = _get_or_404(db, draft_id, membership)
    return DraftOut.model_validate(
        {**_summary_fields(draft, _has_document(db, draft)), "data": draft.data}
    )


@router.put("/{draft_id}", response_model=DraftSummary)
def update_draft(
    draft_id: uuid.UUID,
    payload: DraftIn,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> DraftSummary:
    draft = _get_or_404(db, draft_id, membership)
    _check_size(payload.data)
    draft.data = payload.data
    draft.updated_at = func.now()  # bump even when the JSON is unchanged
    db.commit()
    db.refresh(draft)
    return DraftSummary.model_validate(_summary_fields(draft, _has_document(db, draft)))


@router.put("/{draft_id}/document", status_code=status.HTTP_204_NO_CONTENT)
async def upload_draft_document(
    draft_id: uuid.UUID,
    file: UploadFile,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    draft = _get_or_404(db, draft_id, membership)
    content = await read_pdf_upload(file)
    draft.document = content
    draft.document_filename = file.filename or "document.pdf"
    db.commit()


@router.get("/{draft_id}/document")
def get_draft_document(
    draft_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> Response:
    draft = _get_or_404(db, draft_id, membership, light=False)
    if draft.document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No document attached"
        )
    return Response(
        content=draft.document,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{draft.document_filename}"'
        },
    )


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft(
    draft_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    db.delete(_get_or_404(db, draft_id, membership))
    db.commit()
