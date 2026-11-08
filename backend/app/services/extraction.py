import datetime
import json
import logging
import re
import uuid
from collections import Counter

import openai
from openai import OpenAI
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CommodityGroup
from app.schemas.extraction import (
    WarningDetail,
    ExtractedLine,
    ExtractedVendorData,
    ExtractionResponse,
    LlmLine,
    LlmQuote,
)
from app.services.commodity_groups import active_groups
from app.services.line_links import (
    active_keys,
    inherited_groups,
    parent_allowed,
    primary_group,
)
from app.services.pdf_layout import (
    Box,
    PdfLayout,
    Word,
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


BOOKING_RULE = (
    "Group descriptions are this organization's booking rules. When an item "
    "matches a description, use that group, even if another group's name looks "
    "more specific."
)


def format_groups(groups: list[CommodityGroup]) -> str:
    """One ``ID | Category | Name | Description`` row per group."""
    return "\n".join(
        f"{g.id} | {g.category} | {g.name} | {g.description or ''}".rstrip()
        for g in groups
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

Available Commodity Groups (ID | Category | Name | Description):
{commodity_groups_list}

{BOOKING_RULE}

IMPORTANT INSTRUCTIONS:
- For the title, create a SHORT (2-5 words) description based on the main items being purchased
- Extract ALL line items, even if there are many
- For unit prices and totals, extract only the numeric value (remove currency symbols)
- For the unit field, write out abbreviations in plain English (e.g., "ea." -> "pieces", "lic." -> "licenses")
- Be careful with US number formatting (e.g., "1,438.00" = 1438.00)
- For classification, consider the PRIMARY nature and purpose of the items and use only an ID from the list above
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
  "commodityGroupId": 123,
  "commodityGroupName": "Name of the chosen group"
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


def extract_vendor_data(
    pdf_text: str, db: Session, organization_id: uuid.UUID | None
) -> ExtractionResponse:
    """Extract vendor data and classify commodity group in one AI call.

    Port of ``src/actions/extraction.ts`` — same model and validation; the
    prompt lists the org's active groups with their booking rules.
    """
    if not pdf_text or not pdf_text.strip():
        return ExtractionResponse(success=False, error="No text provided for extraction")

    if not settings.openai_api_key:
        return ExtractionResponse(
            success=False,
            error="OPENAI_API_KEY is not configured on the server",
        )

    commodity_groups = active_groups(db, organization_id)
    if not commodity_groups:
        return ExtractionResponse(
            success=False,
            error="Failed to load commodity groups for classification",
        )

    # Give the model the org's full active catalog so it has complete context
    # to pick the most accurate match in a single pass.
    commodity_groups_list = format_groups(commodity_groups)

    try:
        response = _get_client().chat.completions.create(
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
- Document-level discounts or surcharges that have an amount become their own lines (kind "discount" with a negative total, or "surcharge"). If they have no amount, list them in unpriced_charges instead: text is the charge as printed, source_rows the id(s) of the row(s) where it is printed.
- Shipping and freight are kind "shipping"; recycling and handling fees are kind "fee".
- Use plain numbers only (no currency symbols). US number formatting: "1,438.00" = 1438.00. Write units out in plain English (e.g. "ea." -> "pieces").
- total_source_rows cites, for net, tax and grand, the row id(s) where that total's amount is printed (e.g. the 'Total $1,534.59' row); an empty list when the quote does not print it.
- net_total is the subtotal before tax, tax_total the tax, grand_total the amount payable including tax.
- source_rows lists the id of every row a line's text and price come from, including continuation rows on later pages.
- vendor_tax_id: the vendor's EIN / Tax ID is often printed only in the footer or letterhead small print (e.g. 'CEO: ... | EIN: 12-3456789'); look there before giving up.
- vendor_name: prefer the letterhead / sender line over a later mention in the terms.
- field_source_rows cites, for vendor_name, vendor_tax_id, department, quote_number and valid_until, the row id(s) holding the value itself (e.g. the 'EIN: 12-3456789' row, the vendor's name/letterhead row); an empty list when the value is not on the quote.
- valid_until is an ISO date (YYYY-MM-DD). title is a SHORT (2-5 words) description of what is purchased.
- Classify every line: its commodity_group_id is the SINGLE most appropriate group below for that line, or null if genuinely uncertain. Discounts, surcharges, shipping and fees tied to a position take that position's group.
- commodity_group_id (quote level): the group of the largest share of the spend, or null if genuinely uncertain.
- Use only IDs from the list below.

Available Commodity Groups (ID | Category | Name | Description):
{commodity_groups_list}

{BOOKING_RULE}"""


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


_CORP_WORDS = {"inc", "llc", "corp", "co", "ltd", "gmbh", "the", "and", "of"}
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
# Labels that may directly precede a value and are boxed with it (normalised).
_LABELS = {"ein", "taxid", "taxidno", "taxnumber", "vatid", "vat", "validuntil", "validthru",
           "validto", "expires", "quote", "quoteno", "quotenumber", "quotation", "no",
           "number", "department", "dept"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _date_forms(value: str) -> list[str]:
    """Printed forms of an ISO date (normalised), most specific first."""
    try:
        d = datetime.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return []
    mon, month = d.strftime("%b"), d.strftime("%B")
    forms = [
        f"{d.month}/{d.day}/{d.year}", f"{d.month}/{d.day}/{d.year % 100:02d}",
        f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}",
        f"{mon} {d.day}, {d.year}", f"{month} {d.day}, {d.year}",
        f"{d.day} {mon} {d.year}", f"{d.day} {month} {d.year}",
        f"{d.day:02d}.{d.month:02d}.{d.year}", d.isoformat(),
    ]
    return list(dict.fromkeys(_norm(f) for f in forms))


def _tokens(value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", value.lower())
            if len(t) > 1 and t not in _CORP_WORDS}


def _union(page: int, row, words: list[Word]) -> Box:
    return Box(page=page, x0=min(w.x0 for w in words), y0=row.box.y0,
               x1=max(w.x1 for w in words), y1=row.box.y1)


def _split_fused(words: list[Word]) -> list[Word]:
    """Split words fused with a label at a colon ('number:1800...'), x positions
    divided in proportion to the characters."""
    out: list[Word] = []
    for w in words:
        parts = [p for p in re.split(r"(?<=:)", w.text) if p]
        if len(parts) < 2 or not w.text:
            out.append(w)
            continue
        x = w.x0
        for p in parts:
            nx = x + (w.x1 - w.x0) * len(p) / len(w.text)
            out.append(Word(text=p, x0=x, x1=nx))
            x = nx
    return out


def _find_window(row, words: list[Word], targets: list[str], norm,
                 labels: bool, weekday: bool) -> Box | None:
    """Smallest contiguous run of WHOLE words whose normalised text equals a target
    (never part of a word); widened by a directly preceding label / weekday name."""
    keys = [norm(w.text) for w in words]
    texts = [_norm(w.text) for w in words]  # for labels/weekdays, whatever the field
    longest = max(len(t) for t in targets)
    best: tuple[int, int] | None = None  # (length, start)
    best_span = None
    for i, k in enumerate(keys):
        if not k:
            continue
        acc = ""
        for j in range(i, len(words)):
            acc += keys[j]
            if len(acc) > longest:
                break
            if keys[j] and acc in targets:
                cand = (len(acc), i)
                if best is None or cand < best:
                    best, best_span = cand, (i, j)
                break
    if best_span is None:
        return None
    i, j = best_span
    if labels:
        for k in (3, 2, 1):  # longest preceding label wins
            if i - k >= 0 and "".join(texts[i - k:i]) in _LABELS:
                i -= k
                break
    if weekday and i > 0 and texts[i - 1] in _WEEKDAYS:
        i -= 1
    return _union(row.page, row, words[i:j + 1])


def _gap_segments(row) -> list[list[Word]]:
    """The row's words grouped into gap-separated segments (columns)."""
    seg: list[Word] = []
    segments: list[list[Word]] = []
    prev: Word | None = None
    for w in row.words:
        if prev is None or (w.x0 - prev.x1) > 2.0 * row.char_width:
            seg = []
            segments.append(seg)
        seg.append(w)
        prev = w
    return segments


def _segment_box(row, tokens: set[str]) -> Box | None:
    """Box of the first gap-separated segment of the row that shares a token."""
    if not row.words:
        return row.box if tokens & _tokens(row.text) else None
    for seg in _gap_segments(row):
        if tokens & _tokens(" ".join(w.text for w in seg)):
            return _union(row.page, row, seg)
    return None


def _field_box(attr: str, value: str, cited: list, layout: PdfLayout) -> Box | None:
    """One tight box for a header value: first word-window match in reading order,
    cited rows first, then every row; else the matching gap segment of the first
    cited row; else nothing."""
    is_id = attr == "vendor_tax_id"
    if is_id:
        targets, norm = [_digits(value)], _digits
    elif attr == "valid_until":
        targets, norm = _date_forms(value) or [_norm(value)], _norm
    else:
        targets, norm = [_norm(value)], _norm
    targets = [t for t in targets if t]
    if not targets:
        return None

    labels = attr in ("vendor_tax_id", "quote_number", "valid_until")
    weekday = attr == "valid_until"

    def search(rows: list, tolerant: bool) -> Box | None:
        for row in rows:
            if row.words:
                words = _split_fused(row.words) if tolerant else row.words
                box = _find_window(row, words, targets, norm, labels, weekday)
            elif tolerant:  # no word positions: the row is all we can point at
                box = row.box if any(t in norm(row.text) for t in targets) else None
            else:
                box = None
            if box is not None:
                return box
        return None

    order = {r.id: n for n, r in enumerate(layout.rows)}
    cited = sorted(cited, key=lambda r: order.get(r.id, 0))
    # exact whole-word match in any cited row, then tolerant of label-fused words,
    # then the whole layout (the model may have cited the wrong row)
    box = search(cited, False) or search(cited, True) or search(layout.rows, False)
    if box is None and cited and not is_id and attr != "valid_until":
        box = _segment_box(cited[0], _tokens(value))
    return box


def resolve_field_sources(q: LlmQuote, layout: PdfLayout) -> dict[str, list[Box]]:
    """At most one tight, word-level box per header field."""
    out: dict[str, list[Box]] = {}
    for attr, key, value_attr in _HEADER_FIELDS:
        value = getattr(q, value_attr)
        if not value:
            continue
        cited = _cited_rows(getattr(q.field_source_rows, attr), layout)
        box = _field_box(attr, str(value), cited, layout)
        if box is not None:
            out[key] = [box]
    return out


def _dedupe(boxes: list[Box]) -> list[Box]:
    out: list[Box] = []
    for b in boxes:
        if b not in out:
            out.append(b)
    return out


def charge_boxes(text: str, row_ids: list[str], layout: PdfLayout) -> list[Box]:
    """One word-tight box for an unpriced charge: the first cited row where the
    charge's words (whole text, then the part before a colon / bracket) appear as
    whole words; else the gap-segment of the first cited row sharing a word."""
    candidates = [text]
    head = re.split(r"[:(\u2013\u2014]| - ", text, maxsplit=1)[0].strip()
    if head and head != text:
        candidates.append(head)
    rows = _cited_rows(row_ids, layout)
    for row in rows:
        if not row.words:
            continue
        for cand in candidates:
            target = _norm(cand)
            box = _find_window(row, row.words, [target], _norm, False, False) if target else None
            if box is not None:
                return [box]
    for row in rows:
        box = _segment_box(row, _tokens(head or text))
        if box is not None:
            return [box]
    return []


def total_boxes(amount: float | None, row_ids: list[str], layout: PdfLayout) -> list[Box]:
    """Word-tight box on the printed amount in the cited rows; else the gap
    segment holding the number; else nothing (never a full-width row)."""
    if amount is None:
        return []
    forms = {_digits(f"{abs(amount):.2f}")}
    if float(abs(amount)).is_integer():
        forms.add(str(int(abs(amount))))
    boxes: list[Box] = []
    for row in _cited_rows(row_ids, layout):
        if not row.words:
            continue
        box = _find_window(row, row.words, sorted(forms), _digits, False, False)
        if box is None:
            for seg in _gap_segments(row):
                text = " ".join(w.text for w in seg)
                if any(abs(n - abs(amount)) <= 0.005 for n in _numbers(text)):
                    box = _union(row.page, row, seg)
                    break
        if box is not None:
            boxes.append(box)
    return _dedupe(boxes)


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
    total_sources: dict[str, list[Box]] | None = None,
) -> list[WarningDetail]:
    """Warnings where the quote's stated totals disagree with its lines.

    The deciding check is active lines + tax against the grand total. When it
    holds, the quote is consistent even if its stated "net" or "subtotal" is
    items only (Apple adds a $4 recycling fee after the subtotal).

    total_sources maps net/tax/grand to the boxes of the printed amounts; the
    grand-total warning points at grand + tax, the net warning at net.
    """
    warnings: list[WarningDetail] = []
    total_sources = total_sources or {}
    keys = active_keys(lines)
    active = [ln for ln in lines if ln.key in keys]
    tol = _totals_tol(len(active))
    lines_sum = _round(sum(ln.total_price for ln in active))
    # Items only: what a quote's "subtotal" often covers.
    items_sum = _round(
        sum(ln.total_price for ln in active if ln.kind not in ("shipping", "fee", "surcharge"))
    )

    # Without a stated tax the grand total can't be checked.
    if grand_total is not None and tax_total is not None:
        expected = _round(lines_sum + tax_total)
        if abs(expected - grand_total) <= tol:
            return warnings
        tax_words = f" plus tax {_money(tax_total)}" if tax_total else ""
        warnings.append(WarningDetail(
            message=(
                f"Line items {_money(lines_sum)}{tax_words} come to {_money(expected)} "
                f"but the quote's grand total is {_money(grand_total)}"
            ),
            sources=_dedupe(
                total_sources.get("grand", []) + total_sources.get("tax", [])
            ),
        ))

    if net_total is not None and all(
        abs(s - net_total) > tol for s in (lines_sum, items_sum)
    ):
        warnings.append(WarningDetail(
            message=(
                f"Line items add up to {_money(lines_sum)} but the quote's net "
                f"total is {_money(net_total)}"
            ),
            sources=list(total_sources.get("net", [])),
        ))
    return warnings


def to_extracted(
    q: LlmQuote, layout: PdfLayout, groups: dict[int, str]
) -> tuple[ExtractedVendorData, list[WarningDetail]]:
    """Turn the model's quote into API data plus warnings with their source boxes."""
    warnings: list[WarningDetail] = []
    kept = [ln for ln in q.lines if ln.kind != "heading"]
    keys, pos_to_key = _unique_keys(kept)
    kind_by_key = {key: ln.kind for ln, key in zip(kept, keys)}

    out: list[ExtractedLine] = []
    for ln, key in zip(kept, keys):
        ln = _normalise_line(ln)
        kind = ln.kind  # never "heading" here
        boxes, source_text, verified = resolve_sources(ln, layout)
        parent_key: str | None = None
        if kind != "item" and ln.parent_position:
            parent_key = pos_to_key.get(ln.parent_position.strip())
            if parent_key is None:
                warnings.append(WarningDetail(
                    message=(
                        f"Line {key}: parent position {ln.parent_position} not found; "
                        "treated as not linked"
                    ),
                    sources=boxes,
                ))
            elif parent_key == key:
                parent_key = None
            elif not parent_allowed(kind, kind_by_key[parent_key]):
                warnings.append(WarningDetail(
                    message=(
                        f"Line {key}: a {kind} can't belong to a "
                        f"{kind_by_key[parent_key]} (line {parent_key}); "
                        "treated as not linked"
                    ),
                    sources=boxes,
                ))
                parent_key = None
        if kind == "alternative" and parent_key is None:
            kind = "optional"
            warnings.append(WarningDetail(
                message=(
                    f"Line {key}: alternative without an item to replace; "
                    "treated as optional"
                ),
                sources=boxes,
            ))
        unit_price, amount, total = _line_numbers(ln)
        if ln.total_price is not None and ln.quantity and unit_price:
            expected = _round(ln.quantity * unit_price)
            if abs(abs(ln.total_price) - abs(expected)) > _tol(expected):
                warnings.append(WarningDetail(
                    message=(
                        f"Line {key}: total {_money(ln.total_price)} doesn't match "
                        f"quantity x unit price ({_money(expected)})"
                    ),
                    sources=boxes,
                ))
        if total and not verified:
            warnings.append(WarningDetail(
                message=f"Couldn't find line {key}'s price ({_money(total)}) in the document",
                sources=boxes,
            ))
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
                # An id the org doesn't have (or archived) is dropped.
                commodity_group_id=(
                    ln.commodity_group_id if ln.commodity_group_id in groups else None
                ),
            )
        )

    # Adjustments tied to a line always book to that line's group.
    out = [
        ln if ln.commodity_group_id == gid
        else ln.model_copy(update={"commodity_group_id": gid})
        for ln, gid in zip(out, inherited_groups(out, override=True))
    ]

    for charge in q.unpriced_charges:
        warnings.append(WarningDetail(
            message=f"Quote mentions a charge without an amount: {charge.text}",
            sources=charge_boxes(charge.text, charge.source_rows, layout),
        ))

    keys_active = active_keys(out)
    computed = _round(sum(ln.total_price for ln in out if ln.key in keys_active))
    tax = q.tax_total
    # Server-computed: the stated net may be items only (Apple's subtotal).
    net = computed
    total_cost = (
        q.grand_total if q.grand_total is not None else _round(computed + (tax or 0))
    )

    tsr = q.total_source_rows
    total_sources = {
        "net": total_boxes(q.net_total, tsr.net, layout),
        "tax": total_boxes(tax, tsr.tax, layout),
        "grand": total_boxes(q.grand_total, tsr.grand, layout),
    }
    warnings.extend(reconcile(out, q.net_total, tax, q.grand_total, total_sources))

    # Same rule as on save: the primary group is the largest active spend,
    # falling back to the model's quote-level choice.
    group_id = primary_group(out)
    if group_id is None and q.commodity_group_id in groups:
        group_id = q.commodity_group_id
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


def _finish(
    data: ExtractedVendorData, details: list[WarningDetail]
) -> ExtractionResponse:
    # `warnings` is derived from the details so the two can never diverge.
    return ExtractionResponse(
        success=True,
        data=data,
        missing_fields=_missing_fields(data) or None,
        warnings=[d.message for d in details] or None,
        warning_details=details or None,
    )


def _fail(error: str) -> ExtractionResponse:
    return ExtractionResponse(success=False, error=error)


def extract_from_pdf(
    pdf_bytes: bytes, db: Session, organization_id: uuid.UUID | None
) -> ExtractionResponse:
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

    commodity_groups = active_groups(db, organization_id)
    if not commodity_groups:
        return _fail("Failed to load commodity groups for classification")
    groups = {g.id: g.name for g in commodity_groups}
    groups_list = format_groups(commodity_groups)

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

    data, details = to_extracted(quote, layout, groups)
    return _finish(data, details)
