"""Per-organization commodity groups with booking-rule descriptions, assigned
per order line. TestClient over in-memory SQLite, seeded like production."""
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import CommodityGroup, Organization
from app.schemas.extraction import LlmFieldSources, LlmLine, LlmQuote, LlmTotalSources
from app.seed import DEMO_PASSWORD, seed
from app.services import extraction as svc
from app.services.pdf_layout import Box, PdfLayout, PdfRow

BOOKING_RULE = (
    "Group descriptions are this organization's booking rules. When an item "
    "matches a description, use that group, even if another group's name looks "
    "more specific."
)

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
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    yield TestClient(app)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


def login(client, email):
    r = client.post("/auth/signin", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def acme(client):
    return login(client, "admin@acme.com")


@pytest.fixture
def requester(client):
    return login(client, "requester@acme.com")


@pytest.fixture
def bmw(client):
    return login(client, "admin@bmw.com")


def groups(client, headers, **params):
    r = client.get("/commodity-groups", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def group_id(client, headers, category, name):
    return next(
        g["id"] for g in groups(client, headers)
        if g["category"] == category and g["name"] == name
    )


def new_group(client, headers, name=None, **kw):
    body = {"category": "Test", "name": name or f"G {uuid.uuid4().hex[:6]}", **kw}
    r = client.post("/commodity-groups", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def line(key, total=100.0, **kw):
    base = {"positionDescription": f"Line {key}", "unitPrice": total, "amount": 1,
            "unit": "pc", "totalPrice": total, "key": key}
    base.update(kw)
    return base


def req(lines, **kw):
    base = {"requestorName": "Rita", "titleShortDescription": "Quote",
            "vendorName": "V", "orderLines": lines}
    base.update(kw)
    return base


def as_update(body):
    body = dict(body)
    body["title"] = body.pop("titleShortDescription")
    return body


# --- listing and scoping ------------------------------------------------------


def test_list_is_org_scoped(client, acme, bmw):
    a = groups(client, acme)
    b = groups(client, bmw)
    assert {g["id"] for g in a}.isdisjoint({g["id"] for g in b})
    # sorted by category, then name
    assert [(g["category"], g["name"]) for g in a] == sorted(
        (g["category"], g["name"]) for g in a
    )
    hw = next(g for g in a if g["name"] == "IT Hardware")
    assert hw["category"] == "Information Technology"
    assert "toner" in hw["description"].lower() and hw["archived"] is False
    assert not any(g["name"] == "IT Hardware" for g in b)
    # global templates are not listed: only base templates were copied
    assert not any(" — " in g["name"] for g in a)
    # another org's group is a 404
    r = client.patch(f"/commodity-groups/{hw['id']}", json={"description": "x"}, headers=bmw)
    assert r.status_code == 404


def test_new_org_gets_template_copies(client):
    r = client.post("/auth/signup", json={"email": f"n{uuid.uuid4().hex[:6]}@x.com",
                                          "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    slug = f"org-{uuid.uuid4().hex[:6]}"
    assert client.post("/organizations", json={"name": slug, "slug": slug},
                       headers=h).status_code == 201
    got = groups(client, h)
    with Session() as s:
        n_base = sum(
            " — " not in t.name
            for t in s.scalars(select(CommodityGroup).where(
                CommodityGroup.organization_id.is_(None)))
        )
    assert len(got) == n_base
    assert all(g["description"] is None for g in got)


def test_admin_crud_and_requester_forbidden(client, acme, requester):
    g = new_group(client, acme, description="Booking rule")
    gid = g["id"]
    assert g["description"] == "Booking rule" and g["archived"] is False
    assert client.post("/commodity-groups", json={"category": "X", "name": "Y"},
                       headers=requester).status_code == 403
    assert client.patch(f"/commodity-groups/{gid}", json={"name": "Z"},
                        headers=requester).status_code == 403
    assert client.delete(f"/commodity-groups/{gid}", headers=requester).status_code == 403
    assert client.post(f"/commodity-groups/{gid}/archive",
                       headers=requester).status_code == 403
    # requesters can still read the list
    assert any(x["id"] == gid for x in groups(client, requester))

    r = client.patch(f"/commodity-groups/{gid}", json={"description": "New rule"},
                     headers=acme)
    assert r.status_code == 200 and r.json()["description"] == "New rule"
    assert r.json()["name"] == g["name"]
    assert client.delete(f"/commodity-groups/{gid}", headers=acme).status_code == 204
    assert client.patch(f"/commodity-groups/{gid}", json={"name": "Z"},
                        headers=acme).status_code == 404


def test_delete_in_use_409_archive_ok(client, acme):
    gid = new_group(client, acme)["id"]
    other = group_id(client, acme, "Information Technology", "Software")
    # one request uses it as its group, another only on a line
    r1 = client.post("/requests", json=req([line("1")], commodityGroupId=gid), headers=acme)
    r2 = client.post("/requests", json=req(
        [line("1", 500, commodityGroupId=other), line("2", 10, commodityGroupId=gid)]
    ), headers=acme)
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    r = client.delete(f"/commodity-groups/{gid}", headers=acme)
    assert r.status_code == 409
    assert r.json()["detail"] == "In use by 2 requests — archive it instead"

    r = client.post(f"/commodity-groups/{gid}/archive", headers=acme)
    assert r.status_code == 200 and r.json()["archived"] is True
    r = client.post(f"/commodity-groups/{gid}/unarchive", headers=acme)
    assert r.status_code == 200 and r.json()["archived"] is False
    for rid in (r1.json()["id"], r2.json()["id"]):
        client.delete(f"/requests/{rid}", headers=acme)


def test_archived_hidden_from_list_but_kept_on_request(client, acme):
    gid = new_group(client, acme)["id"]
    body = req([line("1", commodityGroupId=gid)], commodityGroupId=gid)
    created = client.post("/requests", json=body, headers=acme).json()
    client.post(f"/commodity-groups/{gid}/archive", headers=acme)

    assert gid not in {g["id"] for g in groups(client, acme)}
    assert gid in {g["id"] for g in groups(client, acme, include_archived="true")}

    got = client.get(f"/requests/{created['id']}", headers=acme).json()
    assert got["commodity_groups"]["id"] == gid
    assert got["commodity_groups"]["archived"] is True
    assert got["order_lines"][0]["commodityGroupId"] == gid

    # newly choosing an archived group is rejected...
    assert client.post("/requests", json=body, headers=acme).status_code == 422
    # ...but an existing request may keep it unchanged
    r = client.put(f"/requests/{created['id']}", json=as_update(body), headers=acme)
    assert r.status_code == 200, r.text
    # an older client that doesn't send the line field keeps the stored group
    legacy = as_update(req([line("1")], commodityGroupId=gid))
    r = client.put(f"/requests/{created['id']}", json=legacy, headers=acme)
    assert r.status_code == 200, r.text
    assert r.json()["order_lines"][0]["commodityGroupId"] == gid
    client.delete(f"/requests/{created['id']}", headers=acme)


def test_create_request_rejects_other_org_group(client, acme, bmw):
    bmw_gid = groups(client, bmw)[0]["id"]
    assert client.post("/requests", json=req([line("1")], commodityGroupId=bmw_gid),
                       headers=acme).status_code == 422
    # a global template id is not the org's group either
    assert client.post("/requests", json=req([line("1")], commodityGroupId=1),
                       headers=acme).status_code == 422
    # neither a line group nor a request group: still required
    assert client.post("/requests", json=req([line("1")]), headers=acme).status_code == 422


def test_line_group_must_be_own_org_active(client, acme, bmw):
    own = group_id(client, acme, "Information Technology", "Software")
    bmw_gid = groups(client, bmw)[0]["id"]
    r = client.post("/requests", json=req(
        [line("1", commodityGroupId=bmw_gid)], commodityGroupId=own), headers=acme)
    assert r.status_code == 422
    archived = new_group(client, acme)["id"]
    client.post(f"/commodity-groups/{archived}/archive", headers=acme)
    r = client.post("/requests", json=req(
        [line("1", commodityGroupId=archived)], commodityGroupId=own), headers=acme)
    assert r.status_code == 422


# --- per-line assignment --------------------------------------------------------


def test_primary_group_is_largest_active_spend(client, acme):
    a = group_id(client, acme, "Information Technology", "Software")
    b = group_id(client, acme, "Information Technology", "IT Hardware")
    lines = [
        line("1", 100, commodityGroupId=a),
        line("2", 300, commodityGroupId=b),
        line("3", 900, kind="alternative", parentKey="1", commodityGroupId=a),
    ]
    # no request-level group sent: the server computes it from the lines
    r = client.post("/requests", json=req(lines), headers=acme)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["commodity_group_id"] == b
    assert [ln["commodityGroupId"] for ln in body["order_lines"]] == [a, b, a]
    # a client-sent request group loses to the line spend
    r = client.post("/requests", json=req(lines, commodityGroupId=a), headers=acme)
    assert r.json()["commodity_group_id"] == b
    # update: lines re-grouped -> primary recomputed
    lines[1]["commodityGroupId"] = a
    r = client.put(f"/requests/{body['id']}", json=as_update(req(lines)), headers=acme)
    assert r.status_code == 200, r.text
    assert r.json()["commodity_group_id"] == a
    for rid in (body["id"],):
        client.delete(f"/requests/{rid}", headers=acme)


def test_adjustment_inherits_parent_group(client, acme):
    a = group_id(client, acme, "Information Technology", "IT Hardware")
    b = group_id(client, acme, "Information Technology", "Software")
    # on save: a discount on item 1 without a group takes the item's group
    r = client.post("/requests", json=req([
        line("1", 100, commodityGroupId=a),
        line("2", 10, kind="discount", parentKey="1"),
        line("3", 5, kind="shipping", commodityGroupId=b),  # whole quote: own group
        line("4", 95, commodityGroupId=b),
    ]), headers=acme)
    assert r.status_code == 201, r.text
    got = [ln["commodityGroupId"] for ln in r.json()["order_lines"]]
    assert got == [a, a, b, b]
    # A: 100 - 10 = 90 < B: 5 + 95 = 100
    assert r.json()["commodity_group_id"] == b
    client.delete(f"/requests/{r.json()['id']}", headers=acme)

    # in extraction: the parent's group always wins; an unknown id becomes null
    def ll(**kw):
        base = dict(position=None, vendor_item_number=None, description="X",
                    kind="item", parent_position=None, quantity=1, unit="ea",
                    list_unit_price=None, adjustment_percent=None, unit_price=None,
                    total_price=10.0, source_rows=[], commodity_group_id=None)
        base.update(kw)
        return LlmLine(**base)

    q = _quote([
        ll(position="1", total_price=100, commodity_group_id=11),
        ll(position="2", kind="discount", parent_position="1", total_price=-5,
           commodity_group_id=22),
        ll(position="3", total_price=50, commodity_group_id=999),
        ll(position="4", kind="fee", total_price=3, commodity_group_id=22),
    ])
    data, _ = svc.to_extracted(q, PdfLayout(rows=[], page_count=1),
                               {11: "Electrical Engineering", 22: "Software"})
    assert [ln.commodity_group_id for ln in data.order_lines] == [11, 11, None, 22]
    assert data.commodity_group_id == 11
    dumped = data.model_dump(by_alias=True)
    assert dumped["orderLines"][0]["commodityGroupId"] == 11


def test_every_active_line_needs_a_group(client, acme):
    a = group_id(client, acme, "Information Technology", "IT Hardware")
    lines = [
        line("1", 300, commodityGroupId=a),
        line("2", 100, positionDescription="Cable ties"),
    ]
    r = client.post("/requests", json=req(lines), headers=acme)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (
        "Every order line needs a commodity group (missing on: Cable ties)"
    )
    # alternatives and optional lines aren't booked, so they may stay without one
    lines[1].update(kind="alternative", parentKey="1")
    r = client.post("/requests", json=req(lines), headers=acme)
    assert r.status_code == 201, r.text
    assert [ln["commodityGroupId"] for ln in r.json()["order_lines"]] == [a, None]
    client.delete(f"/requests/{r.json()['id']}", headers=acme)


def test_request_group_fills_lines_without_one(client, acme):
    """An API client that sends one group for the request books its lines there."""
    a = group_id(client, acme, "Information Technology", "IT Hardware")
    g = group_id(client, acme, "Information Technology", "Software")
    r = client.post("/requests", json=req([
        line("1", 100, commodityGroupId=a),
        line("2", 10, kind="discount", parentKey="1"),  # inherits a, not g
        line("3", 50),
        line("4", 20, kind="optional"),  # not booked: left alone
    ], commodityGroupId=g), headers=acme)
    assert r.status_code == 201, r.text
    got = [ln["commodityGroupId"] for ln in r.json()["order_lines"]]
    assert got == [a, a, g, None]
    client.delete(f"/requests/{r.json()['id']}", headers=acme)


def test_older_client_update_keeps_or_rebooks_line_groups(client, acme):
    """A client that sends no line groups keeps them, unless it changes the request's."""
    a = group_id(client, acme, "Information Technology", "IT Hardware")
    b = group_id(client, acme, "Information Technology", "Software")
    c = group_id(client, acme, "Facility Management", "Electrical Engineering")
    created = client.post("/requests", json=req([
        line("1", 300, commodityGroupId=a),
        line("2", 100, commodityGroupId=b),
    ]), headers=acme).json()
    assert created["commodity_group_id"] == a

    # unchanged request group (e.g. only the title edited): line groups stay mixed
    legacy = as_update(req([line("1", 300), line("2", 100)], commodityGroupId=a))
    r = client.put(f"/requests/{created['id']}", json=legacy, headers=acme)
    assert r.status_code == 200, r.text
    assert [ln["commodityGroupId"] for ln in r.json()["order_lines"]] == [a, b]

    # a changed request group re-books the lines that don't send their own
    legacy["commodityGroupId"] = c
    r = client.put(f"/requests/{created['id']}", json=legacy, headers=acme)
    assert r.status_code == 200, r.text
    assert [ln["commodityGroupId"] for ln in r.json()["order_lines"]] == [c, c]
    assert r.json()["commodity_group_id"] == c
    client.delete(f"/requests/{created['id']}", headers=acme)


# --- prompt -------------------------------------------------------------------


def _quote(lines, **kw):
    fsr = LlmFieldSources(vendor_name=[], vendor_tax_id=[], department=[],
                          quote_number=[], valid_until=[])
    base = dict(title="Q", vendor_name="V", vendor_tax_id=None, department=None,
                quote_number=None, valid_until=None, currency="USD", lines=lines,
                net_total=None, tax_total=None, grand_total=None, unpriced_charges=[],
                total_source_rows=LlmTotalSources(net=[], tax=[], grand=[]),
                commodity_group_id=None, field_source_rows=fsr)
    base.update(kw)
    return LlmQuote(**base)


class FakeClient:
    def __init__(self, parsed=None, content="{}"):
        self.calls = []
        self._parsed, self._content = parsed, content
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            parse=self._parse, create=self._create))

    def _parse(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(parsed=self._parsed, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=msg)])

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_prompt_contains_org_descriptions_only(client, acme, bmw, monkeypatch):
    bmw_only = new_group(client, bmw, name="Zeppelin Spare Parts")["id"]
    archived = new_group(client, acme, name="Retired Acme Group",
                         description="Never offer this")["id"]
    client.post(f"/commodity-groups/{archived}/archive", headers=acme)

    row = PdfRow(id="p1r1", page=1, text="Cable ties 8 in, pack of 100",
                 box=Box(page=1, x0=0, y0=0, x1=1, y1=0.1))
    monkeypatch.setattr(svc, "extract_layout",
                        lambda b: PdfLayout(rows=[row], page_count=1))
    monkeypatch.setattr(svc.settings, "openai_api_key", "sk-test")
    fake = FakeClient(parsed=_quote([]))
    monkeypatch.setattr(svc, "_get_client", lambda: fake)

    with Session() as db:
        acme_id = db.scalar(select(Organization.id).where(Organization.slug == "acme"))
        ee = db.scalar(select(CommodityGroup).where(
            CommodityGroup.organization_id == acme_id,
            CommodityGroup.name == "Electrical Engineering"))
        assert svc.extract_from_pdf(b"%PDF-", db, acme_id).success
        svc.extract_vendor_data("Cable ties 8 in", db, acme_id)

    assert len(fake.calls) == 2
    for call in fake.calls:
        prompt = call["messages"][1]["content"]
        assert "(ID | Category | Name | Description)" in prompt
        assert BOOKING_RULE in prompt
        assert f"{ee.id} | Facility Management | Electrical Engineering | " in prompt
        assert "cable ties" in prompt.lower()
        assert "| Information Technology | IT Hardware |" in prompt
        assert "| Office & Administration | Office Supplies |" in prompt
        assert "software subscriptions" in prompt
        assert "Zeppelin" not in prompt and f"\n{bmw_only} |" not in prompt
        assert "Retired Acme Group" not in prompt
        # hard-coded hints are gone
        assert "Office Equipment (15)" not in prompt
        assert "Promotional Materials (43)" not in prompt


# --- migration ----------------------------------------------------------------


def _load_migration():
    path = (Path(__file__).resolve().parent.parent
            / "alembic/versions/0007_acme_quote_review.py")
    spec = importlib.util.spec_from_file_location("mig0007", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def test_migration_repoints_requests():
    """Upgrade populated legacy tables, check backfills, then restore the old schema."""
    mig = _load_migration()
    assert (mig.revision, mig.down_revision) == ("0007", "0006")

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        for ddl in (
            "CREATE TABLE organizations (id CHAR(32) PRIMARY KEY)",
            "CREATE TABLE users (id CHAR(32) PRIMARY KEY)",
            "CREATE TABLE articles (id CHAR(32) PRIMARY KEY)",
            "CREATE TABLE commodity_groups (id INTEGER PRIMARY KEY, category TEXT NOT NULL,"
            " name TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)",
            "CREATE TABLE procurement_requests (id CHAR(32) PRIMARY KEY,"
            " organization_id CHAR(32), commodity_group_id INTEGER"
            " REFERENCES commodity_groups(id))",
            "CREATE TABLE order_lines (id CHAR(32) PRIMARY KEY, request_id CHAR(32) NOT NULL,"
            " line_order INTEGER NOT NULL)",
            "INSERT INTO organizations VALUES ('orga'), ('orgb')",
            "INSERT INTO commodity_groups (id, category, name) VALUES"
            " (1, 'IT', 'Hardware'), (2, 'IT', 'Software'),"
            " (3, 'IT', 'Hardware — Premium'), (4, 'Ghost', 'Phantom — Basic')",
            # r1: base template; r2: variant -> base copy; r3: no org; r4: no base match
            "INSERT INTO procurement_requests VALUES ('r1','orga',2), ('r2','orgb',3),"
            " ('r3',NULL,1), ('r4','orga',4)",
            "INSERT INTO order_lines VALUES ('l1','r1',1), ('l2','r2',1),"
            " ('l3','r3',1)",
        ):
            conn.execute(sa.text(ddl))
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()

        assert conn.execute(sa.text(
            "SELECT line_key, line_type, parent_key FROM order_lines"
        )).all() == [("1", "item", None)] * 3
        assert {"request_drafts", "article_embeddings"} <= set(sa.inspect(conn).get_table_names())
        assert "field_sources" in {c["name"] for c in sa.inspect(conn).get_columns("procurement_requests")}

        copies = conn.execute(sa.text(
            "SELECT organization_id, category, name, id FROM commodity_groups"
            " WHERE organization_id IS NOT NULL ORDER BY organization_id, name")).all()
        assert [(o, c, n) for o, c, n, _ in copies] == [
            ("orga", "IT", "Hardware"), ("orga", "IT", "Software"),
            ("orgb", "IT", "Hardware"), ("orgb", "IT", "Software"),
        ]
        ids = {(o, n): i for o, _, n, i in copies}
        assert all(i > 4 for i in ids.values())
        reqs = dict(conn.execute(sa.text(
            "SELECT id, commodity_group_id FROM procurement_requests")).all())
        assert reqs == {"r1": ids[("orga", "Software")], "r2": ids[("orgb", "Hardware")],
                        "r3": 1, "r4": 4}
        lines = dict(conn.execute(sa.text(
            "SELECT id, commodity_group_id FROM order_lines")).all())
        assert lines == {"l1": reqs["r1"], "l2": reqs["r2"], "l3": 1}
        # running the data step again adds nothing
        mig.copy_templates_to_orgs(conn)
        assert conn.execute(sa.text(
            "SELECT count(*) FROM commodity_groups")).scalar() == 8

        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
        reqs = dict(conn.execute(sa.text(
            "SELECT id, commodity_group_id FROM procurement_requests")).all())
        assert reqs == {"r1": 2, "r2": 1, "r3": 1, "r4": 4}
        assert conn.execute(sa.text(
            "SELECT count(*) FROM commodity_groups")).scalar() == 4
        cols = {c["name"] for c in sa.inspect(conn).get_columns("commodity_groups")}
        assert cols == {"id", "category", "name", "created_at"}
        cols = {c["name"] for c in sa.inspect(conn).get_columns("order_lines")}
        assert cols == {"id", "request_id", "line_order"}
        assert "request_drafts" not in sa.inspect(conn).get_table_names()
        assert "article_embeddings" not in sa.inspect(conn).get_table_names()
        assert {c["name"] for c in sa.inspect(conn).get_columns("procurement_requests")} == {
            "id", "organization_id", "commodity_group_id",
        }


# --- review fixes ---------------------------------------------------------------


def test_blank_category_or_name_is_422(client, acme):
    for body in ({"category": "  ", "name": "X"}, {"category": "X", "name": " \t"}):
        assert client.post("/commodity-groups", json=body, headers=acme).status_code == 422
    gid = new_group(client, acme)["id"]
    for body in ({"name": "   "}, {"category": ""}):
        r = client.patch(f"/commodity-groups/{gid}", json=body, headers=acme)
        assert r.status_code == 422, r.text
    # surrounding whitespace is trimmed, not rejected
    r = client.patch(f"/commodity-groups/{gid}", json={"name": "  Trimmed  "}, headers=acme)
    assert r.status_code == 200 and r.json()["name"] == "Trimmed"
    client.delete(f"/commodity-groups/{gid}", headers=acme)


def test_unchanged_legacy_group_is_kept(client, acme):
    from app.models import OrderLine, ProcurementRequest

    own = group_id(client, acme, "Information Technology", "Software")
    created = client.post("/requests", json=req([line("1", commodityGroupId=own)]),
                          headers=acme).json()
    rid = uuid.UUID(created["id"])
    # a legacy request still pointing at a global template (id 1)
    with Session() as s:
        s.get(ProcurementRequest, rid).commodity_group_id = 1
        for ln in s.scalars(select(OrderLine).where(OrderLine.request_id == rid)):
            ln.commodity_group_id = 1
        s.commit()
    body = as_update(req([line("1", commodityGroupId=1)], commodityGroupId=1))
    r = client.put(f"/requests/{rid}", json=body, headers=acme)
    assert r.status_code == 200, r.text
    assert r.json()["commodity_group_id"] == 1
    # choosing a different template is still rejected, on the line or the request
    r = client.put(f"/requests/{rid}", json=as_update(
        req([line("1", commodityGroupId=2)], commodityGroupId=1)), headers=acme)
    assert r.status_code == 422
    r = client.put(f"/requests/{rid}", json=as_update(
        req([line("1", commodityGroupId=1)], commodityGroupId=2)), headers=acme)
    assert r.status_code == 422
    client.delete(f"/requests/{rid}", headers=acme)


def test_acme_descriptions_state_only_the_rule_and_old_text_is_corrected():
    from app.seed import ACME_BOOKING_GROUPS, SUPERSEDED_ACME_DESCRIPTIONS

    with Session() as s:
        acme_id = s.scalar(select(Organization.id).where(Organization.slug == "acme"))

        def get(name):
            return s.scalar(select(CommodityGroup).where(
                CommodityGroup.organization_id == acme_id, CommodityGroup.name == name))

        expected = {name: desc for _, name, desc in ACME_BOOKING_GROUPS}
        assert expected == {
            "Electrical Engineering": "Cable ties are always booked here.",
            "IT Hardware": "Printer toner is always booked here.",
            "Office Supplies": "Small software subscriptions are always booked here.",
        }
        for name, desc in expected.items():
            assert get(name).description == desc

        old_ee = next(d for d in SUPERSEDED_ACME_DESCRIPTIONS if "Cable ties" in d)
        get("Electrical Engineering").description = old_ee
        get("IT Hardware").description = "Admin wording: toner goes here."
        s.commit()
        counts = seed(s)
        assert counts["acme_booking_groups"] == 1
        assert get("Electrical Engineering").description == expected["Electrical Engineering"]
        assert get("IT Hardware").description == "Admin wording: toner goes here."
        assert seed(s)["acme_booking_groups"] == 0
        get("IT Hardware").description = expected["IT Hardware"]
        s.commit()
