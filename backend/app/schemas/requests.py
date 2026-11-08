import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.extraction import LineKind
from app.services.pdf_layout import Box

RequestStatus = Literal["open", "in_progress", "closed"]
ApprovalStatus = Literal["pending", "approved", "rejected"]


# ---------------------------------------------------------------------------
# Inputs (camelCase — matches what the frontend forms already send)
# ---------------------------------------------------------------------------

class OrderLineIn(BaseModel):
    """An order line. Accepts an ``ExtractedLine``'s JSON unchanged.

    Every field beyond the original five is optional so older clients keep
    working. ``unitPrice``/``totalPrice`` are recomputed by the server on save.
    """

    model_config = ConfigDict(populate_by_name=True)

    position_description: str = Field(alias="positionDescription")
    unit_price: float = Field(alias="unitPrice")
    amount: float
    unit: str
    total_price: float = Field(alias="totalPrice")
    key: str | None = None
    parent_key: str | None = Field(default=None, alias="parentKey")
    kind: LineKind = "item"
    position: str | None = None
    vendor_item_number: str | None = Field(default=None, alias="vendorItemNumber")
    list_unit_price: float | None = Field(default=None, alias="listUnitPrice")
    adjustment_percent: float | None = Field(default=None, alias="adjustmentPercent")
    sources: list[Box] = Field(default_factory=list)
    source_text: str | None = Field(default=None, alias="sourceText")
    article_id: uuid.UUID | None = Field(default=None, alias="articleId")


class RequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    requestor_name: str = Field(alias="requestorName")
    title: str = Field(alias="titleShortDescription")
    vendor_name: str = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    # Ignored: the server computes the total from the lines (kept so older
    # clients that still send it validate).
    total_cost: float | None = Field(default=None, alias="totalCost")
    department: str | None = None
    quote_number: str | None = Field(default=None, alias="quoteNumber")
    valid_until: date | None = Field(default=None, alias="validUntil")
    currency: str | None = None
    tax_total: float | None = Field(default=None, alias="taxTotal")
    extraction_warnings: list[str] | None = Field(
        default=None, alias="extractionWarnings"
    )
    field_sources: dict[str, list[Box]] | None = Field(
        default=None, alias="fieldSources"
    )
    order_lines: list[OrderLineIn] = Field(alias="orderLines", min_length=1)


class RequestUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    requestor_name: str = Field(alias="requestorName")
    title: str
    vendor_name: str = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    total_cost: float | None = Field(default=None, alias="totalCost")  # ignored
    department: str | None = None
    quote_number: str | None = Field(default=None, alias="quoteNumber")
    valid_until: date | None = Field(default=None, alias="validUntil")
    currency: str | None = None
    tax_total: float | None = Field(default=None, alias="taxTotal")
    extraction_warnings: list[str] | None = Field(
        default=None, alias="extractionWarnings"
    )
    field_sources: dict[str, list[Box]] | None = Field(
        default=None, alias="fieldSources"
    )
    order_lines: list[OrderLineIn] = Field(alias="orderLines")


class StatusUpdate(BaseModel):
    status: RequestStatus


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]


# ---------------------------------------------------------------------------
# Outputs (snake_case — matches the DB row shapes the frontend renders)
# ---------------------------------------------------------------------------

class CommodityGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    name: str


class OrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    position_description: str
    unit_price: float
    amount: float
    unit: str
    total_price: float
    line_order: int
    created_at: datetime
    # New fields are camelCase on the wire (serialization aliases only, so the
    # ORM attribute names still validate).
    key: str = Field(validation_alias="line_key")
    parent_key: str | None = Field(default=None, serialization_alias="parentKey")
    kind: LineKind = Field(default="item", validation_alias="line_type")
    position: str | None = None
    vendor_item_number: str | None = Field(
        default=None, serialization_alias="vendorItemNumber"
    )
    list_unit_price: float | None = Field(
        default=None, serialization_alias="listUnitPrice"
    )
    adjustment_percent: float | None = Field(
        default=None, serialization_alias="adjustmentPercent"
    )
    sources: list[Box] = Field(default_factory=list)
    source_text: str | None = Field(default=None, serialization_alias="sourceText")
    article_id: uuid.UUID | None = Field(default=None, serialization_alias="articleId")

    @field_validator("sources", mode="before")
    @classmethod
    def _null_sources(cls, v):
        return v or []


class RequestActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    summary: str
    detail: dict | None
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    created_by: uuid.UUID | None
    requestor_name: str
    title: str
    vendor_name: str
    vat_id: str | None
    commodity_group_id: int | None
    department: str | None
    status: RequestStatus
    approval_status: ApprovalStatus
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    total_cost: float | None
    created_at: datetime
    updated_at: datetime
    quote_number: str | None = Field(default=None, serialization_alias="quoteNumber")
    valid_until: date | None = Field(default=None, serialization_alias="validUntil")
    currency: str | None = None
    net_total: float | None = Field(default=None, serialization_alias="netTotal")
    tax_total: float | None = Field(default=None, serialization_alias="taxTotal")
    extraction_warnings: list[str] | None = Field(
        default=None, serialization_alias="extractionWarnings"
    )
    field_sources: dict[str, list[Box]] | None = Field(
        default=None, serialization_alias="fieldSources"
    )
    order_lines: list[OrderLineOut]
    # Serialized as ``commodity_groups`` (the join key the frontend renders) but
    # read from the ORM's ``commodity_group`` relationship.
    commodity_groups: CommodityGroupOut | None = Field(
        default=None, validation_alias="commodity_group"
    )
