"""Rich PDF extraction: LLM output -> structured lines. No network: the OpenAI
client is replaced by a fake that records calls and returns canned quotes."""
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import CommodityGroup
from app.schemas.extraction import LlmLine, LlmQuote
from app.services import extraction as svc
from app.services.pdf_layout import Box, PdfLayout, PdfRow

NO_TEXT = "No text found in this PDF — it may be a scan"


def line(**kw) -> LlmLine:
    base = dict(
        position=None, vendor_item_number=None, description="Thing", kind="item",
        parent_position=None, quantity=1, unit="ea", list_unit_price=None,
        adjustment_percent=None, unit_price=None, total_price=10.0, source_rows=[],
    )
    base.update(kw)
    return LlmLine(**base)


def quote(lines, **kw) -> LlmQuote:
    base = dict(
        title="Quote", vendor_name="Acme", vendor_tax_id="12-3456789", department="IT",
        quote_number="Q1", valid_until="2026-12-31", currency="USD", lines=lines,
        net_total=None, tax_total=None, grand_total=None, unpriced_charges=[],
        commodity_group_id=None,
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
    assert any("9" in w for w in warnings)


def test_item_parent_always_null():
    data, _ = conv(quote([line(position="1"), line(position="2", parent_position="1")]))
    assert data.order_lines[1].parent_key is None


def test_duplicate_positions_get_unique_keys():
    data, _ = conv(quote([line(position="1"), line(position="1"), line(position="1")]))
    assert [l.key for l in data.order_lines] == ["1", "1#2", "1#3"]


def test_line_discount_fields():
    q = quote([line(position="1", quantity=2, list_unit_price=894.08,
                    adjustment_percent=-20, unit_price=None, total_price=None)])
    l = conv(q)[0].order_lines[0]
    assert (l.list_unit_price, l.adjustment_percent, l.unit_price) == (894.08, -20, 715.26)
    assert l.total_price == 1430.52 and l.amount == 2


def test_discount_sign_normalised():
    q = quote([line(position="1"), line(kind="discount", unit_price=None, quantity=None,
                                       total_price=50.0)])
    assert conv(q)[0].order_lines[1].total_price == -50.0


def test_null_tax_id_success():
    data, _ = conv(quote([line(position="1")], vendor_tax_id=None))
    assert data.vat_id == ""
    resp = svc._finish(data, [])
    assert resp.success and "Tax ID" in resp.missing_fields


def test_unpriced_charges_become_warnings():
    _, w = conv(quote([line(position="1")], unpriced_charges=["fuel surcharge"]))
    assert "Quote mentions a charge without an amount: fuel surcharge" in w


def test_total_cost_is_grand_total():
    q = quote([line(position="1", total_price=100),
               line(position="2", kind="optional", total_price=900)],
              tax_total=8.0, grand_total=108.0)
    data, _ = conv(q)
    assert data.total_cost == 108.0 and data.net_total == 100 and data.tax_total == 8.0
    q2 = quote([line(position="1", total_price=100)], tax_total=8.0)
    d2, _ = conv(q2)
    assert d2.total_cost == 108.0 and d2.net_total == 100


def test_inactive_parent_excludes_adjustment():
    q = quote([
        line(position="1", total_price=100),
        line(position="2", kind="optional", total_price=50),
        line(kind="discount", parent_position="2", total_price=-5),
    ])
    assert conv(q)[0].net_total == 100


def test_commodity_name_from_db():
    d, _ = conv(quote([line(position="1")], commodity_group_id=15))
    assert (d.commodity_group_id, d.commodity_group_name) == (15, "Office Equipment")
    d, _ = conv(quote([line(position="1")], commodity_group_id=99))
    assert d.commodity_group_id is None and d.commodity_group_name is None


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


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(CommodityGroup(id=15, category="Facilities", name="Office Equipment"))
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
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert r.success and r.data.commodity_group_name == "Office Equipment"
    kw = fake.calls[0]
    assert kw["response_format"] is LlmQuote and kw["max_completion_tokens"] == 8000
    assert kw["temperature"] == 0.0
    assert "[p1r1] Widget" in kw["messages"][1]["content"]
    assert "15 | Facilities | Office Equipment" in kw["messages"][1]["content"]


def test_temperature_omitted_when_none(monkeypatch, db, rows_layout):
    fake = use(monkeypatch, FakeClient(completion(quote([line(position="1")]))))
    monkeypatch.setattr(svc.settings, "openai_temperature", None)
    svc.extract_from_pdf(b"%PDF-", db)
    assert "temperature" not in fake.calls[0]


def test_settings_defaults_omit_temperature(monkeypatch, db, rows_layout):
    from app.core.config import Settings

    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TEMPERATURE", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.openai_model == "gpt-6.1-sol"
    assert defaults.openai_temperature is None
    monkeypatch.setattr(svc.settings, "openai_model", defaults.openai_model)
    monkeypatch.setattr(svc.settings, "openai_temperature", defaults.openai_temperature)
    fake = use(monkeypatch, FakeClient(completion(quote([line(position="1")]))))
    svc.extract_from_pdf(b"%PDF-", db)
    assert fake.calls[0]["model"] == "gpt-6.1-sol"
    assert "temperature" not in fake.calls[0]


def test_scan_pdf_no_model_call(monkeypatch, db):
    monkeypatch.setattr(svc, "extract_layout", lambda b: PdfLayout(rows=[], page_count=2))
    fake = use(monkeypatch, FakeClient())
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and r.error == NO_TEXT and fake.calls == []


def test_length_and_refusal_errors(monkeypatch, db, rows_layout):
    use(monkeypatch, FakeClient(completion(finish="length")))
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and "too long" in r.error
    use(monkeypatch, FakeClient(completion(refusal="I can't help")))
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and "I can't help" in r.error
    use(monkeypatch, FakeClient(completion(parsed=None)))
    assert not svc.extract_from_pdf(b"%PDF-", db).success


def test_sdk_length_error_readable(monkeypatch, db, rows_layout):
    exc = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    use(monkeypatch, FakeClient(exc=exc))
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and "too long" in r.error


READABLE_FAILURE = "Extraction failed \u2014 please try again or fill in the form manually"


def test_sdk_exception_readable(monkeypatch, db, rows_layout, caplog):
    exc = openai.APIConnectionError(request=SimpleNamespace())
    use(monkeypatch, FakeClient(exc=exc))
    with caplog.at_level("ERROR", logger=svc.logger.name):
        r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and r.error == READABLE_FAILURE
    assert any(rec.exc_info and rec.exc_info[1] is exc for rec in caplog.records)


def test_generic_exception_readable(monkeypatch, db, rows_layout, caplog):
    exc = RuntimeError("Error code: 400 - secret internals")
    use(monkeypatch, FakeClient(exc=exc))
    with caplog.at_level("ERROR", logger=svc.logger.name):
        r = svc.extract_from_pdf(b"%PDF-", db)
    assert r.error == READABLE_FAILURE and "400" not in r.error
    assert any(rec.exc_info and rec.exc_info[1] is exc for rec in caplog.records)


def test_missing_api_key(monkeypatch, db, rows_layout):
    monkeypatch.setattr(svc.settings, "openai_api_key", None)
    fake = use(monkeypatch, FakeClient())
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and "OPENAI_API_KEY" in r.error and fake.calls == []


# --- endpoint ------------------------------------------------------------------


def test_endpoint_requires_pdf_bytes(monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: object())
    c = TestClient(app)
    r = c.post("/extraction/pdf", files={"file": ("a.pdf", b"hello", "application/pdf")})
    assert r.status_code == 400


def test_endpoint_unauthenticated():
    r = TestClient(app).post("/extraction/pdf", files={"file": ("a.pdf", b"%PDF-", "application/pdf")})
    assert r.status_code == 401


def test_endpoint_happy_path(monkeypatch, db, rows_layout):
    use(monkeypatch, FakeClient(completion(quote([line(position="1")]))))
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: object())
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


def test_source_text_is_longest_segment_with_letters():
    words = [Word(text="1.1", x0=0.1, x1=0.13), Word(text="Moss", x0=0.2, x1=0.24),
             Word(text="panel", x0=0.25, x1=0.3), Word(text="715.26", x0=0.8, x1=0.9)]
    row = mkrow("a", "1.1 Moss panel 715.26", words=words, cw=0.01)
    boxes, text, _ = svc.resolve_sources(line(source_rows=["a"]), lay(row))
    assert text == "Moss panel" and len(boxes) == 1
    # first valid row without a 3-letter segment -> None
    num = mkrow("n", "1 2", words=[Word(text="1", x0=0.1, x1=0.11)])
    assert svc.resolve_sources(line(source_rows=["n"]), lay(num))[1] is None


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


def test_price_verified_when_total_in_rows():
    l = line(position="1", quantity=2, list_unit_price=894.08, adjustment_percent=-20,
             total_price=None, source_rows=["a"])
    assert svc.resolve_sources(l, lay(mkrow("a", "Panel 2 pcs 715.26")))[2] is True  # unit price
    assert svc.resolve_sources(l, lay(mkrow("a", "Panel 2 pcs 1,430.52")))[2] is True  # total
    assert svc.resolve_sources(l, lay(mkrow("a", "Panel 2 pcs 999.00")))[2] is False


def test_unverified_price_warns():
    l = line(position="2", quantity=1, unit_price=715.26, total_price=715.26,
             source_rows=["a"])
    _, w = svc.to_extracted(quote([l]), lay(mkrow("a", "Panel 999.00")), GROUPS)
    assert "Couldn't find line 2's price ($715.26) in the document" in w
    l2 = line(position="2", quantity=1, unit_price=715.26, total_price=715.26,
              source_rows=["a"])
    _, w2 = svc.to_extracted(quote([l2]), lay(mkrow("a", "Panel 715.26")), GROUPS)
    assert not any("Couldn't find" in x for x in w2)
    z = line(position="3", quantity=0, unit_price=0, total_price=0)
    _, w3 = svc.to_extracted(quote([z]), lay(), GROUPS)
    assert not any("Couldn't find" in x for x in w3)


def el(key, total, kind="item", parent=None):
    return ExtractedLine(key=key, parent_key=parent, kind=kind, position_description="",
                         unit_price=total, amount=1, unit="", total_price=total)


def test_lines_vs_net_warns():
    lines = [el("1", 100), el("2", 50), el("3", 900, kind="optional"),
             el("4", -5, kind="discount", parent="3")]
    w = svc.reconcile(lines, 160.0, None, None)
    assert w == ["Line items add up to $150.00 but the quote's net total is $160.00"]
    assert svc.reconcile(lines, 150.0, None, None) == []


def test_total_only_shipping_line_normalised():
    ship = line(position="2", kind="shipping", quantity=None, unit=None,
                unit_price=None, total_price=215.0)
    data, w = conv(quote([ship]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, 215.0, 215.0)
    assert not any("quantity x unit price" in x for x in w)


def test_zero_qty_zero_price_with_total_normalised():
    fee = line(position="3", kind="fee", quantity=0, unit_price=0, total_price=215.0)
    data, _ = conv(quote([fee]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, 215.0, 215.0)


def test_list_price_fallback_when_unit_price_null():
    l = line(position="1", quantity=3, unit_price=None, list_unit_price=10.0,
             adjustment_percent=None, total_price=30.0)
    data, _ = conv(quote([l]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (3, 10.0, 30.0)


def test_prompt_department_is_buyers_only():
    prompt = svc._build_pdf_prompt("[r1] x", "15 | Facilities | Office Equipment")
    assert "BUYER's department only" in prompt
    assert "Apple Business Team" in prompt


def test_apple_fee_missing_from_net_warns():
    lines = [el("1", 1409.06), el("2", 4.00, kind="fee")]
    w = svc.reconcile(lines, 1409.06, 121.53, 1534.59)
    assert "Line items add up to $1,413.06 but the quote's net total is $1,409.06" in w
    # net + tax (1,530.59) also disagrees with the 1,534.59 grand total
    assert len(w) == 2 and "grand total is $1,534.59" in w[1]


def test_totals_tolerance_scales_with_active_lines():
    assert svc._totals_tol(0) == svc._totals_tol(1) == pytest.approx(0.01)
    assert svc._totals_tol(5) == pytest.approx(0.05)


@pytest.mark.parametrize("sign", [1, -1])
def test_discount_sign_normalised_unit_and_total(sign):
    d = line(position="2", kind="discount", quantity=1, unit_price=sign * 10.0,
             total_price=sign * 10.0)
    ln = conv(quote([d]))[0].order_lines[0]
    assert (ln.unit_price, ln.total_price) == (-10.0, -10.0)


def test_total_only_discount_gets_negative_unit_price():
    d = line(position="2", kind="discount", quantity=None, unit_price=None,
             total_price=10.0)
    ln = conv(quote([d]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, -10.0, -10.0)


def test_quantity_kept_when_unit_price_missing():
    l = line(position="1", quantity=4, unit_price=None, total_price=100.0)
    data, w = conv(quote([l]))
    ln = data.order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (4, 25.0, 100.0)
    assert not any("quantity x unit price" in x for x in w)


def test_inexact_split_keeps_stated_total():
    l = line(position="1", quantity=3, unit_price=None, total_price=100.0)
    ln = conv(quote([l]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, 100.0, 100.0)


def test_inexact_split_discount_keeps_stated_total():
    l = line(position="1", kind="discount", quantity=3, unit_price=None,
             total_price=-100.0)
    ln = conv(quote([l]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, -100.0, -100.0)


def test_total_only_negative_discount():
    d = line(position="2", kind="discount", quantity=None, unit_price=None,
             total_price=-10.0)
    ln = conv(quote([d]))[0].order_lines[0]
    assert (ln.amount, ln.unit_price, ln.total_price) == (1, -10.0, -10.0)


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


def test_env_temperature_none_gives_none(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("OPENAI_TEMPERATURE", "none")
    assert Settings(_env_file=None).openai_temperature is None


def test_prompt_short_line_descriptions():
    prompt = svc._build_pdf_prompt("[r1] x", "15 | Facilities | Office Equipment")
    assert "short name as printed" in prompt
    assert "120 characters" in prompt
    assert "Moss panel" in prompt


def test_line_math_warns():
    l = line(position="1", quantity=3, unit_price=10.0, total_price=45.0)
    _, w = svc.to_extracted(quote([l]), lay(), GROUPS)
    assert any("line 1" in x.lower() and "$45.00" in x and "$30.00" in x for x in w)
    ok = line(position="1", quantity=3, unit_price=10.0, total_price=30.0)
    _, w2 = svc.to_extracted(quote([ok]), lay(), GROUPS)
    assert not any("quantity" in x for x in w2)


def test_net_plus_tax_vs_grand_warns():
    w = svc.reconcile([el("1", 100)], 100.0, 8.0, 110.0)
    assert w == ["Net total $100.00 plus tax $8.00 is $108.00 but the quote's grand total is $110.00"]
    assert svc.reconcile([el("1", 100)], 100.0, 8.0, 108.0) == []


def test_none_totals_no_crash():
    assert svc.reconcile([], None, None, None) == []
    assert svc.reconcile([el("1", 5)], None, 1.0, None) == []
    assert svc.reconcile([el("1", 5)], 5.0, None, 9.0) == []
    assert svc.reconcile([el("1", 5)], None, None, 9.0) == []
