"""Covers POST /meetings/start's daily-limit gate -- added so a person
sharing this with BA testers can't have one over-eager tester burn the
whole team's shared Gemini quota. Follows this repo's existing convention
(tests/test_cancel_endpoint.py) of monkeypatching app.db functions rather
than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("count_runs_today() must not be called for an empty user_name")


def test_start_allows_a_first_time_user_with_no_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    monkeypatch.setattr(db, "count_runs_today", _fail_if_called)
    monkeypatch.setattr(
        db, "create_run", lambda title, audio_path, user_name="": {"id": "r1", "state": "idle"}
    )
    monkeypatch.setattr(db, "update_run", lambda run_id, **fields: {"id": run_id, "state": fields.get("state")})

    response = client.post("/meetings/start", data={"title": "Weekly Sync"})

    assert response.status_code == 200
    assert response.json() == {"id": "r1", "state": "received"}


def test_start_allows_a_named_user_under_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 2)
    monkeypatch.setattr(
        db, "create_run", lambda title, audio_path, user_name="": {"id": "r2", "state": "idle"}
    )
    monkeypatch.setattr(db, "update_run", lambda run_id, **fields: {"id": run_id, "state": fields.get("state")})

    response = client.post("/meetings/start", data={"title": "Weekly Sync", "user_name": "Priya Shah"})

    assert response.status_code == 200
    assert response.json() == {"id": "r2", "state": "received"}


def test_start_refuses_a_named_user_at_the_limit(monkeypatch):
    monkeypatch.setattr(settings, "daily_meeting_limit", 3)
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 3)
    called = []
    monkeypatch.setattr(db, "create_run", lambda *a, **k: called.append((a, k)))

    response = client.post("/meetings/start", data={"title": "Weekly Sync", "user_name": "Priya Shah"})

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "daily_limit_reached"
    assert called == []
