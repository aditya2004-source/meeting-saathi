"""Covers the admin dashboard's new gate: an unguessable URL path
(settings.admin_url_slug) plus a real username/password login and a signed
session cookie -- replacing the old shared-secret-in-a-query-string
(?admin_token=...) scheme (see tests/test_dashboard_scoping.py for what
remains of the customer-facing ?name= view). A wrong slug must 404 exactly
like a route that doesn't exist -- never reveal "wrong password", which
would confirm admin functionality lives there at all. Follows this repo's
existing convention (tests/test_cancel_endpoint.py) of monkeypatching
app.db functions rather than hitting the real sqlite file.

Every TestClient here is built with base_url="https://testserver" -- the
session cookie is Secure (settings.session_cookie_https_only, matching how
this app is actually accessed in production, over the Cloudflare Tunnel's
https:// URL), and httpx's cookie jar -- correctly -- won't send a Secure
cookie back over the plain-http default TestClient origin, so login would
otherwise appear to "not stick" here even though it works fine for real.
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")

_SLUG = "test-admin-slug-xyz"
_USERNAME = "test-admin"
_PASSWORD = "test-admin-password"


def _patch_admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_url_slug", _SLUG)
    monkeypatch.setattr(settings, "admin_username", _USERNAME)
    monkeypatch.setattr(settings, "admin_password", _PASSWORD)


def test_wrong_slug_404s_on_all_three_routes(monkeypatch):
    _patch_admin_settings(monkeypatch)

    assert client.get("/wrong-slug/login").status_code == 404
    assert client.post("/wrong-slug/login", data={"username": "x", "password": "y"}).status_code == 404
    assert client.get("/wrong-slug/dashboard").status_code == 404
    assert client.get("/wrong-slug/logout").status_code == 404


def test_correct_slug_get_login_shows_the_form(monkeypatch):
    _patch_admin_settings(monkeypatch)

    response = client.get(f"/{_SLUG}/login")

    assert response.status_code == 200
    assert "username" in response.text.lower()
    assert "password" in response.text.lower()


def test_post_correct_credentials_sets_session_and_redirects_to_dashboard(monkeypatch):
    _patch_admin_settings(monkeypatch)
    monkeypatch.setattr(db, "list_runs", lambda user_name=None, client_name=None: [])
    monkeypatch.setattr(db, "usage_summary", lambda: [])
    monkeypatch.setattr(db, "distinct_client_names", lambda user_name=None: [])

    fresh_client = TestClient(app, base_url="https://testserver")
    response = fresh_client.post(
        f"/{_SLUG}/login",
        data={"username": _USERNAME, "password": _PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/{_SLUG}/dashboard"
    assert "session" in fresh_client.cookies

    dashboard = fresh_client.get(f"/{_SLUG}/dashboard")
    assert dashboard.status_code == 200


def test_post_wrong_credentials_redirects_back_with_no_session(monkeypatch):
    _patch_admin_settings(monkeypatch)

    fresh_client = TestClient(app, base_url="https://testserver")
    response = fresh_client.post(
        f"/{_SLUG}/login",
        data={"username": _USERNAME, "password": "wrong-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/{_SLUG}/login?error=1"
    assert "session" not in fresh_client.cookies


def test_dashboard_without_session_redirects_to_login(monkeypatch):
    _patch_admin_settings(monkeypatch)

    fresh_client = TestClient(app, base_url="https://testserver")
    response = fresh_client.get(f"/{_SLUG}/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/{_SLUG}/login"


def test_dashboard_with_valid_session_shows_usage_table_and_all_runs(monkeypatch):
    _patch_admin_settings(monkeypatch)
    calls = []

    def fake_list_runs(user_name=None, client_name=None):
        calls.append(user_name)
        return []

    monkeypatch.setattr(db, "list_runs", fake_list_runs)
    monkeypatch.setattr(db, "distinct_client_names", lambda user_name=None: [])
    monkeypatch.setattr(
        db,
        "usage_summary",
        lambda: [{"user_name": "Priya Shah", "today": 1, "total": 5, "last_active": "2026-01-01T00:00:00+00:00"}],
    )

    fresh_client = TestClient(app, base_url="https://testserver")
    fresh_client.post(
        f"/{_SLUG}/login",
        data={"username": _USERNAME, "password": _PASSWORD},
        follow_redirects=False,
    )

    response = fresh_client.get(f"/{_SLUG}/dashboard")

    assert response.status_code == 200
    assert calls == [None]
    assert "Usage by person" in response.text
    assert "Priya Shah" in response.text


def test_logout_clears_session_so_dashboard_redirects_to_login_again(monkeypatch):
    _patch_admin_settings(monkeypatch)
    monkeypatch.setattr(db, "list_runs", lambda user_name=None, client_name=None: [])
    monkeypatch.setattr(db, "usage_summary", lambda: [])
    monkeypatch.setattr(db, "distinct_client_names", lambda user_name=None: [])

    fresh_client = TestClient(app, base_url="https://testserver")
    fresh_client.post(f"/{_SLUG}/login", data={"username": _USERNAME, "password": _PASSWORD})
    assert fresh_client.get(f"/{_SLUG}/dashboard").status_code == 200

    logout_response = fresh_client.get(f"/{_SLUG}/logout", follow_redirects=False)
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == f"/{_SLUG}/login"

    dashboard_after_logout = fresh_client.get(f"/{_SLUG}/dashboard", follow_redirects=False)
    assert dashboard_after_logout.status_code == 303
    assert dashboard_after_logout.headers["location"] == f"/{_SLUG}/login"
