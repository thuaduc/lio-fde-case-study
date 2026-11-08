import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    article_number: str
    supplier_id: uuid.UUID
    supplier_name: str | None = None
    description: str
    unit_price: Decimal
    currency: str
    unit: str
    quantity: Decimal
    created_at: datetime


class ArticlePage(BaseModel):
    """A page of articles plus the totals the UI needs to render pagination."""

    items: list[ArticleOut]
    total: int
    limit: int
    offset: int


class SuggestionLineIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    description: str
    unit: str | None = None
    unit_price: float | None = Field(default=None, alias="unitPrice")
    vendor_item_number: str | None = Field(default=None, alias="vendorItemNumber")


class SuggestionsRequest(BaseModel):
    lines: list[SuggestionLineIn]


class ArticleSuggestionOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    article_id: uuid.UUID = Field(alias="articleId")
    article_number: str = Field(alias="articleNumber")
    supplier_name: str | None = Field(default=None, alias="supplierName")
    description: str
    unit_price: float = Field(alias="unitPrice")
    unit: str
    quantity: float
    match_type: Literal["same", "similar"] = Field(alias="matchType")
    saving_per_unit: float | None = Field(alias="savingPerUnit")


class LineSuggestionsOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    line_index: int = Field(alias="lineIndex")
    suggestions: list[ArticleSuggestionOut]
