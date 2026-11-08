from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DraftIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Opaque frontend form state; must be a JSON object.
    data: dict[str, Any]


class DraftSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str | None = None
    vendor_name: str | None = Field(default=None, alias="vendorName")
    total_cost: int | float | None = Field(default=None, alias="totalCost")
    file_name: str | None = Field(default=None, alias="fileName")
    has_document: bool = Field(alias="hasDocument")
    updated_at: datetime = Field(alias="updatedAt")


class DraftOut(DraftSummary):
    data: dict[str, Any]
