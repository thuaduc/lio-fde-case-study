import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.schemas.extraction import LineKind, WarningList
from app.services.pdf_layout import Box

RequestStatus = Literal["open", "in_progress", "closed"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ---------------------------------------------------------------------------
# Inputs (camelCase — matches what the frontend forms already send)
# ---------------------------------------------------------------------------

class OriginalQuote(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    positionDescription: str
    unitPrice: float
    amount: float
    unit: str
    vendorItemNumber: str | None = None
    listUnitPrice: float | None = None
    adjustmentPercent: float | None = None


class OrderLineIn(BaseModel):
    """An order line. Accepts an ``ExtractedLine``'s JSON unchanged.

    Every field beyond the original five is optional so older clients keep
    working. ``unitPrice``/``totalPrice`` are recomputed by the server on save.
    """

    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    position_description: str = Field(alias="positionDescription")
    unit_price: float = Field(alias="unitPrice", gt=-1e10, lt=1e10)
    amount: float = Field(gt=-1e8, lt=1e8)
    unit: str
    total_price: float = Field(alias="totalPrice")
    key: str | None = None
    parent_key: str | None = Field(default=None, alias="parentKey")
    kind: LineKind = "item"
    position: str | None = None
    vendor_item_number: str | None = Field(default=None, alias="vendorItemNumber")
    list_unit_price: float | None = Field(default=None, alias="listUnitPrice", gt=-1e10, lt=1e10)
    adjustment_percent: float | None = Field(default=None, alias="adjustmentPercent", gt=-1e4, lt=1e4)
    sources: list[Box] = Field(default_factory=list)
    source_text: str | None = Field(default=None, alias="sourceText")
    original_quote: OriginalQuote | None = Field(default=None, alias="originalQuote")
    article_id: uuid.UUID | None = Field(default=None, alias="articleId")
    # The org's group for this line. Adjustments with a parent and no group
    # take the parent's group on save.
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")


class RequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    requestor_name: NonBlank = Field(alias="requestorName")
    title: NonBlank = Field(alias="titleShortDescription")
    vendor_name: NonBlank = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    # Used only when no line has a group; otherwise the server sets the
    # primary group (largest active spend) from the lines.
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")
    # Ignored: the server computes the total from the lines (kept so older
    # clients that still send it validate).
    total_cost: float | None = Field(default=None, alias="totalCost")
    department: str | None = None
    quote_number: str | None = Field(default=None, alias="quoteNumber")
    valid_until: date | None = Field(default=None, alias="validUntil")
    currency: str | None = None
    tax_total: float | None = Field(default=None, alias="taxTotal", gt=-1e10, lt=1e10)
    extraction_warnings: WarningList | None = Field(
        default=None, alias="extractionWarnings"
    )
    field_sources: dict[str, list[Box]] | None = Field(
        default=None, alias="fieldSources"
    )
    order_lines: list[OrderLineIn] = Field(alias="orderLines", min_length=1)
    draft_id: uuid.UUID | None = Field(default=None, alias="draftId")


class RequestUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, allow_inf_nan=False)

    requestor_name: NonBlank = Field(alias="requestorName")
    title: NonBlank
    vendor_name: NonBlank = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    # Same rule as RequestCreate: only used when no line has a group.
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")
    total_cost: float | None = Field(default=None, alias="totalCost")  # ignored
    department: str | None = None
    quote_number: str | None = Field(default=None, alias="quoteNumber")
    valid_until: date | None = Field(default=None, alias="validUntil")
    currency: str | None = None
    tax_total: float | None = Field(default=None, alias="taxTotal", gt=-1e10, lt=1e10)
    extraction_warnings: WarningList | None = Field(
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
    description: str | None = None
    archived: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, data):
        # ORM rows carry archived_at; the API exposes a plain flag.
        if hasattr(data, "archived_at"):
            return {
                "id": data.id,
                "category": data.category,
                "name": data.name,
                "description": data.description,
                "archived": data.archived_at is not None,
            }
        return data


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
    original_quote: OriginalQuote | None = Field(default=None, serialization_alias="originalQuote")
    article_id: uuid.UUID | None = Field(default=None, serialization_alias="articleId")
    commodity_group_id: int | None = Field(
        default=None, serialization_alias="commodityGroupId"
    )

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
    extraction_warnings: WarningList | None = Field(
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def auto_approved(self) -> bool:
        """Approved by the organization's approval threshold, not by a person."""
        return self.approval_status == "approved" and self.approved_by is None
