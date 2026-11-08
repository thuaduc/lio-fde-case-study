"""Suggest negotiated catalog articles for order lines.

Pipeline per request: shortlist candidates per line (OpenAI embeddings ranked
by cosine, or rapidfuzz without an API key / on an embeddings error), confirm
them with one structured LLM call, then keep the three cheapest confirmed.
"""
import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Literal, NamedTuple

from pydantic import BaseModel
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models import Article, ArticleEmbedding
from app.services import extraction

logger = logging.getLogger(__name__)

SHORTLIST_K = 8
FUZZY_SHORTLIST_CUTOFF = 60
FUZZY_CONFIRM_CUTOFF = 80
MAX_SUGGESTIONS = 3
EMBEDDING_BATCH = 2048  # OpenAI's per-request input limit

Mode = Literal["embedding", "fuzzy"]


@dataclass
class LineQuery:
    description: str
    unit: str | None = None
    unit_price: float | None = None
    vendor_item_number: str | None = None


class Hit(NamedTuple):
    """A shortlisted article. ``score`` is cosine (embedding mode) or fuzzy/100."""

    article: Article
    score: float
    mode: Mode
    exact: bool = False  # vendor item number equals the article number


@dataclass
class Suggestion:
    article: Article
    match_type: str
    saving_per_unit: float | None


# --- embeddings -------------------------------------------------------------


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _upsert_embeddings(db: Session, rows: list[dict]) -> None:
    """INSERT .. ON CONFLICT (text_hash) DO UPDATE: replaces stale-model rows
    and tolerates a concurrent request inserting the same hash."""
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = postgresql.insert
    elif dialect == "sqlite":
        insert = sqlite.insert
    else:
        raise NotImplementedError(dialect)
    stmt = insert(ArticleEmbedding).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["text_hash"],
        set_={"model": stmt.excluded.model, "embedding": stmt.excluded.embedding},
    )
    db.execute(stmt)


def ensure_embeddings(db: Session, texts: list[str]) -> dict[str, list[float]]:
    """Vectors for ``texts`` (keyed by the text as given), embedding only the
    ones missing from the cache in one batched request.

    A cached row from a different embedding model counts as missing.
    """
    model = settings.openai_embedding_model
    by_hash: dict[str, str] = {}  # hash -> first text seen
    for t in texts:
        by_hash.setdefault(text_hash(t), t)

    cached: dict[str, list[float]] = {}
    if by_hash:
        rows = db.scalars(
            select(ArticleEmbedding).where(ArticleEmbedding.text_hash.in_(list(by_hash)))
        )
        cached = {r.text_hash: r.embedding for r in rows if r.model == model}

    missing = [h for h in by_hash if h not in cached]
    if missing:
        client = extraction._get_client()
        for i in range(0, len(missing), EMBEDDING_BATCH):
            chunk = missing[i : i + EMBEDDING_BATCH]
            response = client.embeddings.create(
                model=model, input=[normalize(by_hash[h]) for h in chunk]
            )
            rows_out: list[dict] = []
            for h, item in zip(chunk, response.data, strict=True):
                vector = [float(x) for x in item.embedding]
                cached[h] = vector
                rows_out.append({"text_hash": h, "model": model, "embedding": vector})
            _upsert_embeddings(db, rows_out)
        db.commit()

    return {t: cached[text_hash(t)] for t in texts}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --- shortlist --------------------------------------------------------------


def _fuzzy(a: str, b: str, cutoff: float = 0) -> float:
    return fuzz.token_set_ratio(a, b, processor=default_process, score_cutoff=cutoff)


def shortlist(
    description: str,
    articles: list[Article],
    k: int = SHORTLIST_K,
    *,
    vectors: dict[str, list[float]] | None = None,
    vendor_item_number: str | None = None,
) -> list[Hit]:
    """Top-k candidates for a line description.

    With ``vectors`` (text -> embedding, covering the description and every
    article description) candidates are ranked by cosine similarity; without
    them rapidfuzz ``token_set_ratio`` (cutoff 60) is used. An article whose
    number equals ``vendor_item_number`` (case-insensitive) is always included.
    """
    mode: Mode = "embedding" if vectors is not None else "fuzzy"
    scored: list[tuple[Article, float]] = []
    if mode == "embedding":
        query = vectors.get(description) if description.strip() else None
        if query is not None:
            scored = [
                (a, _cosine(query, vectors[a.description]))
                for a in articles
                if a.description in vectors
            ]
    else:
        for a in articles:
            score = _fuzzy(description, a.description, FUZZY_SHORTLIST_CUTOFF)
            if score:
                scored.append((a, score / 100))
    scored.sort(key=lambda p: p[1], reverse=True)

    wanted = (vendor_item_number or "").strip().lower()
    exact_ids: set[uuid.UUID] = set()
    if wanted:
        exact_ids = {a.id for a in articles if a.article_number.lower() == wanted}

    hits = [Hit(a, s, mode, a.id in exact_ids) for a, s in scored[:k]]
    present = {h.article.id for h in hits}
    for a in articles:
        if a.id in exact_ids and a.id not in present:
            score = 0.0
            if mode == "embedding" and vectors.get(description) is not None and a.description in vectors:
                score = _cosine(vectors[description], vectors[a.description])
            elif mode == "fuzzy":
                score = _fuzzy(description, a.description) / 100
            hits.append(Hit(a, score, mode, True))
    return hits


# --- LLM confirmation -------------------------------------------------------


class LlmMatch(BaseModel):
    article_id: str | None
    match_type: Literal["same", "similar"] | None


class LlmLineMatches(BaseModel):
    line_index: int | None
    matches: list[LlmMatch] | None


class LlmMatches(BaseModel):
    lines: list[LlmLineMatches] | None


CONFIRM_SYSTEM_PROMPT = (
    "You match purchase order lines to articles from the buyer's negotiated "
    "catalog. The user message is JSON: for each order line, its description "
    "and unit, an optional vendor item number, and candidate catalog articles. "
    "For every line return the candidates that qualify, each with a match_type:\n"
    '- "same": the same product (the same item, possibly a different brand or '
    "naming, and equivalent specification).\n"
    '- "similar": an acceptable substitute for the same need.\n'
    "Anything else must be excluded: a different kind of product, a different "
    "specification that would not serve the same need, or an unrelated item. "
    "Most lines have zero or one real match; a line with no catalog equivalent "
    "(for example a grinding machine when the candidates are laptops) gets an "
    "empty list. Only use article_id values listed under that line's own "
    "candidates, copied exactly. Return an entry for every line index."
)


def _fuzzy_confirm(query: LineQuery, hits: list[Hit]) -> list[tuple[uuid.UUID, str]]:
    out = []
    for h in hits:
        if h.exact:
            out.append((h.article.id, "same"))
        elif _fuzzy(query.description, h.article.description, FUZZY_CONFIRM_CUTOFF):
            out.append((h.article.id, "similar"))
    return out


def confirm_matches(
    lines: list[LineQuery], shortlists: list[list[Hit]]
) -> list[list[tuple[uuid.UUID, str]]]:
    """Confirmed (article_id, match_type) per line, using one LLM call for all
    lines. Without an API key, or if the call fails, shortlist hits with a
    fuzzy score >= 80 are kept as "similar" and exact item-number hits as "same".
    """
    if not any(shortlists):
        return [[] for _ in lines]

    def fallback() -> list[list[tuple[uuid.UUID, str]]]:
        return [_fuzzy_confirm(q, hits) for q, hits in zip(lines, shortlists)]

    if not settings.openai_api_key:
        return fallback()

    payload = {
        "lines": [
            {
                "index": i,
                "description": q.description,
                "unit": q.unit,
                "vendor_item_number": q.vendor_item_number,
                "candidates": [
                    {
                        "article_id": str(h.article.id),
                        "article_number": h.article.article_number,
                        "description": h.article.description,
                        "unit": h.article.unit,
                    }
                    for h in hits
                ],
            }
            for i, (q, hits) in enumerate(zip(lines, shortlists))
        ]
    }
    kwargs: dict = dict(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": CONFIRM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format=LlmMatches,
    )
    if settings.openai_temperature is not None:
        kwargs["temperature"] = settings.openai_temperature

    try:
        completion = extraction._get_client().chat.completions.parse(**kwargs)
        message = completion.choices[0].message
        parsed = message.parsed
        if message.refusal or parsed is None:
            raise ValueError(f"LLM returned no result (refusal={message.refusal!r})")
    except Exception:  # noqa: BLE001 — any LLM failure degrades to fuzzy matching
        logger.exception("Catalog match LLM call failed; using fuzzy fallback")
        return fallback()

    allowed = [{str(h.article.id) for h in hits} for hits in shortlists]
    result: list[list[tuple[uuid.UUID, str]]] = [[] for _ in lines]
    for entry in parsed.lines or []:
        i = entry.line_index
        if i is None or not 0 <= i < len(lines):
            continue
        seen = {a for a, _ in result[i]}
        for m in entry.matches or []:
            if m.article_id not in allowed[i] or m.match_type is None:
                continue
            aid = uuid.UUID(m.article_id)
            if aid not in seen:
                seen.add(aid)
                result[i].append((aid, m.match_type))
    return result


# --- orchestration ----------------------------------------------------------


def suggest(
    db: Session, organization_id: uuid.UUID, lines: list[LineQuery]
) -> list[list[Suggestion]]:
    """Up to three suggestions per line from the organization's catalog,
    cheapest article first."""
    if not lines:
        return []
    articles = list(
        db.scalars(
            select(Article)
            .where(Article.organization_id == organization_id)
            .options(joinedload(Article.supplier))
        )
    )

    vectors: dict[str, list[float]] | None = None
    if settings.openai_api_key and articles:
        texts = list({a.description for a in articles})
        texts += [q.description for q in lines if q.description.strip()]
        try:
            vectors = ensure_embeddings(db, list(dict.fromkeys(texts)))
        except Exception:  # noqa: BLE001 — degrade to fuzzy matching
            logger.exception("Embeddings failed; using fuzzy shortlist")
            db.rollback()
            vectors = None

    shortlists = [
        shortlist(
            q.description,
            articles,
            vectors=vectors,
            vendor_item_number=q.vendor_item_number,
        )
        for q in lines
    ]
    confirmed = confirm_matches(lines, shortlists)

    out: list[list[Suggestion]] = []
    for q, hits, matches in zip(lines, shortlists, confirmed):
        by_id = {h.article.id: h.article for h in hits}
        found = [(by_id[aid], mt) for aid, mt in matches if aid in by_id]
        found.sort(key=lambda p: (float(p[0].unit_price), p[0].article_number))
        out.append(
            [
                Suggestion(
                    a,
                    mt,
                    None
                    if q.unit_price is None
                    else round(q.unit_price - float(a.unit_price), 2),
                )
                for a, mt in found[:MAX_SUGGESTIONS]
            ]
        )
    return out
