import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_membership
from app.db.session import get_db
from app.models import Article, OrganizationMember
from app.schemas.articles import (
    ArticleOut,
    ArticlePage,
    ArticleSuggestionOut,
    LineSuggestionsOut,
    SuggestionsRequest,
)
from app.services import catalog_match

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("", response_model=ArticlePage)
def list_articles(
    search: str | None = Query(
        default=None, description="Filter by description or article number (case-insensitive)"
    ),
    supplier_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> ArticlePage:
    filters = [Article.organization_id == membership.organization_id]
    if search:
        term = f"%{search}%"
        filters.append(
            or_(Article.description.ilike(term), Article.article_number.ilike(term))
        )
    if supplier_id:
        filters.append(Article.supplier_id == supplier_id)

    total = db.scalar(
        select(func.count()).select_from(Article).where(*filters)
    ) or 0

    stmt = (
        select(Article)
        .where(*filters)
        .options(joinedload(Article.supplier))
        .order_by(Article.article_number)
        .offset(offset)
        .limit(limit)
    )
    items = [
        ArticleOut(
            id=a.id,
            article_number=a.article_number,
            supplier_id=a.supplier_id,
            supplier_name=a.supplier.name if a.supplier else None,
            description=a.description,
            unit_price=a.unit_price,
            currency=a.currency,
            unit=a.unit,
            quantity=a.quantity,
            created_at=a.created_at,
        )
        for a in db.scalars(stmt)
    ]
    return ArticlePage(items=items, total=total, limit=limit, offset=offset)


@router.get("/count")
def count_articles(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    total = (
        db.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.organization_id == membership.organization_id)
        )
        or 0
    )
    return {"count": total}


@router.post("/suggestions", response_model=list[LineSuggestionsOut])
def suggest_articles(
    payload: SuggestionsRequest,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[LineSuggestionsOut]:
    """Negotiated catalog articles (at most 3 per line, cheapest first)."""
    queries = [
        catalog_match.LineQuery(
            description=ln.description,
            unit=ln.unit,
            unit_price=ln.unit_price,
            vendor_item_number=ln.vendor_item_number,
        )
        for ln in payload.lines
    ]
    results = catalog_match.suggest(db, membership.organization_id, queries)
    return [
        LineSuggestionsOut(
            line_index=i,
            suggestions=[
                ArticleSuggestionOut(
                    article_id=s.article.id,
                    article_number=s.article.article_number,
                    supplier_name=s.article.supplier.name if s.article.supplier else None,
                    description=s.article.description,
                    unit_price=s.article.unit_price,
                    unit=s.article.unit,
                    quantity=s.article.quantity,
                    match_type=s.match_type,
                    saving_per_unit=s.saving_per_unit,
                )
                for s in suggestions
            ],
        )
        for i, suggestions in enumerate(results)
    ]


@router.get("/{article_id}", response_model=ArticleOut)
def get_article(
    article_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> ArticleOut:
    article = db.scalar(select(Article).where(
        Article.id == article_id,
        Article.organization_id == membership.organization_id,
    ).options(joinedload(Article.supplier)))
    if article is None:
        raise HTTPException(status_code=404, detail="Catalog article not found")
    return ArticleOut.model_validate(article).model_copy(update={
        "supplier_name": article.supplier.name if article.supplier else None,
    })
