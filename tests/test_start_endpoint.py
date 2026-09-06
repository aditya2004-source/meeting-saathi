"""Covers POST /meetings/start's wiring -- user_name/client_name/device_id are
threaded straight through to db.create_run(), and (Phase 0 production-audit
fix) a real, resolved customer identity is required before any of that runs
at all. Follows tests/test_entitlement.py's/test_run_ownership.py's
convention of a real TestClient + real app.db functions against a fresh
tmp_path sqlite file, rather than monkeypatching db.create_run -- needed
now since app.entitlement's real checks run in the same request.
"""
from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")


def _verified_customer(email: str) -> dict:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _auth_header(customer_id: str) -> dict:
    token = auth.issue_device_token(customer_id, label="test")
    return {"Authorization": f"Bearer {token}"}


def test_start_allows_a_first_time_user_with_no_name(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")

    response = client.post(
        "/meetings/start", data={"title": "Weekly Sync"}, headers=_auth_header(customer["id"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "received"
    run = db.get_run(body["id"])
    assert run["customer_id"] == customer["id"]
    assert run["billing_mode"] == "free"


def test_start_threads_client_name_and_device_id_to_create_run(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")

    response = client.post(
        "/meetings/start",
        data={"title": "Weekly Sync", "user_name": "Priya Shah", "client_name": "Acme Corp", "device_id": "abc-123"},
        headers=_auth_header(customer["id"]),
    )

    assert response.status_code == 200
    run = db.get_run(response.json()["id"])
    assert run["user_name"] == "Priya Shah"
    assert run["client_name"] == "Acme Corp"
    assert run["device_id"] == "abc-123"


def test_start_rejects_an_unauthenticated_caller(tmp_path, monkeypatch):
    """The genuine trial/payment bypass this phase closes: previously a
    caller with no session and no bearer token still got a run created
    (customer_id left NULL) and skipped app.entitlement entirely -- see
    tests/test_run_ownership.py's test_meetings_start_requires_authentication
    for the ownership-side coverage of the same fix.
    """
    _fresh_db(tmp_path, monkeypatch)

    response = client.post("/meetings/start", data={"title": "Weekly Sync"})

    assert response.status_code == 401
    assert db.list_runs() == []


def test_start_rejects_an_invalid_bearer_token(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    response = client.post(
        "/meetings/start",
        data={"title": "Weekly Sync"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401
    assert db.list_runs() == []
