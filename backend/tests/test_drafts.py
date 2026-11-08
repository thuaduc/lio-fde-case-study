"""Server-side request drafts: CRUD, privacy, summary fields, size limit,
document round-trip, create-from-draft. TestClient over in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import OrganizationMember, RequestActivity, RequestDocument
from app.models.request_draft import RequestDraft
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
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    yield TestClient(app)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


def _signup(client):
    r = client.post(
        "/auth/signup",
        json={"email": f"u{uuid.uuid4().hex[:8]}@example.com", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _new_org(client):
    headers = _signup(client)
    slug = f"org-{uuid.uuid4().hex[:8]}"
    r = client.post("/organizations", json={"name": slug, "slug": slug}, headers=headers)
    assert r.status_code == 201, r.text
    return headers


@pytest.fixture
def auth(client):
    return _new_org(client)


@pytest.fixture
def teammate(client, auth):
    """A second user in the SAME org as ``auth`` (``auth`` is the org admin)."""
    headers = _signup(client)
    with Session() as s:
        admin_id = uuid.UUID(client.get("/auth/me", headers=auth).json()["user"]["id"])
        mate_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["user"]["id"])
        org_id = s.scalar(
            select(OrganizationMember.organization_id).where(
                OrganizationMember.user_id == admin_id
            )
        )
        s.add(OrganizationMember(organization_id=org_id, user_id=mate_id, role="requester"))
        s.commit()
    return headers


@pytest.fixture
def outsider(client):
    return _new_org(client)


PDF = b"%PDF-1.4 test"


def pdf(name="quote.pdf", content=PDF, ctype="application/pdf"):
    return {"file": (name, content, ctype)}


def mk(client, headers, data=None):
    r = client.post("/drafts", json={"data": data if data is not None else {}}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_requires_auth(client):
    assert client.get("/drafts").status_code == 401


def test_crud(client, auth):
    d = mk(client, auth, {"title": "Chairs", "foo": [1, 2]})
    assert d["data"] == {"title": "Chairs", "foo": [1, 2]}
    did = d["id"]

    r = client.get(f"/drafts/{did}", headers=auth)
    assert r.status_code == 200 and r.json()["data"]["foo"] == [1, 2]

    r = client.put(f"/drafts/{did}", json={"data": {"title": "Desks"}}, headers=auth)
    assert r.status_code == 200
    assert r.json()["title"] == "Desks"
    assert "data" not in r.json()
    assert client.get(f"/drafts/{did}", headers=auth).json()["data"] == {"title": "Desks"}

    assert client.delete(f"/drafts/{did}", headers=auth).status_code == 204
    assert client.get(f"/drafts/{did}", headers=auth).status_code == 404
    assert client.delete(f"/drafts/{did}", headers=auth).status_code == 404


def test_data_must_be_object(client, auth):
    r = client.post("/drafts", json={"data": [1]}, headers=auth)
    assert r.status_code == 422


def test_list_summary_fields_newest_first(client, auth):
    a = mk(client, auth, {"title": "A", "vendorName": "V", "totalCost": 12.5})
    b = mk(client, auth, {"title": 5, "vendorName": None, "totalCost": True})
    c = mk(client, auth, {"title": "C", "totalCost": "9"})
    client.put(f"/drafts/{a['id']}/document", files=pdf("a.pdf"), headers=auth)
    # SQLite's now() has 1s resolution: pin the order explicitly.
    with Session() as s:
        for i, d in enumerate((c, b, a)):
            row = s.get(RequestDraft, uuid.UUID(d["id"]))
            row.updated_at = datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc)
        s.commit()
    r = client.put(f"/drafts/{a['id']}", json={"data": {"title": "A2", "vendorName": "V", "totalCost": 7}}, headers=auth)
    assert r.json()["updatedAt"] > "2026-01-02"  # bumped to now
    rows = client.get("/drafts", headers=auth).json()
    ids = [r["id"] for r in rows]
    assert set(ids) == {a["id"], b["id"], c["id"]}
    assert ids[0] == a["id"]  # most recently updated
    by_id = {r["id"]: r for r in rows}
    assert by_id[a["id"]] == {
        "id": a["id"],
        "title": "A2",
        "vendorName": "V",
        "totalCost": 7,
        "fileName": "a.pdf",
        "hasDocument": True,
        "updatedAt": by_id[a["id"]]["updatedAt"],
    }
    assert by_id[b["id"]]["title"] is None
    assert by_id[b["id"]]["vendorName"] is None
    assert by_id[b["id"]]["totalCost"] is None
    assert by_id[b["id"]]["hasDocument"] is False
    assert by_id[b["id"]]["fileName"] is None
    assert by_id[c["id"]]["totalCost"] is None


def test_privacy_other_user_same_org_incl_admin(client, auth, teammate):
    # teammate (member) owns a draft; the org admin must not see it.
    d = mk(client, teammate, {"title": "secret"})
    did = d["id"]
    assert client.get("/drafts", headers=auth).json() == []
    assert client.get(f"/drafts/{did}", headers=auth).status_code == 404
    assert client.put(f"/drafts/{did}", json={"data": {}}, headers=auth).status_code == 404
    assert client.put(f"/drafts/{did}/document", files=pdf(), headers=auth).status_code == 404
    assert client.get(f"/drafts/{did}/document", headers=auth).status_code == 404
    assert client.delete(f"/drafts/{did}", headers=auth).status_code == 404
    # and still intact for the owner
    assert client.get(f"/drafts/{did}", headers=teammate).status_code == 200
    # reverse direction
    d2 = mk(client, auth, {"title": "admin's"})
    assert client.get(f"/drafts/{d2['id']}", headers=teammate).status_code == 404


def test_privacy_other_org(client, auth, outsider):
    d = mk(client, auth, {})
    assert client.get(f"/drafts/{d['id']}", headers=outsider).status_code == 404
    assert client.delete(f"/drafts/{d['id']}", headers=outsider).status_code == 404
    assert client.get("/drafts", headers=outsider).json() == []


def test_size_limit(client, auth):
    big = {"blob": "x" * (2 * 1024 * 1024)}
    assert client.post("/drafts", json={"data": big}, headers=auth).status_code == 413
    d = mk(client, auth, {"title": "ok"})
    assert client.put(f"/drafts/{d['id']}", json={"data": big}, headers=auth).status_code == 413
    assert client.get(f"/drafts/{d['id']}", headers=auth).json()["data"] == {"title": "ok"}


def test_document_round_trip_and_checks(client, auth):
    did = mk(client, auth)["id"]
    assert client.get(f"/drafts/{did}/document", headers=auth).status_code == 404
    r = client.put(f"/drafts/{did}/document", files=pdf("q.pdf"), headers=auth)
    assert r.status_code == 204
    r = client.get(f"/drafts/{did}/document", headers=auth)
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="q.pdf"' in r.headers["content-disposition"]
    assert client.get(f"/drafts/{did}", headers=auth).json()["hasDocument"] is True

    r = client.put(
        f"/drafts/{did}/document", files=pdf("x.txt", b"hi", "text/plain"), headers=auth
    )
    assert r.status_code == 400
    r = client.put(
        f"/drafts/{did}/document",
        files=pdf(content=b"0" * (10 * 1024 * 1024 + 1)),
        headers=auth,
    )
    assert r.status_code == 413


REQ = {
    "requestorName": "Rita",
    "titleShortDescription": "Quote",
    "vendorName": "Acme",
    "commodityGroupId": 31,
    "orderLines": [
        {
            "positionDescription": "L",
            "unitPrice": 10.0,
            "amount": 1,
            "unit": "pc",
            "totalPrice": 10.0,
            "key": "1",
        }
    ],
}


def test_create_from_draft_moves_pdf_and_deletes_draft(client, auth):
    did = mk(client, auth, {"title": "T"})["id"]
    client.put(f"/drafts/{did}/document", files=pdf("moved.pdf"), headers=auth)
    r = client.post("/requests", json={**REQ, "draftId": did}, headers=auth)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.get(f"/drafts/{did}", headers=auth).status_code == 404
    r = client.get(f"/requests/{rid}/document", headers=auth)
    assert r.status_code == 200 and r.content == PDF
    assert 'filename="moved.pdf"' in r.headers["content-disposition"]
    with Session() as s:
        acts = [
            (a.action, a.summary)
            for a in s.scalars(
                select(RequestActivity).where(RequestActivity.request_id == uuid.UUID(rid))
            )
        ]
        assert ("document_attached", "Attached document moved.pdf") in acts
        assert s.get(RequestDraft, uuid.UUID(did)) is None


def test_create_from_draft_without_document(client, auth):
    did = mk(client, auth)["id"]
    r = client.post("/requests", json={**REQ, "draftId": did}, headers=auth)
    assert r.status_code == 201
    assert client.get(f"/drafts/{did}", headers=auth).status_code == 404
    assert client.get(f"/requests/{r.json()['id']}/document", headers=auth).status_code == 404


def test_create_with_foreign_or_missing_draft_is_422(client, auth, teammate, outsider):
    foreign = mk(client, teammate)["id"]
    other_org = mk(client, outsider)["id"]
    before = len(client.get("/requests", headers=auth).json())
    for did in (foreign, other_org, str(uuid.uuid4())):
        r = client.post("/requests", json={**REQ, "draftId": did}, headers=auth)
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "Draft not found"
    assert len(client.get("/requests", headers=auth).json()) == before
    # the foreign drafts survive
    assert client.get(f"/drafts/{foreign}", headers=teammate).status_code == 200
