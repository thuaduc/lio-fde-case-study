from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.session import get_db
from app.schemas.extraction import ExtractionRequest, ExtractionResponse
from app.routers.pdf import MAX_PDF_BYTES
from app.services.extraction import extract_from_pdf, extract_vendor_data
from app.services.pdf_layout import is_pdf

router = APIRouter(prefix="/extraction", tags=["extraction"])


@router.post("", response_model=ExtractionResponse, response_model_by_alias=True,
             dependencies=[Depends(get_current_user)])
def extract(payload: ExtractionRequest, db: Session = Depends(get_db)) -> ExtractionResponse:
    """Extract structured vendor data from PDF text via OpenAI.

    Returns ``{success, data?, error?, missingFields?}`` — the same envelope the
    original server action produced (errors are part of the payload, not HTTP
    errors, so the UI can show partial-extraction warnings).
    """
    return extract_vendor_data(payload.text, db)


@router.post("/pdf", response_model=ExtractionResponse, response_model_by_alias=True,
             dependencies=[Depends(get_current_user)])
async def extract_pdf(file: UploadFile, db: Session = Depends(get_db)) -> ExtractionResponse:
    """Extract structured quote lines (with kinds and links) straight from a PDF."""
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF exceeds the 10MB limit",
        )
    if not is_pdf(pdf_bytes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Please upload a PDF file"
        )
    # Blocking work (pdfplumber + sync OpenAI call): keep it off the event loop.
    # The Session is created per request and only used inside this one thread.
    return await run_in_threadpool(extract_from_pdf, pdf_bytes, db)
