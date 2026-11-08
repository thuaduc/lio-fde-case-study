import uuid

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class RequestDraft(Base, TimestampMixin):
    """A private, server-side draft of the new-request form (Gmail-style)."""

    __tablename__ = "request_drafts"
    __table_args__ = (
        Index("ix_request_drafts_user_org_updated", "user_id", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Opaque frontend form state.
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    document: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    document_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
