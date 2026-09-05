"""Covers Phase 8's account/data-deletion endpoints -- real cascading
effects, not a toast that does nothing. Follows tests/test_auth_flow.py's
convention of a real TestClient against real app.db functions.
"""
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _verified_customer(email: str) -> dict:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _logged_in_client(customer: dict) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    code = auth.issue_otp(customer["email"])
    c.post("/auth/verify-otp", data={"email": customer["email"], "code": code})
    return c


def test_delete_meeting_data_removes_folder_but_keeps_the_row(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    folder = tmp_path / "Weekly Sync - 2026-09-05 1200"
    folder.mkdir()
    (folder / "transcript.txt").write_text("secret transcript")
    run = db.create_run(title="Weekly Sync", audio_path="", customer_id=owner["id"])
    db.update_run(run["id"], state="saved", folder_path=str(folder), duration_seconds=600.0)

    session = _logged_in_client(owner)
    response = session.post("/account/delete-meeting-data")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "meetings_cleared": 1}
    assert not folder.exists()
    surviving_row = db.get_run(run["id"])
    assert surviving_row is not None  # row kept for historical analytics
    assert surviving_row["folder_path"] is None
    assert surviving_row["state"] == "saved"
    assert surviving_row["duration_seconds"] == 600.0


def test_delete_meeting_data_requires_login(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fresh_client = TestClient(app, base_url="https://testserver")

    response = fresh_client.post("/account/delete-meeting-data")

    assert response.status_code == 401


def test_delete_account_revokes_device_tokens(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    token = auth.issue_device_token(owner["id"], label="test")
    session = _logged_in_client(owner)

    response = session.post("/account/delete-account")

    assert response.status_code == 200
    unauthenticated_client = TestClient(app, base_url="https://testserver")
    me = unauthenticated_client.get("/account/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401


def test_delete_account_clears_the_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    session = _logged_in_client(owner)

    session.post("/account/delete-account")

    assert session.get("/account/me").status_code == 401


def test_delete_account_marks_customer_inactive(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    session = _logged_in_client(owner)

    session.post("/account/delete-account")

    assert db.get_customer(owner["id"])["status"] == "deleted"


def test_delete_account_cancels_an_active_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    db.create_pending_subscription(owner["id"], "monthly", "INR", "sub_test123")
    db.update_subscription_status("sub_test123", status="active")
    session = _logged_in_client(owner)

    with patch("app.auth_routes.razorpay_client.cancel_subscription", return_value={}) as mock_cancel:
        response = session.post("/account/delete-account")

    assert response.status_code == 200
    mock_cancel.assert_called_once_with("sub_test123")
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "cancelled"


def test_delete_account_succeeds_even_if_razorpay_cancel_call_fails(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    db.create_pending_subscription(owner["id"], "monthly", "INR", "sub_test123")
    db.update_subscription_status("sub_test123", status="active")
    session = _logged_in_client(owner)

    with patch("app.auth_routes.razorpay_client.cancel_subscription", side_effect=RuntimeError("network error")):
        response = session.post("/account/delete-account")

    assert response.status_code == 200
    assert db.get_customer(owner["id"])["status"] == "deleted"
