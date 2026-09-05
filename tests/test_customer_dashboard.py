"""Covers Phase 5's real customer_id scoping on GET /dashboard: a logged-in
customer (real session, from Phase 1's OTP login) sees only their own
meetings -- genuinely secure, unlike the `?name=` path this coexists with
(kept for a caller with no session at all, see auth.authorize_run_access's
docstring for why). Also covers the free-trial/plan status banner and the
/account page.
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


def _verified_customer(email: str, name: str = "Priya Shah") -> dict:
    db.create_customer(name=name, email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _logged_in_client(customer: dict) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    code = auth.issue_otp(customer["email"])
    c.post("/auth/verify-otp", data={"email": customer["email"], "code": code})
    return c


def test_dashboard_shows_only_the_logged_in_customers_own_meetings(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    other = _verified_customer("other@example.com")
    db.create_run(title="My meeting", audio_path="", customer_id=owner["id"])
    db.create_run(title="Someone else's meeting", audio_path="", customer_id=other["id"])

    session = _logged_in_client(owner)
    response = session.get("/dashboard")

    assert response.status_code == 200
    assert "My meeting" in response.text
    assert "Someone else's meeting" not in response.text


def test_dashboard_shows_free_trial_usage_when_no_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    db.increment_free_meetings_used(owner["id"])

    session = _logged_in_client(owner)
    response = session.get("/dashboard")

    assert "1 of" in response.text and str(settings.free_meeting_allowance) in response.text


def test_dashboard_shows_upgrade_banner_when_trial_exhausted(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "free_meeting_allowance", 1)
    owner = _verified_customer("owner@example.com")
    db.increment_free_meetings_used(owner["id"])

    session = _logged_in_client(owner)
    response = session.get("/dashboard")

    assert "Upgrade to keep recording" in response.text


def test_dashboard_shows_active_plan_name_when_subscribed(tmp_path, monkeypatch):
    import uuid

    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            """INSERT INTO subscriptions (id, customer_id, plan, currency, status, created_at, updated_at)
               VALUES (?, ?, 'monthly', 'INR', 'active', datetime('now'), datetime('now'))""",
            (str(uuid.uuid4()), owner["id"]),
        )

    session = _logged_in_client(owner)
    response = session.get("/dashboard")

    assert "Monthly" in response.text
    assert "(active)" in response.text


def test_anonymous_name_scoping_still_works_without_a_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.create_run(title="Legacy meeting", audio_path="", user_name="Priya Shah")

    response = client.get("/dashboard", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert "Legacy meeting" in response.text


def test_account_page_requires_login(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    fresh_client = TestClient(app, base_url="https://testserver")

    response = fresh_client.get("/account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/signup"


def test_account_page_shows_customer_details_when_logged_in(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com", name="Priya Shah")

    session = _logged_in_client(owner)
    response = session.get("/account")

    assert response.status_code == 200
    assert "Priya Shah" in response.text
    assert "owner@example.com" in response.text
