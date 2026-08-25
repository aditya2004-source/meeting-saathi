"""Covers /meetings/upload and /meetings/upload-form's wiring -- the
daily-limit gate that used to live on these routes was removed entirely (see
app/main.py); this is currently a self-use/testing deployment, not a shared
one with a quota to protect. Follows this repo's existing convention
(tests/test_cancel_endpoint.py) of monkeypatching app.main's own helpers
rather than exercising the real transcription pipeline.
"""
import app.main as main_module
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_AUDIO_FILE = {"audio": ("test.webm", b"fake audio bytes", "audio/webm")}


def test_upload_starts_processing_and_returns_run_id(monkeypatch):
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post(
        "/meetings/upload", data={"title": "Weekly Sync", "user_name": "Priya Shah"}, files=_AUDIO_FILE
    )

    assert response.status_code == 200
    assert response.json() == {"id": "r1", "state": "received"}


def test_upload_threads_client_name_to_start_processing(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        main_module,
        "_start_processing",
        lambda title, content, filename, speaker_events=None, attendee_roster=None, user_name="", client_name="", device_id="": (
            seen.update(user_name=user_name, client_name=client_name, device_id=device_id)
            or {"id": "r1", "state": "received"}
        ),
    )

    response = client.post(
        "/meetings/upload",
        data={"title": "Weekly Sync", "user_name": "Priya Shah", "client_name": "Acme Corp", "device_id": "abc-123"},
        files=_AUDIO_FILE,
    )

    assert response.status_code == 200
    assert seen == {"user_name": "Priya Shah", "client_name": "Acme Corp", "device_id": "abc-123"}


def test_upload_allows_empty_user_name(monkeypatch):
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post("/meetings/upload", data={"title": "Weekly Sync"}, files=_AUDIO_FILE)

    assert response.status_code == 200


def test_upload_form_redirects_home(monkeypatch):
    monkeypatch.setattr(main_module, "_start_processing", lambda *a, **k: {"id": "r1", "state": "received"})

    response = client.post(
        "/meetings/upload-form",
        data={"title": "Weekly Sync", "user_name": "Priya Shah"},
        files=_AUDIO_FILE,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
