from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.pdf_layout import Box

LineKind = Literal[
    "item", "surcharge", "discount", "shipping", "fee", "alternative", "optional"
]


class PdfParseResponse(BaseModel):
    text: str


class ExtractionRequest(BaseModel):
    text: str


# camelCase on the wire — matches the AI's JSON output format and what the
# frontend's extraction flow already consumes.
class OrderLineData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    position_description: str = Field(default="", alias="positionDescription")
    unit_price: float = Field(default=0, alias="unitPrice")
    amount: float = 0
    unit: str = ""
    total_price: float = Field(default=0, alias="totalPrice")


class ExtractedLine(OrderLineData):
    """Order line enriched with structure (kind/parent) and source boxes."""

    key: str = ""
    parent_key: str | None = Field(default=None, alias="parentKey")
    kind: LineKind = "item"
    position: str | None = None
    vendor_item_number: str | None = Field(default=None, alias="vendorItemNumber")
    list_unit_price: float | None = Field(default=None, alias="listUnitPrice")
    adjustment_percent: float | None = Field(default=None, alias="adjustmentPercent")
    # Filled by Task 3 (resolve_sources).
    sources: list[Box] = Field(default_factory=list)
    source_text: str | None = Field(default=None, alias="sourceText")


class ExtractedVendorData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = ""
    vendor_name: str = Field(default="", alias="vendorName")
    vat_id: str = Field(default="", alias="vatId")
    department: str = ""
    order_lines: list[ExtractedLine] = Field(default_factory=list, alias="orderLines")
    total_cost: float = Field(default=0, alias="totalCost")
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")
    commodity_group_name: str | None = Field(default=None, alias="commodityGroupName")
    quote_number: str | None = Field(default=None, alias="quoteNumber")
    valid_until: str | None = Field(default=None, alias="validUntil")
    currency: str | None = None
    net_total: float | None = Field(default=None, alias="netTotal")
    tax_total: float | None = Field(default=None, alias="taxTotal")
    # Header-field highlights keyed vendorName/vatId/department/quoteNumber/
    # validUntil; a key is present only when it has boxes.
    field_sources: dict[str, list[Box]] = Field(
        default_factory=dict, alias="fieldSources"
    )


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    data: ExtractedVendorData | None = None
    error: str | None = None
    missing_fields: list[str] | None = Field(default=None, alias="missingFields")
    warnings: list[str] | None = None


# --- Internal LLM output types (strict structured output: every field required,
# nullable where the quote may not state it). Never exposed on the wire. ---


class LlmLine(BaseModel):
    position: str | None
    vendor_item_number: str | None
    description: str | None
    kind: LineKind | Literal["heading"]
    # Position of the item this line replaces or applies to; null = whole quote.
    parent_position: str | None
    quantity: float | None
    unit: str | None
    list_unit_price: float | None
    adjustment_percent: float | None
    unit_price: float | None
    total_price: float | None
    source_rows: list[str]


class LlmFieldSources(BaseModel):
    """Row ids where each header value is printed (empty list if not on the quote)."""

    vendor_name: list[str]
    vendor_tax_id: list[str]
    department: list[str]
    quote_number: list[str]
    valid_until: list[str]


class LlmQuote(BaseModel):
    title: str | None
    vendor_name: str | None
    vendor_tax_id: str | None
    department: str | None
    quote_number: str | None
    valid_until: str | None  # ISO date
    currency: str | None
    lines: list[LlmLine]
    net_total: float | None
    tax_total: float | None
    grand_total: float | None
    unpriced_charges: list[str]
    commodity_group_id: int | None
    field_source_rows: LlmFieldSources
