"""Rich order lines: persistence, server totals, link validation, open-only
editing and approval reset. TestClient over in-memory SQLite."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import RequestActivity
from app.seed import seed

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)


@event.listens_for(engine, "connect")
def _fk_on(dbapi_connection, _record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(engine)
with Session() as _s:
    seed(_s)


def _get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    # test_smoke installs its own override at import time; restore it after us.
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
        "/auth/signup",
        json={"email": f"u{uuid.uuid4().hex[:8]}@example.com", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    slug = f"org-{uuid.uuid4().hex[:8]}"
    r = client.post("/organizations", json={"name": slug, "slug": slug}, headers=headers)
    assert r.status_code == 201, r.text
    return headers


def line(key, **kw):
    base = {
        "positionDescription": f"Line {key}",
        "unitPrice": 100.0,
        "amount": 1,
        "unit": "pc",
        "totalPrice": 100.0,
        "key": key,
    }
    base.update(kw)
    return base


def payload(lines, **kw):
    base = {
        "requestorName": "Rita",
        "titleShortDescription": "Quote",
        "vendorName": "Acme",
        "orderLines": lines,
    }
    base.update(kw)
    return base


def org_group(client, auth):
    """One of the caller's org's commodity groups (groups are per org)."""
    return client.get("/commodity-groups", headers=auth).json()[0]["id"]


def create(client, auth, lines, **kw):
    kw.setdefault("commodityGroupId", org_group(client, auth))
    return client.post("/requests", json=payload(lines, **kw), headers=auth)


def put(client, auth, rid, lines, **kw):
    kw.setdefault("commodityGroupId", org_group(client, auth))
    body = payload(lines, **kw)
    body["title"] = body.pop("titleShortDescription")
    return client.put(f"/requests/{rid}", json=body, headers=auth)


def actions(rid):
    with Session() as s:
        return [
            (a.action, a.summary)
            for a in s.scalars(
                select(RequestActivity)
                .where(RequestActivity.request_id == uuid.UUID(rid))
                .order_by(RequestActivity.created_at)
            )
        ]


BOX = {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.25}


def test_create_round_trips_rich_lines(client, auth):
    lines = [
        line(
            "3",
            position="3",
            vendorItemNumber="AB-1",
            listUnitPrice=200.0,
            adjustmentPercent=-10.0,
            unitPrice=180.0,
            amount=2,
            totalPrice=360.0,
            sources=[BOX],
            sourceText="3 Widget 2 x 180.00",
        ),
        line("3a", kind="alternative", parentKey="3"),
    ]
    r = create(
        client,
        auth,
        lines,
        quoteNumber="Q-7",
        validUntil="2026-12-31",
        currency="EUR",
        taxTotal=19.0,
        extractionWarnings=["check this"],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # camelCase for the new request fields, snake_case for the old ones
    assert body["quoteNumber"] == "Q-7"
    assert body["validUntil"] == "2026-12-31"
    assert body["currency"] == "EUR"
    assert body["taxTotal"] == 19.0
    assert body["netTotal"] == 360.0
    assert body["extractionWarnings"] == [{"message": "check this", "sources": []}]
    assert body["total_cost"] == 379.0
    assert "quote_number" not in body and "tax_total" not in body
    first, second = body["order_lines"]
    assert first["key"] == "3" and first["kind"] == "item"
    assert first["parentKey"] is None
    assert first["position"] == "3"
    assert first["vendorItemNumber"] == "AB-1"
    assert first["listUnitPrice"] == 200.0
    assert first["adjustmentPercent"] == -10.0
    assert first["sources"] == [BOX]
    assert first["sourceText"] == "3 Widget 2 x 180.00"
    assert first["articleId"] is None
    assert first["unit_price"] == 180.0 and first["total_price"] == 360.0
    assert second["kind"] == "alternative" and second["parentKey"] == "3"
    assert not {"parent_key", "list_unit_price", "source_text", "article_id"} & set(first)
    # Same data on GET
    got = client.get(f"/requests/{body['id']}", headers=auth).json()
    assert got["order_lines"][0]["sources"] == [BOX]


def test_server_computes_totals(client, auth):
    lines = [
        line("1", unitPrice=100.0, amount=3, totalPrice=1.0),  # wrong total sent
        line("2", unitPrice=50.0, kind="alternative", parentKey="1"),
        line("3", listUnitPrice=200.0, adjustmentPercent=-10.0, unitPrice=1.0),
        line("4", unitPrice=40.0, kind="discount", totalPrice=40.0),  # sent positive
    ]
    r = create(client, auth, lines, totalCost=99999.0, taxTotal=10.0)
    assert r.status_code == 201, r.text
    body = r.json()
    by_key = {ln["key"]: ln for ln in body["order_lines"]}
    assert by_key["1"]["total_price"] == 300.0
    assert by_key["3"]["unit_price"] == 180.0 and by_key["3"]["total_price"] == 180.0
    assert by_key["4"]["total_price"] == -40.0 and by_key["4"]["unit_price"] == -40.0
    assert body["netTotal"] == 300.0 + 180.0 - 40.0  # alternative excluded
    assert body["taxTotal"] == 10.0
    assert body["total_cost"] == 450.0


def test_legacy_payload_defaults(client, auth):
    legacy = {k: v for k, v in line("x").items() if k != "key"}
    legacy2 = dict(legacy, positionDescription="Second")
    r = create(client, auth, [legacy, legacy2], totalCost=1.0)
    assert r.status_code == 201, r.text
    body = r.json()
    assert [ln["key"] for ln in body["order_lines"]] == ["1", "2"]
    assert {ln["kind"] for ln in body["order_lines"]} == {"item"}
    assert body["currency"] == "USD" and body["total_cost"] == 200.0


@pytest.mark.parametrize(
    "lines",
    [
        [line("1", kind="discount", parentKey="nope")],  # dangling
        [line("1", parentKey="2"), line("2")],  # item with a parent
        [line("1", kind="alternative", parentKey="2"), line("2", kind="surcharge")],  # parent not item
        [line("1"), line("2", kind="discount", parentKey="1"),
         line("3", kind="fee", parentKey="2")],  # adjustment -> adjustment
        [line("1"), line("1")],  # duplicate key
        [line("1"), line("2", kind="optional", parentKey="1"),
         line("3", kind="alternative", parentKey="2")],  # alt -> optional
        [line("1"), line("2", kind="alternative", parentKey="1"),
         line("3", kind="optional", parentKey="2")],  # optional -> non-item
        [line("1"), line("2", kind="alternative", parentKey="1"),
         line("3", kind="alternative", parentKey="2")],  # alt -> alt
    ],
    ids=["dangling", "item-parent", "alt-parent-not-item",
         "adjustment-chain", "duplicate", "alt-optional", "optional-non-item", "alt-chain"],
)
def test_invalid_links_rejected(client, auth, lines):
    r = create(client, auth, lines)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_child_of_inactive_line_excluded(client, auth):
    lines = [
        line("1", unitPrice=100.0),
        line("2", kind="alternative", parentKey="1", unitPrice=80.0),
        line("3", kind="discount", parentKey="2", unitPrice=-10.0, totalPrice=-10.0),
    ]
    r = create(client, auth, lines)
    assert r.status_code == 201, r.text
    assert r.json()["netTotal"] == 100.0


def _make(client, auth, lines=None, **kw):
    r = create(client, auth, lines or [line("1"), line("2", unitPrice=50.0)], **kw)
    assert r.status_code == 201, r.text
    return r.json()


def _echo(body):
    """The lines of a response, as a client would send them back."""
    out = []
    for ln in body["order_lines"]:
        out.append({
            "positionDescription": ln["position_description"],
            "unitPrice": ln["unit_price"], "amount": ln["amount"], "unit": ln["unit"],
            "totalPrice": ln["total_price"], "key": ln["key"], "parentKey": ln["parentKey"],
            "kind": ln["kind"], "position": ln["position"],
            "vendorItemNumber": ln["vendorItemNumber"],
            "listUnitPrice": ln["listUnitPrice"],
            "adjustmentPercent": ln["adjustmentPercent"], "sources": ln["sources"],
            "sourceText": ln["sourceText"], "articleId": ln["articleId"],
        })
    return out


def test_update_lines_when_not_open_409(client, auth):
    body = _make(client, auth)
    rid = body["id"]
    client.patch(f"/requests/{rid}/status", json={"status": "in_progress"}, headers=auth)
    changed = _echo(body)
    changed[0]["amount"] = 5
    r = put(client, auth, rid, changed)
    assert r.status_code == 409, r.text
    # nothing was persisted
    got = client.get(f"/requests/{rid}", headers=auth).json()
    assert got["order_lines"][0]["amount"] == 1


def test_update_header_only_keeps_lines(client, auth):
    lines = [
        line("1", kind="item", sources=[BOX], sourceText="row text"),
        line("2", kind="alternative", parentKey="1", sources=[BOX]),
    ]
    body = _make(client, auth, lines)
    rid = body["id"]
    client.patch(f"/requests/{rid}/status", json={"status": "in_progress"}, headers=auth)
    r = put(client, auth, rid, _echo(body), requestorName="Someone Else")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["requestor_name"] == "Someone Else"
    assert [ln["kind"] for ln in out["order_lines"]] == ["item", "alternative"]
    assert [ln["sources"] for ln in out["order_lines"]] == [[BOX], [BOX]]
    assert out["order_lines"][0]["sourceText"] == "row text"
    assert out["order_lines"][1]["parentKey"] == "1"


def _approve(client, auth, rid):
    r = client.patch(f"/requests/{rid}/approval", json={"decision": "approved"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["approval_status"] == "approved"


def test_approved_total_change_resets_approval(client, auth):
    body = _make(client, auth)
    rid = body["id"]
    _approve(client, auth, rid)
    changed = _echo(body)
    changed[0]["amount"] = 3
    r = put(client, auth, rid, changed)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approval_status"] == "pending"
    assert out["approved_by"] is None and out["approved_at"] is None
    assert ("approval_reset", "Approval reset: total changed") in actions(rid)


def test_approved_no_total_change_keeps_approval(client, auth):
    body = _make(client, auth)
    rid = body["id"]
    _approve(client, auth, rid)
    r = put(client, auth, rid, _echo(body), requestorName="Renamed")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["approval_status"] == "approved"
    assert out["approved_by"] is not None
    assert all(a != "approval_reset" for a, _ in actions(rid))


def test_tax_total_change_resets_approval_and_missing_tax_keeps_stored(client, auth):
    body = _make(client, auth, taxTotal=5.0)
    rid = body["id"]
    _approve(client, auth, rid)
    # tax omitted -> stored tax kept, total unchanged, approval kept
    r = put(client, auth, rid, _echo(body))
    assert r.json()["taxTotal"] == 5.0 and r.json()["approval_status"] == "approved"
    r = put(client, auth, rid, _echo(body), taxTotal=9.0)
    assert r.json()["total_cost"] == 159.0 and r.json()["approval_status"] == "pending"


def test_unknown_article_rejected(client, auth):
    r = create(client, auth, [line("1", articleId=str(uuid.uuid4()))])
    assert r.status_code == 422


def test_fractional_list_price_round_trips(client, auth):
    lines = [line("1", listUnitPrice=0.135, adjustmentPercent=-10.0, amount=100,
                  unitPrice=0.0, totalPrice=0.0)]
    body = _make(client, auth, lines)
    rid = body["id"]
    assert body["order_lines"][0]["listUnitPrice"] == 0.14
    client.patch(f"/requests/{rid}/status", json={"status": "in_progress"}, headers=auth)
    r = put(client, auth, rid, _echo(body), requestorName="Other")
    assert r.status_code == 200, r.text
    assert r.json()["order_lines"] == body["order_lines"]
    # the raw (unrounded) inputs also compare equal
    r = put(client, auth, rid, lines)
    assert r.status_code == 200, r.text


FS = {"vendorName": [BOX], "vatId": [{"page": 1, "x0": 0.2, "y0": 0.3, "x1": 0.6, "y1": 0.32}]}


def test_field_sources_round_trip_and_header_only_keeps(client, auth):
    body = _make(client, auth, fieldSources=FS)
    rid = body["id"]
    assert body["fieldSources"] == FS
    assert client.get(f"/requests/{rid}", headers=auth).json()["fieldSources"] == FS
    # header-only PUT without fieldSources keeps it
    r = put(client, auth, rid, _echo(body), requestorName="Someone Else")
    assert r.status_code == 200, r.text
    assert r.json()["fieldSources"] == FS
    # explicit fieldSources replaces it
    r = put(client, auth, rid, _echo(body), fieldSources={"vendorName": [BOX]})
    assert r.json()["fieldSources"] == {"vendorName": [BOX]}


def test_extraction_warning_objects_round_trip(client, auth):
    warns = [
        {"message": "Quote mentions a charge without an amount: Fuel surcharge",
         "sources": [BOX]},
        "plain string",
    ]
    r = create(client, auth, [line("1", unitPrice=10.0, amount=1)], extractionWarnings=warns)
    assert r.status_code == 201, r.text
    expected = [warns[0], {"message": "plain string", "sources": []}]
    body = r.json()
    assert body["extractionWarnings"] == expected
    assert client.get(f"/requests/{body['id']}", headers=auth).json()["extractionWarnings"] == expected


def test_header_only_put_keeps_warnings(client, auth):
    warns = [{"message": "w", "sources": [BOX]}]
    body = create(client, auth, [line("1", unitPrice=10.0, amount=1)],
                  extractionWarnings=warns).json()
    lines = [line("1", unitPrice=10.0, amount=1)]
    r = put(client, auth, body["id"], lines, vendorName="V2")
    assert r.status_code == 200, r.text
    assert r.json()["extractionWarnings"] == warns
    r2 = put(client, auth, body["id"], lines, extractionWarnings=["s"])
    assert r2.json()["extractionWarnings"] == [{"message": "s", "sources": []}]


def test_legacy_string_warnings_in_db_load_as_objects(client, auth):
    from app.models import ProcurementRequest

    body = create(client, auth, [line("1", unitPrice=10.0, amount=1)]).json()
    with Session() as s:
        row = s.get(ProcurementRequest, uuid.UUID(body["id"]))
        row.extraction_warnings = ["old string", {"message": "new", "sources": [BOX]}]
        s.commit()
    got = client.get(f"/requests/{body['id']}", headers=auth).json()
    assert got["extractionWarnings"] == [
        {"message": "old string", "sources": []}, {"message": "new", "sources": [BOX]}]
