"""Backfill of source highlights for requests extracted before a field had one.

The extraction call is faked: only the merge rules are under test.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backfill_field_sources import backfill
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import ProcurementRequest
from app.schemas.extraction import ExtractedVendorData, ExtractionResponse
from app.seed import DEMO_PASSWORD, seed

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(engine)
with Session() as _s:
    seed(_s)

OLD = {"page": 1, "x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.12}
NEW = {"page": 1, "x0": 0.5, "y0": 0.5, "x1": 0.6, "y1": 0.52}
KEYS = ["title", "requestorName", "department", "vendorName", "vatId", "quoteNumber",
        "validUntil", "totalCost"]


def _get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    yield TestClient(app)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest.fixture
def auth(client):
    r = client.post(
        "/auth/signin", json={"email": "requester@acme.com", "password": DEMO_PASSWORD}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create(client, auth, with_document=True):
    group = client.get("/commodity-groups", headers=auth).json()[0]["id"]
    body = {
        "requestorName": "Rene", "titleShortDescription": "Chairs", "vendorName": "Office Co",
        "vatId": "12-3456789", "quoteNumber": "Q-1", "validUntil": "2024-01-09",
        "commodityGroupId": group, "fieldSources": {"vendorName": [OLD]},
        "orderLines": [{"positionDescription": "Chair", "unitPrice": 3000, "amount": 1,
                        "unit": "pc", "totalPrice": 3000, "key": "1"}],
    }
    r = client.post("/requests", json=body, headers=auth)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    if with_document:
        r = client.put(f"/requests/{rid}/document", headers=auth,
                       files={"file": ("q.pdf", b"%PDF-1.4 stub", "application/pdf")})
        assert r.status_code == 204, r.text
    return rid


class FakeExtract:
    """Re-extraction that finds every field, with some values differing."""

    def __init__(self, success=True):
        self.calls = 0
        self.success = success

    def __call__(self, pdf_bytes, db, organization_id):
        self.calls += 1
        if not self.success:
            return ExtractionResponse(success=False, error="no key")
        return ExtractionResponse(success=True, data=ExtractedVendorData(
            title="Office chairs", requestor_name="Vladimir Keil", vendor_name="OFFICE CO",
            vat_id="12 3456789", quote_number="Q-2", valid_until="2024-01-09",
            total_cost=2900.0, field_sources={k: [NEW] for k in KEYS},
        ))


def _sources(rid):
    with Session() as db:
        return db.get(ProcurementRequest, rid).field_sources


@pytest.fixture(autouse=True)
def _clean():
    yield
    with Session() as db:
        for r in db.query(ProcurementRequest).all():
            db.delete(r)
        db.commit()


def test_adds_missing_highlights_and_keeps_stored_ones(client, auth):
    import uuid
    rid = uuid.UUID(_create(client, auth))
    fake = FakeExtract()
    with Session() as db:
        assert backfill(db, extract=fake) == 1
    fs = _sources(rid)
    assert fs["vendorName"] == [OLD]                      # stored box kept
    assert fs["vatId"] == [NEW] and fs["validUntil"] == [NEW]  # same value, new box
    assert fs["title"] == [NEW] and fs["totalCost"] == [NEW]   # always the quote's source
    assert "quoteNumber" not in fs                        # Q-2 is not the stored Q-1
    assert "requestorName" not in fs                      # typed name, not the quote's
    assert "department" not in fs                         # nothing stored


def test_second_run_changes_nothing(client, auth):
    import uuid
    rid = uuid.UUID(_create(client, auth))
    with Session() as db:
        backfill(db, extract=FakeExtract())
    first = _sources(rid)
    with Session() as db:
        assert backfill(db, extract=FakeExtract()) == 0
    assert _sources(rid) == first


def test_requests_without_a_document_or_a_failed_extraction_are_skipped(client, auth):
    import uuid
    no_doc = uuid.UUID(_create(client, auth, with_document=False))
    with_doc = uuid.UUID(_create(client, auth))
    fake = FakeExtract(success=False)
    with Session() as db:
        assert backfill(db, extract=fake) == 0
    assert fake.calls == 1
    assert _sources(no_doc) == {"vendorName": [OLD]}
    assert _sources(with_doc) == {"vendorName": [OLD]}


def test_complete_requests_need_no_model_call(client, auth):
    import uuid
    rid = uuid.UUID(_create(client, auth))
    with Session() as db:
        r = db.get(ProcurementRequest, rid)
        r.requestor_name, r.quote_number = "Vladimir Keil", "Q-2"
        db.commit()
    with Session() as db:
        backfill(db, extract=FakeExtract())
    fake = FakeExtract()
    with Session() as db:
        assert backfill(db, extract=fake) == 0
    assert fake.calls == 0
