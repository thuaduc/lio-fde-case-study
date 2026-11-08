import json
import logging
import re
from collections import Counter

import openai
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CommodityGroup
from app.schemas.extraction import (
    ExtractedLine,
    ExtractedVendorData,
    ExtractionResponse,
    LlmLine,
    LlmQuote,
)
from app.services.line_links import active_keys
from app.services.pdf_layout import (
    Box,
    PdfLayout,
    extract_layout,
    layout_to_prompt_text,
    row_segments,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a data extraction and classification specialist. Always respond with "
    "valid JSON only, no markdown or code blocks. Extract all information accurately "
    "from US business documents and classify into the appropriate commodity "
    "group based on the full context of the offer."
)


def _build_prompt(pdf_text: str, commodity_groups_list: str) -> str:
    return f"""You are a data extraction and classification specialist. Extract procurement/vendor offer information from the following US document text AND classify it into the appropriate commodity group.

Document Text:
{pdf_text}

Please extract the following information:
1. Title/Short Description (A brief summary of what this procurement is for - e.g., "Office Equipment Purchase", "Software Licenses", etc.)
2. Vendor Name (Vendor/Supplier/Seller/Company name)
3. Tax ID (Employer Identification Number/EIN/Federal Tax ID/TIN/VAT ID)
4. Department/Customer (Department/Customer/Bill To/Customer name)
5. Order Lines/Items (All line items with):
   - Position Description (Description/Product/Item/Article)
   - Unit Price (Unit Price/Price/Rate/Price per unit)
   - Amount (Qty/Quantity/Count)
   - Unit (Unit/UoM - e.g., "licenses", "pieces", "ea.", "items")
   - Total Price (Total/Ext. Price/Amount)
6. Total Cost (Grand Total/Total Cost/Total Offer Cost)
7. Commodity Group Classification - Select the SINGLE most appropriate commodity group from the list below

Available Commodity Groups (ID | Category | Name):
{commodity_groups_list}

IMPORTANT INSTRUCTIONS:
- For the title, create a SHORT (2-5 words) description based on the main items being purchased
- Extract ALL line items, even if there are many
- For unit prices and totals, extract only the numeric value (remove currency symbols)
- For the unit field, write out abbreviations in plain English (e.g., "ea." -> "pieces", "lic." -> "licenses")
- Be careful with US number formatting (e.g., "1,438.00" = 1438.00)
- For classification, consider the PRIMARY nature and purpose of the items:
  * Branded office decor/furniture → Office Equipment (15)
  * Traditional promotional items (brochures, giveaways, banners) → Promotional Materials (43)
  * Software/IT products → Software (31) or IT Services (30)
  * If genuinely uncertain, set commodityGroupId to null

Return ONLY valid JSON in this exact format (no markdown, no code blocks):
{{
  "title": "Short description of procurement",
  "vendorName": "Company Name",
  "vatId": "12-3456789",
  "department": "Department Name",
  "orderLines": [
    {{
      "positionDescription": "Item description",
      "unitPrice": 150.00,
      "amount": 10,
      "unit": "licenses",
      "totalPrice": 1500.00
    }}
  ],
  "totalCost": 1500.00,
  "commodityGroupId": 15,
  "commodityGroupName": "Office Equipment"
}}"""


def _strip_markdown_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()
    return cleaned


def extract_vendor_data(pdf_text: str, db: Session) -> ExtractionResponse:
    """Extract vendor data and classify commodity group in one AI call.

    Port of ``src/actions/extraction.ts`` — same prompt, model and validation.
    """
    if not pdf_text or not pdf_text.strip():
        return ExtractionResponse(success=False, error="No text provided for extraction")

    if not settings.openai_api_key:
        return ExtractionResponse(
            success=False,
            error="OPENAI_API_KEY is not configured on the server",
        )

    commodity_groups = list(
        db.scalars(select(CommodityGroup).order_by(CommodityGroup.id))
    )
    if not commodity_groups:
        return ExtractionResponse(
            success=False,
            error="Failed to load commodity groups for classification",
        )

    # Give the model the full commodity catalog so it has complete context to
    # pick the most accurate match in a single pass.
    commodity_groups_list = "\n".join(
        f"{g.id} | {g.category} | {g.name}" for g in commodity_groups
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(pdf_text, commodity_groups_list)},
            ],
            max_completion_tokens=4000,
            temperature=0.3,
        )

        content = response.choices[0].message.content
        if not content:
            return ExtractionResponse(success=False, error="Empty response from AI")

        extracted = ExtractedVendorData.model_validate(
            json.loads(_strip_markdown_fences(content))
        )
    except Exception as exc:  # noqa: BLE001 — mirror the original catch-all
        logger.exception("Error extracting vendor data")
        return ExtractionResponse(success=False, error=str(exc))

    # Check which fields are missing or empty (same rules as the original).
    missing_fields: list[str] = []
    if not extracted.title.strip():
        missing_fields.append("Title/Short Description")
    if not extracted.vendor_name.strip():
        missing_fields.append("Vendor Name")
    if not extracted.vat_id.strip():
        missing_fields.append("Tax ID")
    if not extracted.department.strip():
        missing_fields.append("Department")
    if not extracted.order_lines:
        missing_fields.append("Order Lines")
    if not extracted.total_cost:
        missing_fields.append("Total Cost")
    if not extracted.commodity_group_id or not extracted.commodity_group_name:
        missing_fields.append("Commodity Group")

    # Validate that the returned commodity group id actually exists.
    if extracted.commodity_group_id is not None:
        valid_ids = {g.id for g in commodity_groups}
        if extracted.commodity_group_id not in valid_ids:
            logger.warning(
                "Invalid commodity group ID returned: %s", extracted.commodity_group_id
            )
            extracted.commodity_group_id = None
            extracted.commodity_group_name = None
            if "Commodity Group" not in missing_fields:
                missing_fields.append("Commodity Group")

    return ExtractionResponse(
        success=True,
        data=extracted,
        missing_fields=missing_fields or None,
    )


# ---------------------------------------------------------------------------
# Rich extraction from PDF layout rows (POST /extraction/pdf)
# ---------------------------------------------------------------------------

EXTRACTION_FAILED = (
    "Extraction failed \u2014 please try again or fill in the form manually"
)
SCAN_ERROR = "No text found in this PDF — it may be a scan"

PDF_SYSTEM_PROMPT = (
    "You are a procurement data extraction specialist. You read vendor quotes "
    "given as numbered text rows of the form [row id] text, and return the "
    "structured data requested. Never invent values: use null when the quote "
    "does not state something."
)


def _build_pdf_prompt(rows_text: str, commodity_groups_list: str) -> str:
    return f"""Extract the vendor quote below.

Quote rows:
{rows_text}

Rules:
- The vendor is the seller who issued the quote, never the recipient/customer.
- vendor_tax_id is only the vendor's EIN / Tax ID. Never use a customer number, quote number or permit number; otherwise null.
- department is the BUYER's department only, and only if the quote states one. Never use the seller's team or contact (e.g. an "Apple Business Team" on an Apple quote is not a department; use null unless the buyer's own department is stated). Never use the buyer company name.
- Return every priced line in document order. Section headings get kind "heading".
- description is the position's short name as printed (e.g. Moss panel "70:30" with "askLio" lettering), at most about 120 characters. Never the whole specification paragraph; drop the spec details.
- Use the vendor's own position number in `position` (e.g. "3", "1.1a"), else null.
- Lines labelled alternative or option are kind "alternative", with parent_position set to the position of the item they replace. Optional add-ons are kind "optional" (parent_position = the item they are an add-on to, or null).
- Discounts, surcharges, shipping and fees that the quote ties to one position get that parent_position; otherwise parent_position is null (applies to the whole quote).
- A line-level "Disc./Surch." percentage goes into list_unit_price (the price before the adjustment) and adjustment_percent (negative = discount, positive = surcharge). unit_price is the effective price.
- Document-level discounts or surcharges that have an amount become their own lines (kind "discount" with a negative total, or "surcharge"). If they have no amount, list them in unpriced_charges instead.
- Shipping and freight are kind "shipping"; recycling and handling fees are kind "fee".
- Use plain numbers only (no currency symbols). US number formatting: "1,438.00" = 1438.00. Write units out in plain English (e.g. "ea." -> "pieces").
- net_total is the subtotal before tax, tax_total the tax, grand_total the amount payable including tax.
- source_rows lists the id of every row a line's text and price come from, including continuation rows on later pages.
- field_source_rows cites, for vendor_name, vendor_tax_id, department, quote_number and valid_until, the row id(s) holding the value itself (e.g. the 'EIN: 94 1985704' row, the vendor's name/letterhead row); an empty list when the value is not on the quote.
- valid_until is an ISO date (YYYY-MM-DD). title is a SHORT (2-5 words) description of what is purchased.
- commodity_group_id: choose the SINGLE most appropriate group below, or null if genuinely uncertain.

Available Commodity Groups (ID | Category | Name):
{commodity_groups_list}"""


def _get_client() -> OpenAI:
    """Single seam for the OpenAI client; tests monkeypatch this."""
    return OpenAI(api_key=settings.openai_api_key)


def _unique_keys(lines: list[LlmLine]) -> tuple[list[str], dict[str, str]]:
    """Per-line keys plus a position -> key map (first line wins)."""
    keys: list[str] = []
    seen: Counter[str] = Counter()
    used: set[str] = set()
    pos_to_key: dict[str, str] = {}
    for n, ln in enumerate(lines, start=1):
        pos = (ln.position or "").strip()
        base = pos or f"L{n}"
        seen[base] += 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        while key in used:
            seen[base] += 1
            key = f"{base}#{seen[base]}"
        used.add(key)
        keys.append(key)
        if pos:
            pos_to_key.setdefault(pos, key)
    return keys, pos_to_key


def _round(v: float) -> float:
    return round(v + 0.0, 2)


def _line_numbers(ln: LlmLine) -> tuple[float, float, float]:
    """(unit_price, amount, total_price) for a line, per the line-math rules."""
    if ln.list_unit_price is not None and ln.adjustment_percent is not None:
        unit_price: float | None = _round(
            ln.list_unit_price * (1 + ln.adjustment_percent / 100)
        )
    elif ln.unit_price is not None:
        unit_price = ln.unit_price
    else:
        unit_price = ln.list_unit_price
    if ln.quantity and unit_price is not None:
        total = _round(ln.quantity * unit_price)
    else:
        total = ln.total_price or 0.0
    unit_price = unit_price or 0.0
    if ln.kind == "discount":
        total = -abs(total)
        unit_price = -abs(unit_price)
    return unit_price, ln.quantity or 0.0, total


def _normalise_line(ln: LlmLine) -> LlmLine:
    """Fill gaps on lines that only carry a total (shipping, fees, surcharges)."""
    updates: dict = {}
    unit_price = ln.unit_price
    has_adjusted_unit = (
        ln.list_unit_price is not None and ln.adjustment_percent is not None
    )
    if unit_price is None and ln.list_unit_price is not None and not has_adjusted_unit:
        unit_price = ln.list_unit_price
        updates["unit_price"] = unit_price
    if ln.total_price:
        if has_adjusted_unit:
            if not ln.quantity:
                updates["quantity"] = 1
        elif not ln.quantity:
            updates["quantity"] = 1
            updates["unit_price"] = ln.total_price
        elif not unit_price:
            derived = _round(ln.total_price / ln.quantity)
            if _round(ln.quantity * derived) == _round(ln.total_price):
                updates["unit_price"] = derived
            else:  # inexact split: keep the stated total
                updates["quantity"] = 1
                updates["unit_price"] = ln.total_price
    return ln.model_copy(update=updates) if updates else ln


def _money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


PRICE_TOL_ABS = 0.01
PRICE_TOL_REL = 0.005
TOTALS_TOL_PER_LINE = 0.01  # rounding slack allowed per active line in totals checks


def _tol(ref: float) -> float:
    """Tolerance for price verification and the per-line math check."""
    return max(PRICE_TOL_ABS, PRICE_TOL_REL * abs(ref)) + 1e-9


def _totals_tol(active_line_count: int) -> float:
    """Tight tolerance for the totals checks: one cent of rounding per line."""
    return TOTALS_TOL_PER_LINE * max(1, active_line_count) + 1e-9


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUMBER_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _merge_boxes(rows: list) -> list[Box]:
    """Union vertically adjacent row boxes per page (gap < 1.5 row heights)."""
    boxes: list[Box] = []
    for page in sorted({r.page for r in rows}):
        group = sorted((r.box for r in rows if r.page == page), key=lambda b: b.y0)
        cur: Box | None = None
        height = 0.0
        for b in group:
            if cur is not None and b.y0 - cur.y1 < 1.5 * height:
                cur = Box(page=page, x0=min(cur.x0, b.x0), y0=min(cur.y0, b.y0),
                          x1=max(cur.x1, b.x1), y1=max(cur.y1, b.y1))
            else:
                if cur is not None:
                    boxes.append(cur)
                cur = b.model_copy()
            height = b.y1 - b.y0
        if cur is not None:
            boxes.append(cur)
    return boxes


def _cited_rows(row_ids: list[str], layout: PdfLayout) -> list:
    """Layout rows for the cited ids, in citation order; unknown ids and repeats dropped."""
    by_id = {r.id: r for r in layout.rows}
    rows = []
    for rid in row_ids:
        row = by_id.get(rid)
        if row is not None and row not in rows:
            rows.append(row)
    return rows


def boxes_for_rows(row_ids: list[str], layout: PdfLayout) -> list[Box]:
    """Merged boxes for the cited row ids; unknown ids are dropped."""
    return _merge_boxes(_cited_rows(row_ids, layout))


# LlmFieldSources attribute -> (ExtractedVendorData wire key, LlmQuote value attribute)
_HEADER_FIELDS = (
    ("vendor_name", "vendorName", "vendor_name"),
    ("vendor_tax_id", "vatId", "vendor_tax_id"),
    ("department", "department", "department"),
    ("quote_number", "quoteNumber", "quote_number"),
    ("valid_until", "validUntil", "valid_until"),
)


def resolve_field_sources(q: LlmQuote, layout: PdfLayout) -> dict[str, list[Box]]:
    out: dict[str, list[Box]] = {}
    for attr, key, value_attr in _HEADER_FIELDS:
        if not getattr(q, value_attr):
            continue
        boxes = boxes_for_rows(getattr(q.field_source_rows, attr), layout)
        if boxes:
            out[key] = boxes
    return out


def resolve_sources(
    line: LlmLine, layout: PdfLayout
) -> tuple[list[Box], str | None, bool]:
    """(merged boxes, source_text, price verified) from the line's cited rows."""
    rows = _cited_rows(line.source_rows, layout)
    if not rows:
        return [], None, False

    source_text: str | None = None
    for row in rows:
        segs = [s for s in row_segments(row) if sum(c.isalpha() for c in s) >= 3]
        if segs:
            source_text = max(segs, key=len)
            break

    unit_price, _, total = _line_numbers(line)
    targets = [abs(v) for v in (total, unit_price) if v]
    found = [n for r in rows for n in _numbers(r.text)]
    verified = any(abs(n - t) <= _tol(t) for t in targets for n in found)
    return _merge_boxes(rows), source_text, verified


def reconcile(
    lines: list[ExtractedLine],
    net_total: float | None,
    tax_total: float | None,
    grand_total: float | None,
) -> list[str]:
    """Warnings where the quote's stated totals disagree with its lines."""
    warnings: list[str] = []
    keys = active_keys(lines)
    tol = _totals_tol(sum(1 for ln in lines if ln.key in keys))
    if net_total is not None:
        computed = _round(sum(ln.total_price for ln in lines if ln.key in keys))
        if abs(computed - net_total) > tol:
            warnings.append(
                f"Line items add up to {_money(computed)} but the quote's net "
                f"total is {_money(net_total)}"
            )
    if net_total is not None and tax_total is not None and grand_total is not None:
        expected = _round(net_total + tax_total)
        if abs(expected - grand_total) > tol:
            warnings.append(
                f"Net total {_money(net_total)} plus tax {_money(tax_total)} is "
                f"{_money(expected)} but the quote's grand total is "
                f"{_money(grand_total)}"
            )
    return warnings


def to_extracted(
    q: LlmQuote, layout: PdfLayout, groups: dict[int, str]
) -> tuple[ExtractedVendorData, list[str]]:
    """Turn the model's quote into API data plus human-readable warnings."""
    warnings: list[str] = []
    kept = [ln for ln in q.lines if ln.kind != "heading"]
    keys, pos_to_key = _unique_keys(kept)

    out: list[ExtractedLine] = []
    for ln, key in zip(kept, keys):
        ln = _normalise_line(ln)
        kind = ln.kind  # never "heading" here
        parent_key: str | None = None
        if kind != "item" and ln.parent_position:
            parent_key = pos_to_key.get(ln.parent_position.strip())
            if parent_key is None:
                warnings.append(
                    f"Line {key}: parent position {ln.parent_position} not found; "
                    "treated as not linked"
                )
            elif parent_key == key:
                parent_key = None
        if kind == "alternative" and parent_key is None:
            kind = "optional"
        unit_price, amount, total = _line_numbers(ln)
        if ln.total_price is not None and ln.quantity and unit_price:
            expected = _round(ln.quantity * unit_price)
            if abs(abs(ln.total_price) - abs(expected)) > _tol(expected):
                warnings.append(
                    f"Line {key}: total {_money(ln.total_price)} doesn't match "
                    f"quantity x unit price ({_money(expected)})"
                )
        boxes, source_text, verified = resolve_sources(ln, layout)
        if total and not verified:
            warnings.append(
                f"Couldn't find line {key}'s price ({_money(total)}) in the document"
            )
        out.append(
            ExtractedLine(
                key=key,
                parent_key=parent_key,
                kind=kind,
                position=(ln.position or "").strip() or None,
                vendor_item_number=ln.vendor_item_number,
                position_description=ln.description or "",
                unit_price=unit_price,
                amount=amount,
                unit=ln.unit or "",
                total_price=total,
                list_unit_price=ln.list_unit_price,
                adjustment_percent=ln.adjustment_percent,
                sources=boxes,
                source_text=source_text,
            )
        )

    for charge in q.unpriced_charges:
        warnings.append(f"Quote mentions a charge without an amount: {charge}")

    keys_active = active_keys(out)
    computed = _round(sum(ln.total_price for ln in out if ln.key in keys_active))
    tax = q.tax_total
    net = q.net_total if q.net_total is not None else computed
    total_cost = (
        q.grand_total if q.grand_total is not None else _round(computed + (tax or 0))
    )

    warnings.extend(reconcile(out, q.net_total, tax, q.grand_total))

    group_id = q.commodity_group_id if q.commodity_group_id in groups else None
    data = ExtractedVendorData(
        title=q.title or "",
        vendor_name=q.vendor_name or "",
        vat_id=q.vendor_tax_id or "",
        department=q.department or "",
        order_lines=out,
        total_cost=total_cost,
        commodity_group_id=group_id,
        commodity_group_name=groups.get(group_id) if group_id is not None else None,
        quote_number=q.quote_number,
        valid_until=q.valid_until,
        currency=q.currency,
        net_total=net,
        tax_total=tax,
        field_sources=resolve_field_sources(q, layout),
    )
    return data, warnings


def _missing_fields(data: ExtractedVendorData) -> list[str]:
    missing: list[str] = []
    if not data.title.strip():
        missing.append("Title/Short Description")
    if not data.vendor_name.strip():
        missing.append("Vendor Name")
    if not data.vat_id.strip():
        missing.append("Tax ID")
    if not data.department.strip():
        missing.append("Department")
    if not data.order_lines:
        missing.append("Order Lines")
    if not data.total_cost:
        missing.append("Total Cost")
    if not data.commodity_group_id or not data.commodity_group_name:
        missing.append("Commodity Group")
    return missing


def _finish(data: ExtractedVendorData, warnings: list[str]) -> ExtractionResponse:
    return ExtractionResponse(
        success=True,
        data=data,
        missing_fields=_missing_fields(data) or None,
        warnings=warnings or None,
    )


def _fail(error: str) -> ExtractionResponse:
    return ExtractionResponse(success=False, error=error)


def extract_from_pdf(pdf_bytes: bytes, db: Session) -> ExtractionResponse:
    """PDF -> layout rows -> one structured model call -> structured lines."""
    try:
        layout = extract_layout(pdf_bytes)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read PDF layout")
        return _fail("Failed to read this PDF")
    if not layout.rows:
        return _fail(SCAN_ERROR)

    if not settings.openai_api_key:
        return _fail("OPENAI_API_KEY is not configured on the server")

    commodity_groups = list(
        db.scalars(select(CommodityGroup).order_by(CommodityGroup.id))
    )
    if not commodity_groups:
        return _fail("Failed to load commodity groups for classification")
    groups = {g.id: g.name for g in commodity_groups}
    groups_list = "\n".join(f"{g.id} | {g.category} | {g.name}" for g in commodity_groups)

    kwargs: dict = dict(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": PDF_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_pdf_prompt(layout_to_prompt_text(layout), groups_list),
            },
        ],
        response_format=LlmQuote,
        max_completion_tokens=8000,
    )
    if settings.openai_temperature is not None:
        kwargs["temperature"] = settings.openai_temperature

    too_long = (
        "The quote is too long to extract in one pass (the AI response was cut off)"
    )
    try:
        completion = _get_client().chat.completions.parse(**kwargs)
    except openai.LengthFinishReasonError:
        return _fail(too_long)
    except openai.ContentFilterFinishReasonError:
        return _fail("The AI provider's content filter blocked this document")
    except openai.OpenAIError:
        logger.exception("OpenAI request failed")
        return _fail(EXTRACTION_FAILED)
    except Exception:  # noqa: BLE001
        logger.exception("Error extracting quote")
        return _fail(EXTRACTION_FAILED)

    choice = completion.choices[0]
    if choice.finish_reason == "length":
        return _fail(too_long)
    if choice.message.refusal:
        return _fail(f"The AI declined to process this document: {choice.message.refusal}")
    quote = choice.message.parsed
    if quote is None:
        return _fail("Empty response from AI")

    data, warnings = to_extracted(quote, layout, groups)
    return _finish(data, warnings)
