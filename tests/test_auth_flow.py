"""Covers Phase 1's customer identity flow: signup -> consent -> OTP ->
session, and the extension device-token pairing endpoint. Follows this
repo's existing convention (tests/test_admin_login.py) of a real TestClient
against the real app, with app.db pointed at a throwaway sqlite file per
test rather than mocking the DB layer -- this phase's whole point is
end-to-end correctness of the auth wiring, so exercising the real db.py
functions (not a monkeypatched stand-in) is the right level here.
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


def _signup_and_consent(email: str, name: str = "Priya Shah") -> None:
    response = client.post("/auth/signup", data={"name": name, "email": email})
    assert response.status_code == 200
    response = client.post("/auth/consent", data={"email": email})
    assert response.status_code == 200


def test_signup_is_idempotent_on_email(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"

    first = client.post("/auth/signup", data={"name": "Priya", "email": email})
    second = client.post("/auth/signup", data={"name": "Priya Shah", "email": email})

    assert first.status_code == second.status_code == 200
    # Same customer row both times (a second signup for the same email
    # didn't create a duplicate) -- the row's own id is stable, and its name
    # still reflects the *first* signup, not silently overwritten by the
    # second.
    customer = db.get_customer_by_email(email)
    assert customer is not None
    assert customer["name"] == "Priya"


def test_send_otp_requires_consent_for_a_brand_new_email(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "new-user@example.com"
    client.post("/auth/signup", data={"name": "New User", "email": email})

    response = client.post("/auth/send-otp", data={"email": email})

    assert response.status_code == 400
    assert response.json()["detail"] == "consent_required"


def test_full_signup_consent_otp_flow_sets_session_and_verifies_email(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)

    otp_response = client.post("/auth/send-otp", data={"email": email})
    assert otp_response.status_code == 200

    # The raw code is only ever emailed (or logged, in dev-mode fallback),
    # never returned over HTTP -- issue a second one directly via app.auth
    # (same underlying otp_codes row app.auth.verify_otp checks against) to
    # get a code this test can actually use, rather than scraping logs.
    code = auth.issue_otp(email)

    wrong = client.post("/auth/verify-otp", data={"email": email, "code": "000000"})
    assert wrong.status_code == 400

    right = client.post("/auth/verify-otp", data={"email": email, "code": code})
    assert right.status_code == 200
    customer = db.get_customer_by_email(email)
    assert customer["email_verified"] == 1

    me = client.get("/account/me")
    assert me.status_code == 200
    assert me.json()["email"] == email

    client.post("/auth/logout")
    assert client.get("/account/me").status_code == 401


def test_otp_cannot_be_replayed_after_use(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)
    code = auth.issue_otp(email)

    first = client.post("/auth/verify-otp", data={"email": email, "code": code})
    assert first.status_code == 200
    client.post("/auth/logout")

    replay = client.post("/auth/verify-otp", data={"email": email, "code": code})
    assert replay.status_code == 400


def test_otp_locks_out_after_max_attempts(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)
    code = auth.issue_otp(email)

    for _ in range(auth.OTP_MAX_ATTEMPTS):
        response = client.post("/auth/verify-otp", data={"email": email, "code": "000000"})
        assert response.status_code == 400

    # Even the correct code no longer works once attempts are exhausted.
    final = client.post("/auth/verify-otp", data={"email": email, "code": code})
    assert final.status_code == 400


def test_send_otp_is_rate_limited(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)

    for _ in range(auth.OTP_MAX_REQUESTS_PER_WINDOW):
        response = client.post("/auth/send-otp", data={"email": email})
        assert response.status_code == 200

    limited = client.post("/auth/send-otp", data={"email": email})
    assert limited.status_code == 429


def test_send_otp_reports_success_only_when_the_email_actually_sends(tmp_path, monkeypatch):
    """Production-audit fix: /auth/send-otp must check send_email()'s
    return value rather than always answering {"ok": True} -- otherwise a
    customer is told a code was sent when Resend actually failed to
    deliver it.
    """
    from unittest.mock import patch

    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)

    with patch("app.auth_routes.send_email", return_value=True) as mock_send:
        response = client.post("/auth/send-otp", data={"email": email})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_send.assert_called_once()


def test_send_otp_returns_a_safe_generic_error_when_the_email_fails_to_send(tmp_path, monkeypatch):
    from unittest.mock import patch

    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)

    with patch("app.auth_routes.send_email", return_value=False):
        response = client.post("/auth/send-otp", data={"email": email})

    assert response.status_code == 502
    body = response.json()
    assert body["detail"] == "email_send_failed"
    # Never leak the provider name, an exception message, or the OTP itself
    # into the response the customer's browser receives.
    body_text = response.text.lower()
    for leaked_term in ("resend", "traceback", "exception", "api key"):
        assert leaked_term not in body_text


def test_returning_verified_customer_does_not_need_to_reconsent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)
    code = auth.issue_otp(email)
    client.post("/auth/verify-otp", data={"email": email, "code": code})
    client.post("/auth/logout")

    # No new consent call -- a second login for an already-verified customer
    # must not be blocked by the first-time consent gate.
    response = client.post("/auth/send-otp", data={"email": email})
    assert response.status_code == 200


def test_device_token_pairing_authenticates_bearer_requests(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    email = "priya@example.com"
    _signup_and_consent(email)
    code = auth.issue_otp(email)
    client.post("/auth/verify-otp", data={"email": email, "code": code})

    token_response = client.post("/account/connect/device-token")
    assert token_response.status_code == 200
    token = token_response.json()["token"]
    client.post("/auth/logout")

    # No session cookie now -- only the bearer token proves identity.
    unauthenticated_client = TestClient(app, base_url="https://testserver")
    me = unauthenticated_client.get("/account/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


def test_device_token_creation_requires_authentication(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fresh_client = TestClient(app, base_url="https://testserver")

    response = fresh_client.post("/account/connect/device-token")

    assert response.status_code == 401
