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


def test_sdk_exception_readable(monkeypatch, db, rows_layout):
    exc = openai.APIConnectionError(request=SimpleNamespace())
    use(monkeypatch, FakeClient(exc=exc))
    r = svc.extract_from_pdf(b"%PDF-", db)
    assert not r.success and r.error and "OpenAI" in r.error


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
