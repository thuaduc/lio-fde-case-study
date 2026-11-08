from sqlalchemy import JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ArticleEmbedding(Base):
    """Embedding cache keyed by the SHA-256 of the normalised text.

    Shared by every org and supplier; also holds order-line descriptions.
    """

    __tablename__ = "article_embeddings"

    text_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
