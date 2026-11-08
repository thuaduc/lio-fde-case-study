"""Pre-build the embedding cache for every organization's article descriptions.

Descriptions are shared across orgs and suppliers, so each distinct text is
embedded once. Without this, missing embeddings are built lazily on the first
suggestions request.

Usage (from backend/, needs OPENAI_API_KEY):
    python scripts/embed_articles.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models import Article  # noqa: E402
from app.services.catalog_match import ensure_embeddings  # noqa: E402


def main() -> None:
    with SessionLocal() as db:
        texts = list(db.scalars(select(Article.description).distinct()))
        ensure_embeddings(db, texts)
    print(f"Embeddings ready for {len(texts)} distinct article descriptions")


if __name__ == "__main__":
    main()
