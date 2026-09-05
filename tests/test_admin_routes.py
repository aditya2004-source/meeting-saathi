"""Covers app/admin_routes.py's access control (same wrong-slug-404 /
must-be-logged-in-as-admin pattern as tests/test_admin_login.py) and the
mutation actions (block/unblock a customer, add a fixed cost, resolve
feedback).
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")

_SLUG = "test-admin-slug-xyz"
_USERNAME = "test-admin"
_PASSWORD = "test-admin-password"

_ADMIN_PAGES = [
    "/overview",
    "/customers",
    "/subscriptions",
    "/usage",
    "/expenses",
    "/admin-feedback",
]


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _patch_admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_url_slug", _SLUG)
    monkeypatch.setattr(settings, "admin_username", _USERNAME)
    monkeypatch.setattr(settings, "admin_password", _PASSWORD)


def _admin_session_client() -> TestClient:
    session = TestClient(app, base_url="https://testserver")
    session.post(f"/{_SLUG}/login", data={"username": _USERNAME, "password": _PASSWORD})
    return session


def test_wrong_slug_404s_on_every_admin_analytics_page(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    for page in _ADMIN_PAGES:
        assert client.get(f"/wrong-slug{page}").status_code == 404


def test_admin_pages_redirect_to_login_without_a_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    fresh_client = TestClient(app, base_url="https://testserver")
    for page in _ADMIN_PAGES:
        response = fresh_client.get(f"/{_SLUG}{page}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/{_SLUG}/login"


def test_admin_pages_render_with_a_valid_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    session = _admin_session_client()
    for page in _ADMIN_PAGES:
        response = session.get(f"/{_SLUG}{page}")
        assert response.status_code == 200, page


def test_block_and_unblock_customer(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    customer = db.create_customer(name="Priya", email="priya@example.com")
    session = _admin_session_client()

    block = session.post(f"/{_SLUG}/customers/{customer['id']}/status", data={"status": "blocked"}, follow_redirects=False)
    assert block.status_code == 303
    assert db.get_customer(customer["id"])["status"] == "blocked"

    unblock = session.post(f"/{_SLUG}/customers/{customer['id']}/status", data={"status": "active"}, follow_redirects=False)
    assert unblock.status_code == 303
    assert db.get_customer(customer["id"])["status"] == "active"


def test_customer_status_mutation_requires_admin_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    customer = db.create_customer(name="Priya", email="priya@example.com")
    fresh_client = TestClient(app, base_url="https://testserver")

    response = fresh_client.post(f"/{_SLUG}/customers/{customer['id']}/status", data={"status": "blocked"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/{_SLUG}/login"
    assert db.get_customer(customer["id"])["status"] == "active"


def test_add_fixed_cost_persists(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    session = _admin_session_client()

    response = session.post(
        f"/{_SLUG}/expenses/fixed-costs",
        data={"effective_month": "2026-09", "amount_inr": "1500.50", "note": "VPS"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    costs = db.list_fixed_costs()
    assert len(costs) == 1
    assert costs[0]["amount_inr"] == 1500.50
    assert costs[0]["note"] == "VPS"


def test_resolve_feedback_persists(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_admin_settings(monkeypatch)
    feedback = db.create_feedback(message="Speaker names were wrong", category="bug")
    session = _admin_session_client()

    response = session.post(f"/{_SLUG}/admin-feedback/{feedback['id']}/resolve", follow_redirects=False)

    assert response.status_code == 303
    assert db.list_feedback()[0]["status"] == "resolved"
