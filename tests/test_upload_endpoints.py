"""Covers the daily-limit check on /meetings/upload and
/meetings/upload-form -- these are fallback/manual upload paths (used by
the dashboard's "testing / fallback" form) that used to have NO limit
check at all, unlike /meetings/start. That made them an easy way to
bypass settings.daily_meeting_limit entirely. Follows this repo's
existing convention (tests/test_cancel_endpoint.py) of monkeypatching
app.db functions and app.main's own helpers rather than exercising the
real transcription pipeline.
"""
import app.main as main_module
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)

_AUDIO_FILE = {"audio": ("test.webm", b"fake audio bytes", "audio/webm")}


def test_upload_refuses_when_over_daily_limit(monkeypatch):
    monkeypatch.setattr(settings, "daily_meeting_limit", 3)
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 3)
    called = []
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: called.append((a, k)))

    response = client.post(
        "/meetings/upload", data={"title": "Weekly Sync", "user_name": "Priya Shah"}, files=_AUDIO_FILE
    )

    assert response.status_code == 429
    assert response.json()["error"] == "daily_limit_reached"
    assert called == []


def test_upload_allows_when_under_daily_limit(monkeypatch):
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 1)
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post(
        "/meetings/upload", data={"title": "Weekly Sync", "user_name": "Priya Shah"}, files=_AUDIO_FILE
    )

    assert response.status_code == 200
    assert response.json() == {"id": "r1", "state": "received"}


def test_upload_allows_empty_user_name_without_checking_limit(monkeypatch):
    def _fail_if_called(user_name):
        raise AssertionError("count_runs_today() must not be called for an empty user_name")

    monkeypatch.setattr(db, "count_runs_today", _fail_if_called)
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post("/meetings/upload", data={"title": "Weekly Sync"}, files=_AUDIO_FILE)

    assert response.status_code == 200


def test_upload_form_redirects_to_limit_reached_when_over_limit(monkeypatch):
    monkeypatch.setattr(settings, "daily_meeting_limit", 3)
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 5)
    called = []
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: called.append((a, k)))

    response = client.post(
        "/meetings/upload-form",
        data={"title": "Weekly Sync", "user_name": "Priya Shah"},
        files=_AUDIO_FILE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?limit_reached=1"
    assert called == []


def test_upload_form_redirects_normally_when_under_limit(monkeypatch):
    monkeypatch.setattr(db, "count_runs_today", lambda user_name: 0)
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post(
        "/meetings/upload-form",
        data={"title": "Weekly Sync", "user_name": "Priya Shah"},
        files=_AUDIO_FILE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
