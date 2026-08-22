"""Covers GET /?name=... -- added so a customer's "View Dashboard" click
(extension/popup.js, which appends its own stored user_name) only ever
shows that person's own meetings, never another customer's -- and
GET /?admin_token=... -- the owner-only unfiltered "everyone" view,
which must reject anyone who doesn't have the configured secret (e.g. a
customer who just strips ?name= off their own dashboard URL). Follows
this repo's existing convention (tests/test_cancel_endpoint.py) of
monkeypatching app.db functions rather than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)


def test_dashboard_without_name_or_token_is_blocked(monkeypatch):
    # The default-insecure case this whole gate exists to prevent: no name
    # (so it would be the unfiltered "everyone" view) and no admin_token.
    monkeypatch.setattr(settings, "admin_token", "real-secret")
    called = []
    monkeypatch.setattr(db, "list_runs", lambda user_name=None: called.append(user_name) or [])

    response = client.get("/")

    assert response.status_code == 403
    assert called == []  # never even reaches the DB


def test_dashboard_with_wrong_admin_token_is_blocked(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "real-secret")
    called = []
    monkeypatch.setattr(db, "list_runs", lambda user_name=None: called.append(user_name) or [])

    response = client.get("/", params={"admin_token": "guessed-wrong"})

    assert response.status_code == 403
    assert called == []


def test_dashboard_admin_token_not_configured_blocks_everyone(monkeypatch):
    # A misconfigured/empty ADMIN_TOKEN must fail closed, not open.
    monkeypatch.setattr(settings, "admin_token", "")

    response = client.get("/", params={"admin_token": ""})

    assert response.status_code == 403


def test_dashboard_with_correct_admin_token_shows_everyone_and_the_usage_table(monkeypatch):
    calls = []

    def fake_list_runs(user_name=None):
        calls.append(user_name)
        return []

    monkeypatch.setattr(settings, "admin_token", "real-secret")
    monkeypatch.setattr(db, "list_runs", fake_list_runs)
    monkeypatch.setattr(db, "usage_summary", lambda: [{"user_name": "Priya Shah", "today": 1, "total": 5, "last_active": "2026-01-01T00:00:00+00:00"}])

    response = client.get("/", params={"admin_token": "real-secret"})

    assert response.status_code == 200
    assert calls == [None]
    assert "Usage by person" in response.text
    assert "Priya Shah" in response.text


def test_dashboard_with_name_scopes_runs_and_hides_the_usage_table(monkeypatch):
    calls = []

    def fake_list_runs(user_name=None):
        calls.append(user_name)
        return []

    called_usage_summary = []
    monkeypatch.setattr(db, "list_runs", fake_list_runs)
    monkeypatch.setattr(db, "usage_summary", lambda: called_usage_summary.append(1) or [])

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert calls == ["Priya Shah"]
    assert called_usage_summary == []  # never even called for a scoped view
    assert "Usage by person" not in response.text
    assert "Showing meetings for" in response.text
    assert "Priya Shah" in response.text
