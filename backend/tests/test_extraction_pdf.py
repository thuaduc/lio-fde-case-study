"""Rich PDF extraction: LLM output -> structured lines. No network: the OpenAI
client is replaced by a fake that records calls and returns canned quotes."""
import uuid
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.deps import get_current_user, get_optional_membership
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import CommodityGroup, Organization
from app.schemas.extraction import (
    LlmFieldSources, LlmLine, LlmQuote, LlmTotalSources,
    LlmUnpricedCharge,
)
from app.services import extraction as svc
from app.services.pdf_layout import Box, PdfLayout, PdfRow, Word

NO_TEXT = "No text found in this PDF — it may be a scan"


def line(**kw) -> LlmLine:
    base = dict(
        position=None, vendor_item_number=None, description="Thing", kind="item",
        parent_position=None, quantity=1, unit="ea", list_unit_price=None,
        adjustment_percent=None, unit_price=None, total_price=10.0, source_rows=[],
        commodity_group_id=None,
    )
    base.update(kw)
    return LlmLine(**base)


def fsr(**kw) -> LlmFieldSources:
    base = dict(vendor_name=[], vendor_tax_id=[], department=[], quote_number=[],
                valid_until=[])
    base.update(kw)
    return LlmFieldSources(**base)


def tsr(**kw) -> LlmTotalSources:
    base = dict(net=[], tax=[], grand=[])
    base.update(kw)
    return LlmTotalSources(**base)


def quote(lines, **kw) -> LlmQuote:
    base = dict(
        title="Quote", vendor_name="Acme", vendor_tax_id="12-3456789", department="IT",
        quote_number="Q1", valid_until="2026-12-31", currency="USD", lines=lines,
        net_total=None, tax_total=None, grand_total=None, unpriced_charges=[],
        total_source_rows=tsr(), commodity_group_id=None, field_source_rows=fsr(),
    )
    base.update(kw)
    return LlmQuote(**base)


GROUPS = {15: "Office Equipment"}
LAYOUT = PdfLayout(rows=[], page_count=1)


def conv(q, groups=GROUPS):
    data, warnings = svc.to_extracted(q, LAYOUT, groups)
    return data, warnings


def test_heading_dropped():
    data, _ = conv(quote([line(kind="heading", description="Section A"), line(position="1")]))
    assert [l.key for l in data.order_lines] == ["1"]


def test_links_resolved():
    q = quote([
        line(position="3", unit_price=100, total_price=100),
        line(position="5", kind="alternative", parent_position="3"),
        line(kind="shipping", description="Freight"),
        line(position="4", kind="discount", parent_position="3", unit_price=-5, total_price=-5),
    ])
    data, _ = conv(q)
    lines = data.order_lines
    assert lines[1].parent_key == "3" and lines[1].kind == "alternative"
    assert lines[2].kind == "shipping" and lines[2].parent_key is None
    assert lines[2].key == "L3"
    assert lines[3].parent_key == "3"


def test_unknown_parent_dropped():
    q = quote([
        line(position="1"),
        line(position="2", kind="alternative", parent_position="9"),
        line(position="3", kind="surcharge", parent_position="9"),
    ])
    data, warnings = conv(q)
    alt, sur = data.order_lines[1], data.order_lines[2]
    assert alt.parent_key is None and alt.kind == "optional"
    assert sur.parent_key is None and sur.kind == "surcharge"
    assert any("9" in w for w in msgs(warnings))


def test_duplicate_positions_get_unique_keys():
    data, _ = conv(quote([line(position="1"), line(position="1"), line(position="1")]))
    assert [l.key for l in data.order_lines] == ["1", "1#2", "1#3"]


def test_line_discount_fields():
    q = quote([line(position="1", quantity=2, list_unit_price=894.08,
                    adjustment_percent=-20, unit_price=None, total_price=None)])
    l = conv(q)[0].order_lines[0]
    assert (l.list_unit_price, l.adjustment_percent, l.unit_price) == (894.08, -20, 715.26)
    assert l.total_price == 1430.52 and l.amount == 2


def test_null_tax_id_success():
    data, _ = conv(quote([line(position="1")], vendor_tax_id=None))
    assert data.vat_id == ""
    resp = svc._finish(data, [])
    assert resp.success and "Tax ID" in resp.missing_fields


def test_unpriced_charges_become_warnings():
    _, w = conv(quote([line(position="1")], unpriced_charges=[
        LlmUnpricedCharge(text="fuel surcharge", source_rows=[])]))
    assert "Quote mentions a charge without an amount: fuel surcharge" in msgs(w)


def test_net_total_is_sum_of_active_lines_not_stated_subtotal():
    # Apple: the printed subtotal (1,409.06) is items only; the $4 fee is active.
    # netTotal is server-computed (1,413.06); totalCost stays the quote's grand.
    # reconcile() sees lines + tax == grand, so it raises no warnings.
    q = quote(
        [line(position="1", total_price=1409.06),
         line(position="2", kind="fee", total_price=4.00)],
        net_total=1409.06, tax_total=121.53, grand_total=1534.59,
    )
    data, warnings = conv(q)
    assert data.net_total == 1413.06
    assert data.total_cost == 1534.59
    assert data.tax_total == 121.53
    # Only the unverified-price warnings remain (the canned layout has no rows).
    assert all(w.message.startswith("Couldn't find line") for w in warnings)


def test_inactive_parent_excludes_adjustment():
    q = quote([
        line(position="1", total_price=100),
        line(position="2", kind="optional", total_price=50),
        line(kind="discount", parent_position="2", total_price=-5),
    ])
    assert conv(q)[0].net_total == 100


# --- extract_from_pdf with a fake client --------------------------------------


class FakeClient:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result, self._exc = result, exc
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return self._result


def completion(parsed=None, finish="stop", refusal=None):
    msg = SimpleNamespace(parsed=parsed, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=msg)])


ORG = uuid.uuid4()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Organization(id=ORG, name="Org", slug="org"))
    s.add(CommodityGroup(id=15, organization_id=ORG, category="Facilities",
                         name="Office Equipment"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def rows_layout(monkeypatch):
    row = PdfRow(id="p1r1", page=1, text="Widget 1 x 10.00",
                 box=Box(page=1, x0=0, y0=0, x1=1, y1=0.1))
    monkeypatch.setattr(svc, "extract_layout", lambda b: PdfLayout(rows=[row], page_count=1))
    monkeypatch.setattr(svc.settings, "openai_api_key", "sk-test")


def use(monkeypatch, fake):
    monkeypatch.setattr(svc, "_get_client", lambda: fake)
    return fake


def test_success_call_shape(monkeypatch, db, rows_layout):
    fake = use(monkeypatch, FakeClient(completion(quote([line(position="1")],
                                                         commodity_group_id=15))))
    monkeypatch.setattr(svc.settings, "openai_temperature", 0.0)
    r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert r.success and r.data.commodity_group_name == "Office Equipment"
    kw = fake.calls[0]
    assert kw["response_format"] is LlmQuote and kw["max_completion_tokens"] == 8000
    assert kw["temperature"] == 0.0
    assert "[p1r1] Widget" in kw["messages"][1]["content"]
    assert "15 | Facilities | Office Equipment" in kw["messages"][1]["content"]


def test_scan_pdf_no_model_call(monkeypatch, db):
    monkeypatch.setattr(svc, "extract_layout", lambda b: PdfLayout(rows=[], page_count=2))
    fake = use(monkeypatch, FakeClient())
    r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert not r.success and r.error == NO_TEXT and fake.calls == []


def test_length_and_refusal_errors(monkeypatch, db, rows_layout):
    use(monkeypatch, FakeClient(completion(finish="length")))
    r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert not r.success and "too long" in r.error
    use(monkeypatch, FakeClient(completion(refusal="I can't help")))
    r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert not r.success and "I can't help" in r.error
    use(monkeypatch, FakeClient(completion(parsed=None)))
    assert not svc.extract_from_pdf(b"%PDF-", db, ORG).success


READABLE_FAILURE = "Extraction failed \u2014 please try again or fill in the form manually"


def test_sdk_exception_readable(monkeypatch, db, rows_layout, caplog):
    exc = openai.APIConnectionError(request=SimpleNamespace())
    use(monkeypatch, FakeClient(exc=exc))
    with caplog.at_level("ERROR", logger=svc.logger.name):
        r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert not r.success and r.error == READABLE_FAILURE
    assert any(rec.exc_info and rec.exc_info[1] is exc for rec in caplog.records)


def test_missing_api_key(monkeypatch, db, rows_layout):
    monkeypatch.setattr(svc.settings, "openai_api_key", None)
    fake = use(monkeypatch, FakeClient())
    r = svc.extract_from_pdf(b"%PDF-", db, ORG)
    assert not r.success and "OPENAI_API_KEY" in r.error and fake.calls == []


# --- endpoint ------------------------------------------------------------------


def test_endpoint_requires_pdf_bytes(monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: object())
    monkeypatch.setitem(app.dependency_overrides, get_optional_membership, lambda: None)
    c = TestClient(app)
    r = c.post("/extraction/pdf", files={"file": ("a.pdf", b"hello", "application/pdf")})
    assert r.status_code == 400


def test_endpoint_unauthenticated():
    r = TestClient(app).post("/extraction/pdf", files={"file": ("a.pdf", b"%PDF-", "application/pdf")})
    assert r.status_code == 401


def test_endpoint_happy_path(monkeypatch, db, rows_layout):
    use(monkeypatch, FakeClient(completion(quote([line(position="1")]))))
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: object())
    monkeypatch.setitem(app.dependency_overrides, get_optional_membership,
                        lambda: SimpleNamespace(organization_id=ORG))
    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)
    r = TestClient(app).post("/extraction/pdf",
                             files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")})
    body = r.json()
    assert r.status_code == 200 and body["success"]
    assert body["data"]["orderLines"][0]["key"] == "1"
    assert body["data"]["vatId"] == "12-3456789"


# --- source boxes, price verification, reconciliation ---------------------------

from app.schemas.extraction import ExtractedLine  # noqa: E402
from app.services.pdf_layout import Word  # noqa: E402


def mkrow(rid, text, page=1, y0=0.1, y1=0.12, words=None, cw=0.01):
    return PdfRow(id=rid, page=page, text=text,
                  box=Box(page=page, x0=0.1, y0=y0, x1=0.9, y1=y1),
                  words=words or [], char_width=cw)


def lay(*rows):
    return PdfLayout(rows=list(rows), page_count=3)


def test_unknown_row_ids_ignored():
    l = line(position="1", source_rows=["nope", "also-nope"])
    assert svc.resolve_sources(l, lay(mkrow("a", "x 1.00"))) == ([], None, False)
    l2 = line(position="1", source_rows=[])
    assert svc.resolve_sources(l2, lay()) == ([], None, False)
    data, _ = svc.to_extracted(quote([l]), lay(), GROUPS)
    assert data.order_lines[0].sources == [] and data.order_lines[0].source_text is None


def test_source_text_is_description_segment(quote_bytes):
    from app.services.pdf_layout import extract_layout
    from tests.test_pdf_layout import HOLLIS
    layout = extract_layout(quote_bytes(HOLLIS))
    row = next(r for r in layout.rows if "askLio" in r.text)
    _, text, _ = svc.resolve_sources(line(source_rows=[row.id]), layout)
    assert text == 'Moss panel "70:30" with "askLio"'


def test_boxes_merge_adjacent_rows_per_page():
    r1 = mkrow("a", "x", y0=0.10, y1=0.12)
    r2 = mkrow("b", "y", y0=0.125, y1=0.145)   # adjacent
    r3 = mkrow("c", "z", y0=0.50, y1=0.52)     # far away
    r4 = mkrow("d", "w", page=2, y0=0.13, y1=0.15)  # other page
    boxes, _, _ = svc.resolve_sources(line(source_rows=["a", "b", "c", "d"]),
                                      lay(r1, r2, r3, r4))
    assert [(b.page, b.y0, b.y1) for b in boxes] == [
        (1, 0.10, 0.145), (1, 0.50, 0.52), (2, 0.13, 0.15)]


@pytest.mark.parametrize("text", ["Panel 1,560.00", "Panel $1,560.00", "Panel 1560.00"])
def test_price_verified_formats(text):
    l = line(position="1", total_price=1560.0, quantity=None, source_rows=["a"])
    assert svc.resolve_sources(l, lay(mkrow("a", text)))[2] is True


def test_unverified_price_warns():
    l = line(position="2", quantity=1, unit_price=715.26, total_price=715.26,
             source_rows=["a"])
    _, w = svc.to_extracted(quote([l]), lay(mkrow("a", "Panel 999.00")), GROUPS)
    assert "Couldn't find line 2's price ($715.26) in the document" in msgs(w)
    l2 = line(position="2", quantity=1, unit_price=715.26, total_price=715.26,
              source_rows=["a"])
    _, w2 = svc.to_extracted(quote([l2]), lay(mkrow("a", "Panel 715.26")), GROUPS)
    assert not any("Couldn't find" in x for x in msgs(w2))
    z = line(position="3", quantity=0, unit_price=0, total_price=0)
    _, w3 = svc.to_extracted(quote([z]), lay(), GROUPS)
    assert not any("Couldn't find" in x for x in msgs(w3))


def el(key, total, kind="item", parent=None):
    return ExtractedLine(key=key, parent_key=parent, kind=kind, position_description="",
                         unit_price=total, amount=1, unit="", total_price=total)


def test_lines_vs_net_warns():
    lines = [el("1", 100), el("2", 50), el("3", 900, kind="optional"),
             el("4", -5, kind="discount", parent="3")]
    w = svc.reconcile(lines, 160.0, None, None)
    assert msgs(w) == ["Line items add up to $150.00 but the quote's net total is $160.00"]
    assert svc.reconcile(lines, 150.0, None, None) == []


def test_total_only_shipping_line_normalised():
    ship = line(position="2", kind="shipping", quantity=None, unit=None,
                unit_price=None, total_price=215.0)
    data, w = conv(quote([ship]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, 215.0, 215.0)
    assert not any("quantity x unit price" in x for x in msgs(w))


def test_apple_fee_after_items_subtotal_is_consistent():
    # Apple: subtotal $1,409.06 covers the item only; the $4 fee comes after tax.
    # Lines (1,413.06) + tax (121.53) = grand (1,534.59), so no warning.
    lines = [el("1", 1409.06), el("2", 4.00, kind="fee")]
    assert svc.reconcile(lines, 1409.06, 121.53, 1534.59) == []


def test_missing_fee_still_warns_on_grand_total():
    # Same quote with the $4 fee line dropped: lines + tax miss the grand total.
    w = svc.reconcile([el("1", 1409.06)], 1409.06, 121.53, 1534.59)
    assert msgs(w) == ["Line items $1,409.06 plus tax $121.53 come to $1,530.59 but the quote's grand total is $1,534.59"]


@pytest.mark.parametrize("sign", [1, -1])
def test_discount_sign_normalised_unit_and_total(sign):
    d = line(position="2", kind="discount", quantity=1, unit_price=sign * 10.0,
             total_price=sign * 10.0)
    ln = conv(quote([d]))[0].order_lines[0]
    assert (ln.unit_price, ln.total_price) == (-10.0, -10.0)


def test_quantity_kept_when_unit_price_missing():
    l = line(position="1", quantity=4, unit_price=None, total_price=100.0)
    data, w = conv(quote([l]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (4, 25.0, 100.0)
    assert not any("quantity x unit price" in x for x in msgs(w))


def test_inexact_split_keeps_stated_total():
    l = line(position="1", quantity=3, unit_price=None, total_price=100.0)
    ln = conv(quote([l]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, 100.0, 100.0)


def test_discount_with_list_and_adjustment():
    d = line(position="2", kind="discount", quantity=5, unit_price=None,
             list_unit_price=10.0, adjustment_percent=-10.0, total_price=-45.0)
    ln = conv(quote([d]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (5, -9.0, -45.0)


def test_adjusted_line_not_overwritten_by_total():
    l = line(position="1", quantity=5, unit_price=None, list_unit_price=10.0,
             adjustment_percent=-10.0, total_price=45.0)
    ln = conv(quote([l]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (5, 9.0, 45.0)


def test_line_math_warns():
    l = line(position="1", quantity=3, unit_price=10.0, total_price=45.0)
    _, w = svc.to_extracted(quote([l]), lay(), GROUPS)
    assert any("line 1" in x.lower() and "$45.00" in x and "$30.00" in x for x in msgs(w))
    ok = line(position="1", quantity=3, unit_price=10.0, total_price=30.0)
    _, w2 = svc.to_extracted(quote([ok]), lay(), GROUPS)
    assert not any("quantity" in x for x in msgs(w2))


def test_net_plus_tax_vs_grand_warns():
    w = svc.reconcile([el("1", 100)], 100.0, 8.0, 110.0)
    assert msgs(w) == ["Line items $100.00 plus tax $8.00 come to $108.00 but the quote's grand total is $110.00"]
    assert svc.reconcile([el("1", 100)], 100.0, 8.0, 108.0) == []


def test_none_totals_no_crash():
    assert svc.reconcile([], None, None, None) == []
    assert svc.reconcile([el("1", 5)], None, 1.0, None) == []
    assert svc.reconcile([el("1", 5)], 5.0, None, 9.0) == []
    assert svc.reconcile([el("1", 5)], None, None, 9.0) == []


# --- header field sources ---


def test_field_sources_resolved_and_keys_camel():
    lt = lay(mkrow("v", "Acme Corp", y0=0.05, y1=0.07), mkrow("q", "Quote Q1", y0=0.30, y1=0.32))
    q = quote([], field_source_rows=fsr(vendor_name=["v"], quote_number=["q"]))
    data, _ = svc.to_extracted(q, lt, GROUPS)
    assert set(data.field_sources) == {"vendorName", "quoteNumber"}
    assert data.field_sources["vendorName"] == [lt.rows[0].box]
    wire = data.model_dump(by_alias=True)
    assert "fieldSources" in wire and "field_sources" not in wire


def test_field_sources_unknown_rows_dropped_and_empty_omitted():
    lt = lay(mkrow("v", "Acme Corp"))
    q = quote([], vendor_name="Zed Ltd", department="Acme", field_source_rows=fsr(vendor_name=["nope"], department=["v"]))
    data, _ = svc.to_extracted(q, lt, GROUPS)
    assert "vendorName" not in data.field_sources
    assert "vatId" not in data.field_sources
    assert "department" in data.field_sources


def test_field_sources_omitted_when_value_null():
    lt = lay(mkrow("v", "EIN: 1"))
    q = quote([], vendor_tax_id=None, valid_until=None,
              field_source_rows=fsr(vendor_tax_id=["v"], valid_until=["v"]))
    data, _ = svc.to_extracted(q, lt, GROUPS)
    assert data.field_sources == {}


# --- word-level header highlights on the real quotes (fake LLM, no network) ---

APPLE = "Quote_1__Lio_Technologies_Inc__1x_MBA___2214703918.pdf"
VERDEFORM = "QT-OF2312380-Cust-57692.pdf"
HARTWELL = "Quote_Hartwell_Cleveland_ACME_Grinding_Machine.pdf"
GREENMANTLE = "QT-4120-Cust-14918.pdf"


def real(quote_bytes, name):
    from app.services.pdf_layout import extract_layout
    return extract_layout(quote_bytes(name))


def cite(layout, needle):
    """What a thorough model cites: every row containing the value."""
    return [r.id for r in layout.rows if needle in r.text]


def covered(layout, boxes):
    """Words of the layout that the boxes cover (centre inside the box), in order."""
    out = []
    for b in boxes:
        for r in layout.rows:
            if r.page != b.page or not (r.box.y0 < b.y1 and r.box.y1 > b.y0):
                continue
            out += [w.text for w in r.words if b.x0 <= (w.x0 + w.x1) / 2 <= b.x1]
    return out


def sources(layout, **kw):
    return svc.resolve_field_sources(quote([], **kw), layout)


def overlap(a, b):
    return a.page == b.page and a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def test_apple_vendor_box_covers_only_apple(quote_bytes):
    lt = real(quote_bytes, APPLE)
    fs = sources(lt, vendor_name="Apple", field_source_rows=fsr(vendor_name=cite(lt, "Apple Business Team")))
    assert len(fs["vendorName"]) == 1
    assert covered(lt, fs["vendorName"]) == ["Apple"]


def test_apple_quote_number_and_valid_until_do_not_overlap(quote_bytes):
    lt = real(quote_bytes, APPLE)
    row = cite(lt, "Wednesday, Mar 20, 2024")
    fs = sources(lt, quote_number="2214703918", valid_until="2024-03-20",
                 field_source_rows=fsr(quote_number=row, valid_until=row))
    qn, vu = fs["quoteNumber"][0], fs["validUntil"][0]
    assert not overlap(qn, vu)
    assert covered(lt, [qn]) == ["2214703918"]
    assert covered(lt, [vu])[-3:] == ["Mar", "20,", "2024"]
    assert "Tuesday," not in covered(lt, [vu])


def test_greenmantle_vendor_has_exactly_one_box_on_page_1(quote_bytes):
    lt = real(quote_bytes, GREENMANTLE)
    ids = cite(lt, "Greenmantle Moss Studio, LLC")
    assert len(ids) >= 3  # letterhead + footers
    fs = sources(lt, vendor_name="Greenmantle Moss Studio, LLC",
                 field_source_rows=fsr(vendor_name=ids))
    assert len(fs["vendorName"]) == 1 and fs["vendorName"][0].page == 1
    assert covered(lt, fs["vendorName"]) == ["Greenmantle", "Moss", "Studio,", "LLC"]


def test_greenmantle_tax_id_boxed_once_not_on_every_page(quote_bytes):
    lt = real(quote_bytes, GREENMANTLE)
    fs = sources(lt, vendor_tax_id="85-3252405",
                 field_source_rows=fsr(vendor_tax_id=cite(lt, "EIN: 85-3252405")))
    assert len(fs["vatId"]) == 1
    assert "85-3252405" in covered(lt, fs["vatId"])
    assert "Fax:" not in covered(lt, fs["vatId"])


def test_hartwell_vendor_single_box(quote_bytes):
    lt = real(quote_bytes, HARTWELL)
    fs = sources(lt, vendor_name="Hartwell Industrial Tools, Inc.",
                 field_source_rows=fsr(vendor_name=cite(lt, "Hartwell Industrial Tools, Inc.")))
    assert len(fs["vendorName"]) == 1 and fs["vendorName"][0].page == 1


def test_hollis_vat_box_covers_the_digits_and_label(quote_bytes):
    from tests.test_pdf_layout import HOLLIS
    lt = real(quote_bytes, HOLLIS)
    fs = sources(lt, vendor_tax_id="94-1985704",
                 field_source_rows=fsr(vendor_tax_id=cite(lt, "EIN: 94 1985704")))
    assert len(fs["vatId"]) == 1
    assert covered(lt, fs["vatId"]) == ["EIN:", "94", "1985704"]


def test_wrong_cited_row_falls_back_to_whole_layout(quote_bytes):
    from tests.test_pdf_layout import HOLLIS
    lt = real(quote_bytes, HOLLIS)
    wrong = [lt.rows[0].id]
    fs = sources(lt, vendor_tax_id="94 1985704", field_source_rows=fsr(vendor_tax_id=wrong))
    assert covered(lt, fs["vatId"])[-2:] == ["94", "1985704"]


def test_unmatched_value_keeps_only_a_gap_segment_or_nothing(quote_bytes):
    from tests.test_pdf_layout import HOLLIS
    lt = real(quote_bytes, HOLLIS)
    # derived value ('four weeks' after 12/12/2023): no printed form on the quote
    row = next(r for r in lt.rows if "prices in this quote are valid" in r.text)
    fs = sources(lt, valid_until="2024-01-09", field_source_rows=fsr(valid_until=[row.id]))
    assert "validUntil" not in fs
    # token fallback: a multi-column row is cut to the segment holding the value
    lt = real(quote_bytes, APPLE)
    row = cite(lt, "Apple Business Team")
    fs = sources(lt, vendor_name="Apple Inc.", field_source_rows=fsr(vendor_name=row))
    assert covered(lt, fs["vendorName"]) == ["Apple", "Business", "Team"]


# --- matching logic on synthetic rows (no PDF) ---


def wrow(rid, text, page=1, y0=0.1, gap=0.01):
    """A row whose words are laid out left to right, one char = 0.01 wide."""
    words, x = [], 0.1
    for t in text.split():
        words.append(Word(text=t, x0=x, x1=x + 0.01 * len(t)))
        x += 0.01 * len(t) + gap
    return mkrow(rid, text, page=page, y0=y0, y1=y0 + 0.02, words=words, cw=0.01)


def synth(*rows, **kw):
    lt = lay(*rows)
    ids = kw.pop("ids", [r.id for r in rows])
    attr = kw.pop("attr")
    q = quote([], **kw, field_source_rows=fsr(**{attr: ids}))
    return lt, svc.resolve_field_sources(q, lt)


def words_in(lt, boxes):
    return covered(lt, boxes)


@pytest.mark.parametrize("printed", ["1/27/24", "01/27/2024", "Jan 27, 2024", "January 27, 2024"])
def test_valid_until_printed_forms(printed):
    lt, fs = synth(wrow("a", f"Date: 11/28/23 Valid until: {printed}"), attr="valid_until",
                   valid_until="2024-01-27")
    assert words_in(lt, fs["validUntil"]) == ["Valid", "until:", *printed.split()]


def test_label_fused_to_value_is_split():
    lt, fs = synth(wrow("a", "Customer number:18000010931219"), attr="department",
                   department="18000010931219")
    (b,) = fs["department"]
    w = lt.rows[0].words[1]
    assert b.x1 == pytest.approx(w.x1) and w.x0 < b.x0 < w.x1


@pytest.mark.parametrize("attr,key,value,row,neighbour", [
    ("quote_number", "quoteNumber", "4120", "Total $14,120.00", "4120"),
    ("vendor_name", "vendorName", "Apple", "Pineapple Juice", "Apple Inc"),
    ("valid_until", "validUntil", "2024-01-05", "Valid 11/5/2024", "Valid 1/5/2024"),
    ("vendor_tax_id", "vatId", "415 5550171", "Phone 14155550171", "Phone 415 5550171"),
])
def test_matches_never_inside_a_longer_token(attr, key, value, row, neighbour):
    lt, fs = synth(wrow("a", row), attr=attr, **{attr: value})
    assert key not in fs
    lt, fs = synth(wrow("a", row, y0=0.1), wrow("b", neighbour, y0=0.3), attr=attr, **{attr: value})
    assert fs[key][0].y0 == pytest.approx(0.3)


def test_exact_in_later_cited_row_beats_earlier_fused_hit():
    early = wrow("a", "Customer number:4120", y0=0.1)
    late = wrow("b", "Quote 4120", y0=0.4)
    lt, fs = synth(early, late, attr="quote_number", quote_number="4120")
    assert fs["quoteNumber"][0].y0 == pytest.approx(0.4)


def test_repeats_deduped_first_in_reading_order_wins():
    rows = [wrow("f2", "Acme GmbH", page=2, y0=0.9), wrow("f1", "Acme GmbH", y0=0.95),
            wrow("h", "Acme GmbH", y0=0.05)]
    lt, fs = synth(*sorted(rows, key=lambda r: r.id), attr="vendor_name", vendor_name="Acme GmbH",
                   ids=["f2", "f1", "h"])
    # layout order is f1, f2, h; cited order is irrelevant, the first layout row wins
    assert len(fs["vendorName"]) == 1 and fs["vendorName"][0].page == 1
    assert fs["vendorName"][0].y0 == pytest.approx(0.95)


# --- warning source highlights ------------------------------------------------


def msgs(details):
    return [d.message for d in details]


def prow(rid, y, *pairs, page=1):
    """Row with explicit word positions: pairs of (text, x0, x1)."""
    words = [Word(text=t, x0=a, x1=b) for t, a, b in pairs]
    return mkrow(rid, " ".join(t for t, _, _ in pairs), page=page, y0=y, y1=y + 0.02,
                 words=words)


def test_unpriced_charges_each_get_word_tight_boxes():
    lt = lay(
        prow("f", 0.40, ("Fuel", 0.10, 0.14), ("surcharge", 0.15, 0.23), ("TBD", 0.80, 0.85)),
        prow("r", 0.45, ("Rush", 0.10, 0.14), ("surcharge:", 0.15, 0.24),
             ("25%", 0.25, 0.28), ("of", 0.29, 0.31), ("freight", 0.32, 0.38)),
    )
    q = quote([line(position="1", quantity=0, unit_price=0, total_price=0)], unpriced_charges=[
        LlmUnpricedCharge(text="Fuel surcharge", source_rows=["f"]),
        LlmUnpricedCharge(text="Rush surcharge: 25% of freight, if rush", source_rows=["r"]),
    ])
    _, details = svc.to_extracted(q, lt, GROUPS)
    assert msgs(details) == [
        "Quote mentions a charge without an amount: Fuel surcharge",
        "Quote mentions a charge without an amount: Rush surcharge: 25% of freight, if rush",
    ]
    fuel, rush = details
    assert len(fuel.sources) == 1 and len(rush.sources) >= 1
    assert covered(lt, fuel.sources) == ["Fuel", "surcharge"]
    assert fuel.sources[0].x1 < 0.5  # not the TBD column
    assert covered(lt, rush.sources)[:2] == ["Rush", "surcharge:"]


def test_unpriced_charge_without_citation_has_no_sources():
    q = quote([line(position="1")], unpriced_charges=[
        LlmUnpricedCharge(text="Handling", source_rows=["nope"])])
    _, details = svc.to_extracted(q, lay(), GROUPS)
    assert details[0].sources == []


def _apple_like():
    lt = lay(
        prow("l1", 0.30, ("MacBook", 0.10, 0.18), ("Air", 0.19, 0.22), ("1,400.00", 0.80, 0.88)),
        prow("n", 0.60, ("Subtotal", 0.50, 0.58), ("$1,400.00", 0.80, 0.90)),
        prow("t", 0.64, ("Tax", 0.50, 0.54), ("$121.53", 0.82, 0.90)),
        prow("g", 0.68, ("Total", 0.50, 0.55), ("$1,534.59", 0.80, 0.90)),
    )
    q = quote([line(position="1", unit_price=1400, total_price=1400, source_rows=["l1"])],
              net_total=1400.0, tax_total=121.53, grand_total=1534.59,
              total_source_rows=tsr(net=["n"], tax=["t"], grand=["g"]))
    return lt, q


def test_grand_total_warning_boxes_grand_and_tax_amounts():
    lt, q = _apple_like()
    _, details = svc.to_extracted(q, lt, GROUPS)
    assert msgs(details) == [
        "Line items $1,400.00 plus tax $121.53 come to $1,521.53 but the quote's grand total is $1,534.59"
    ]
    boxes = details[0].sources
    assert covered(lt, boxes) == ["$1,534.59", "$121.53"] or covered(lt, boxes) == ["$121.53", "$1,534.59"]
    assert all(b.x1 - b.x0 < 0.2 for b in boxes)  # amounts, never full rows


def test_net_warning_boxes_net_row():
    lt = lay(prow("n", 0.60, ("Net", 0.50, 0.54), ("110.00", 0.80, 0.88)),
             prow("l", 0.30, ("Panel", 0.10, 0.15), ("100.00", 0.80, 0.88)))
    q = quote([line(position="1", unit_price=100, total_price=100, source_rows=["l"])],
              net_total=110.0,
              total_source_rows=tsr(net=["n"]))
    _, details = svc.to_extracted(q, lt, GROUPS)
    assert len(details) == 1 and "net total" in details[0].message
    assert covered(lt, details[0].sources) == ["110.00"]


def test_line_warnings_carry_the_lines_sources():
    lt = lay(prow("a", 0.3, ("Panel", 0.1, 0.15), ("999.00", 0.8, 0.88)))
    l = line(position="2", quantity=1, unit_price=715.26, total_price=715.26,
             source_rows=["a"])
    _, details = svc.to_extracted(quote([l]), lt, GROUPS)
    d = [x for x in details if "Couldn't find" in x.message][0]
    assert d.sources and d.sources[0].y0 == pytest.approx(0.3)
    l2 = line(position="3", quantity=3, unit_price=10.0, total_price=45.0, source_rows=["a"])
    _, d2 = svc.to_extracted(quote([l2]), lt, GROUPS)
    assert [x for x in d2 if "quantity" in x.message][0].sources
    l3 = line(position="4", kind="surcharge", parent_position="9", source_rows=["a"])
    _, d3 = svc.to_extracted(quote([l3]), lt, GROUPS)
    assert [x for x in d3 if "parent position" in x.message][0].sources


def test_verdeform_unpriced_charges_boxed(quote_bytes):
    lt = real(quote_bytes, VERDEFORM)
    fuel = cite(lt, "Fuel surcharge")
    rush = cite(lt, "Rush surcharge")
    assert fuel and rush
    q = quote([], unpriced_charges=[
        LlmUnpricedCharge(text="Fuel surcharge", source_rows=fuel),
        LlmUnpricedCharge(text="Rush surcharge", source_rows=rush)])
    _, details = svc.to_extracted(q, lt, GROUPS)
    assert len(details) == 2 and all(d.sources for d in details)


# --- parent rule (I1) ---


def _kinds(data):
    return {ln.key: (ln.kind, ln.parent_key) for ln in data.order_lines}


@pytest.mark.parametrize(
    "child_kind,parent_kind",
    [("optional", "alternative"), ("optional", "optional"),
     ("discount", "discount"), ("fee", "shipping"), ("alternative", "optional")],
)
def test_illegal_parent_dropped_with_warning(child_kind, parent_kind):
    q = quote([
        line(position="1"),
        line(position="2", kind=parent_kind, parent_position="1"),
        line(position="3", kind=child_kind, parent_position="2"),
    ])
    data, warnings = conv(q)
    kinds = _kinds(data)
    assert kinds["3"][1] is None
    assert any(w.message.startswith("Line 3:") and "not linked" in w.message
               for w in warnings)


def test_illegal_link_warning_carries_line_sources():
    layout = PdfLayout(
        rows=[PdfRow(id="r1", page=1, text="x", box=Box(page=1, x0=0.1, y0=0.1, x1=0.9, y1=0.12), words=[])],
        page_count=1,
    )
    q = quote([
        line(position="1"),
        line(position="2", kind="alternative", parent_position="1"),
        line(position="3", kind="optional", parent_position="2", source_rows=["r1"]),
    ])
    _, warnings = svc.to_extracted(q, layout, GROUPS)
    w = next(w for w in warnings if "not linked" in w.message)
    assert w.sources
