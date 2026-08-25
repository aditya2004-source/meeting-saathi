"""Covers POST /meetings/start's wiring -- user_name/client_name/device_id are
threaded straight through to db.create_run(). The daily-limit gate that used to
live here was removed entirely (see app/main.py) -- this is currently a
self-use/testing deployment, not a shared one with a quota to protect. Follows
this repo's existing convention (tests/test_cancel_endpoint.py) of
monkeypatching app.db functions rather than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)


def test_start_allows_a_first_time_user_with_no_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    monkeypatch.setattr(
        db,
        "create_run",
        lambda title, audio_path, user_name="", client_name="", device_id="": {"id": "r1", "state": "idle"},
    )
    monkeypatch.setattr(db, "update_run", lambda run_id, **fields: {"id": run_id, "state": fields.get("state")})

    response = client.post("/meetings/start", data={"title": "Weekly Sync"})

    assert response.status_code == 200
    assert response.json() == {"id": "r1", "state": "received"}


def test_start_threads_client_name_and_device_id_to_create_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "working_dir", tmp_path)
    seen = {}

    def fake_create_run(title, audio_path, user_name="", client_name="", device_id=""):
        seen["user_name"] = user_name
        seen["client_name"] = client_name
        seen["device_id"] = device_id
        return {"id": "r2", "state": "idle"}

    monkeypatch.setattr(db, "create_run", fake_create_run)
    monkeypatch.setattr(db, "update_run", lambda run_id, **fields: {"id": run_id, "state": fields.get("state")})

    response = client.post(
        "/meetings/start",
        data={"title": "Weekly Sync", "user_name": "Priya Shah", "client_name": "Acme Corp", "device_id": "abc-123"},
    )

    assert response.status_code == 200
    assert seen == {"user_name": "Priya Shah", "client_name": "Acme Corp", "device_id": "abc-123"}
