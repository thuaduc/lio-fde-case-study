"""Auto-approval below the org's approval threshold (Acme: $2,000).

Uses the seeded Acme Corp accounts (admin / buyer / requester) over an
in-memory SQLite DB.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.seed import DEMO_PASSWORD, seed

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


def _login(client, email):
    r = client.post("/auth/signin", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    return _login(client, "admin@acme.com")


@pytest.fixture
def buyer(client):
    return _login(client, "buyer@acme.com")


@pytest.fixture
def requester(client):
    return _login(client, "requester@acme.com")


@pytest.fixture
def threshold(client, admin):
    """Set the Acme threshold for a test; restore the seeded $2,000 after."""

    def set_threshold(value):
        r = client.patch(
            "/organizations/me/settings",
            json={"required_fields": [], "approval_threshold": value},
            headers=admin,
        )
        assert r.status_code == 200, r.text
        return r.json()

    yield set_threshold
    set_threshold(2000)


def _group_id(client, headers) -> int:
    groups = client.get("/commodity-groups", headers=headers).json()
    assert groups
    return groups[0]["id"]


def _payload(client, headers, total, tax=None):
    body = {
        "requestorName": "Rene",
        "titleShortDescription": "Chairs",
        "vendorName": "Office Co",
        "commodityGroupId": _group_id(client, headers),
        "orderLines": [
            {
                "positionDescription": "Chair",
                "unitPrice": total,
                "amount": 1,
                "unit": "pc",
                "totalPrice": total,
                "key": "1",
            }
        ],
    }
    if tax is not None:
        body["taxTotal"] = tax
    return body


def _create(client, headers, total, tax=None):
    r = client.post("/requests", json=_payload(client, headers, total, tax), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _update(client, headers, request, total):
    body = _payload(client, headers, total)
    body["title"] = body.pop("titleShortDescription")
    r = client.put(f"/requests/{request['id']}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _actions(client, headers, request_id):
    return [
        a["action"]
        for a in client.get(f"/requests/{request_id}/activity", headers=headers).json()
    ]


# --- settings ---------------------------------------------------------------


def test_acme_is_seeded_with_2000(client, requester):
    r = client.get("/organizations/me/settings", headers=requester)
    assert r.status_code == 200
    assert r.json()["approval_threshold"] == 2000


def test_seed_is_idempotent_and_keeps_admin_choice(client, threshold, requester):
    threshold(None)
    with Session() as s:
        seed(s)
    assert client.get("/organizations/me/settings", headers=requester).json()[
        "approval_threshold"
    ] is None


def test_settings_round_trip(client, admin, threshold):
    assert threshold(1500.5)["approval_threshold"] == 1500.5
    got = client.get("/organizations/me/settings", headers=admin).json()
    assert got == {"required_fields": [], "approval_threshold": 1500.5}
    assert threshold(None)["approval_threshold"] is None


@pytest.mark.parametrize("who", ["buyer", "requester"])
def test_non_admin_cannot_change_threshold(client, who, request):
    headers = request.getfixturevalue(who)
    r = client.patch(
        "/organizations/me/settings",
        json={"required_fields": [], "approval_threshold": 10},
        headers=headers,
    )
    assert r.status_code == 403


def test_negative_threshold_rejected(client, admin):
    r = client.patch(
        "/organizations/me/settings",
        json={"required_fields": [], "approval_threshold": -1},
        headers=admin,
    )
    assert r.status_code == 422


# --- create -----------------------------------------------------------------


def test_create_below_threshold_is_auto_approved(client, requester):
    req = _create(client, requester, 1400, tax=134.59)  # grand total 1534.59
    assert req["approval_status"] == "approved"
    assert req["auto_approved"] is True
    assert req["approved_by"] is None
    assert req["approved_at"] is not None
    activity = client.get(f"/requests/{req['id']}/activity", headers=requester).json()
    auto = [a for a in activity if a["action"] == "auto_approved"]
    assert len(auto) == 1
    assert auto[0]["actor_id"] is None
    assert auto[0]["summary"] == (
        "Auto-approved: total $1,534.59 is below the $2,000.00 threshold"
    )


def test_tax_counts_toward_threshold(client, requester):
    req = _create(client, requester, 1900, tax=100)  # grand total exactly 2000
    assert req["approval_status"] == "pending"
    assert req["auto_approved"] is False


def test_create_at_or_above_threshold_is_pending(client, requester):
    for total in (2000, 2500):
        req = _create(client, requester, total)
        assert req["approval_status"] == "pending"
        assert req["auto_approved"] is False
        assert "auto_approved" not in _actions(client, requester, req["id"])


def test_no_threshold_means_pending(client, requester, threshold):
    threshold(None)
    req = _create(client, requester, 5)
    assert req["approval_status"] == "pending"
    assert req["auto_approved"] is False


# --- update -----------------------------------------------------------------


def test_update_crossing_up_resets_to_pending(client, requester):
    req = _create(client, requester, 1000)
    assert req["auto_approved"] is True
    req = _update(client, requester, req, 2500)
    assert req["approval_status"] == "pending"
    assert req["auto_approved"] is False
    assert req["approved_at"] is None
    actions = _actions(client, requester, req["id"])
    assert actions.count("approval_reset") == 1


def test_update_crossing_down_auto_approves(client, requester):
    req = _create(client, requester, 2500)
    assert req["approval_status"] == "pending"
    req = _update(client, requester, req, 1500)
    assert req["approval_status"] == "approved"
    assert req["auto_approved"] is True
    actions = _actions(client, requester, req["id"])
    assert actions.count("auto_approved") == 1
    assert "approval_reset" not in actions


def test_manual_approval_dropping_below_becomes_auto(client, requester, buyer):
    req = _create(client, requester, 2500)
    r = client.patch(
        f"/requests/{req['id']}/approval", json={"decision": "approved"}, headers=buyer
    )
    assert r.status_code == 200
    req = _update(client, requester, req, 1500)
    assert req["auto_approved"] is True
    assert "approval_reset" not in _actions(client, requester, req["id"])


def test_rejected_stays_rejected(client, requester, buyer):
    req = _create(client, requester, 2500)
    client.patch(
        f"/requests/{req['id']}/approval", json={"decision": "rejected"}, headers=buyer
    )
    # Rejection closes the request; reopen it to edit the lines.
    client.patch(
        f"/requests/{req['id']}/status", json={"status": "open"}, headers=requester
    )
    req = _update(client, requester, req, 1500)
    assert req["approval_status"] == "rejected"
    assert req["auto_approved"] is False


def test_unchanged_total_keeps_manual_approval(client, requester, buyer):
    req = _create(client, requester, 2500)
    client.patch(
        f"/requests/{req['id']}/approval", json={"decision": "approved"}, headers=buyer
    )
    req = _update(client, requester, req, 2500)
    assert req["approval_status"] == "approved"
    assert req["auto_approved"] is False


def test_threshold_change_is_not_retroactive(client, requester, threshold):
    req = _create(client, requester, 1500)
    threshold(1000)
    got = client.get(f"/requests/{req['id']}", headers=requester).json()
    assert got["auto_approved"] is True


# --- decide -----------------------------------------------------------------


def test_deciding_auto_approved_request_conflicts(client, requester, buyer):
    req = _create(client, requester, 100)
    r = client.patch(
        f"/requests/{req['id']}/approval", json={"decision": "rejected"}, headers=buyer
    )
    assert r.status_code == 409


# --- status follows approval ------------------------------------------------


def _decide(client, headers, request, decision):
    r = client.patch(
        f"/requests/{request['id']}/approval", json={"decision": decision}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _status_changes(client, headers, request_id):
    return [
        (a["detail"]["from"], a["detail"]["to"], a["actor_id"])
        for a in client.get(f"/requests/{request_id}/activity", headers=headers).json()
        if a["action"] == "status_changed"
    ]


def test_pending_request_stays_open(client, requester):
    req = _create(client, requester, 2500)
    assert req["status"] == "open"
    assert _status_changes(client, requester, req["id"]) == []


def test_auto_approval_moves_request_in_progress(client, requester):
    req = _create(client, requester, 1000)
    assert req["status"] == "in_progress"
    assert _status_changes(client, requester, req["id"]) == [("open", "in_progress", None)]


def test_buyer_approval_moves_request_in_progress(client, requester, buyer):
    req = _decide(client, buyer, _create(client, requester, 2500), "approved")
    assert req["status"] == "in_progress"
    assert _status_changes(client, requester, req["id"]) == [
        ("open", "in_progress", req["approved_by"])
    ]


def test_rejection_closes_request(client, requester, buyer):
    req = _decide(client, buyer, _create(client, requester, 2500), "rejected")
    assert req["status"] == "closed"
    assert _status_changes(client, requester, req["id"]) == [
        ("open", "closed", req["approved_by"])
    ]


def test_approval_reset_reopens_request(client, requester):
    req = _create(client, requester, 1000)
    assert req["status"] == "in_progress"
    req = _update(client, requester, req, 2500)
    assert req["approval_status"] == "pending"
    assert req["status"] == "open"


def test_auto_approval_on_update_moves_request_in_progress(client, requester):
    req = _create(client, requester, 2500)
    req = _update(client, requester, req, 1500)
    assert req["status"] == "in_progress"


def test_approval_keeps_a_manually_closed_status(client, requester, buyer):
    req = _create(client, requester, 2500)
    r = client.patch(
        f"/requests/{req['id']}/status", json={"status": "closed"}, headers=requester
    )
    assert r.status_code == 200, r.text
    assert _decide(client, buyer, req, "approved")["status"] == "closed"
