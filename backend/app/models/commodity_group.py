import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Sequence, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

# New rows (org copies, admin-created groups) draw ids from this sequence on
# Postgres, well above the fixed template ids. SQLite has no sequences: there
# SQLAlchemy ignores it and the INTEGER PRIMARY KEY autoincrements instead.
COMMODITY_GROUP_ID_SEQ = Sequence("commodity_groups_id_seq", start=10001)


class CommodityGroup(Base, CreatedAtMixin):
    """A commodity group.

    ``organization_id IS NULL`` marks a global template (fixed ids 1..N from
    ``app.data.commodity_groups``). Each organization works with its own copies
    of the base templates plus any groups its admins add; ``description`` holds
    the org's booking rules, which the extraction classifier treats as binding.
    """

    __tablename__ = "commodity_groups"

    # Templates are inserted with explicit ids; everything else uses the sequence.
    id: Mapped[int] = mapped_column(
        Integer, COMMODITY_GROUP_ID_SEQ, primary_key=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
