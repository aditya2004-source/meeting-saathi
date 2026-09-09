"""Covers GET /dashboard's real customer_id scoping. Moved from `/` to
`/dashboard` in Phase 4 (`/` is now the public marketing landing page, see
app/site_routes.py). The old unfiltered "everyone" view (previously
?admin_token=...) has moved to /{admin_url_slug}/dashboard behind a real
login -- see tests/test_admin_login.py. Follows this repo's existing
convention (tests/test_cancel_endpoint.py) of monkeypatching app.db
functions rather than hitting the real sqlite file.
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


def test_dashboard_scopes_by_real_customer_id_ignoring_any_name_param(tmp_path, monkeypatch):
    """Production-simplification: the anonymous ?name=... fallback is gone
    (see tests/test_customer_dashboard.py's redirect-to-login test) -- a
    logged-in customer's own real customer_id is the only thing that ever
    scopes this view now, even if a stale extension popup build still
    appends its own ?name=... (harmless, silently ignored).
    """
    _fresh_db(tmp_path, monkeypatch)
    db.create_customer(name="Priya Shah", email="priya@example.com")
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email="priya@example.com", policy_type=policy, policy_version="test")
    customer = db.update_customer(db.get_customer_by_email("priya@example.com")["id"], email_verified=1)
    code = auth.issue_otp("priya@example.com")
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/verify-otp", data={"email": "priya@example.com", "code": code})

    calls = []

    def fake_list_runs(limit=50, user_name=None, client_name=None, customer_id=None):
        calls.append({"user_name": user_name, "customer_id": customer_id})
        return []

    monkeypatch.setattr(db, "list_runs", fake_list_runs)
    monkeypatch.setattr(db, "distinct_client_names", lambda **kwargs: [])

    response = session.get("/dashboard", params={"name": "Someone Else Entirely"})

    assert response.status_code == 200
    assert calls == [{"user_name": None, "customer_id": customer["id"]}]
