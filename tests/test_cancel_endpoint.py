"""Covers POST /meetings/{run_id}/cancel -- added so a recording-start
failure on the extension side (after /meetings/start already created a DB
row) has somewhere to report "this run never actually started" instead of
leaving the row stuck at state=received forever. Follows this repo's
existing convention (tests/test_orchestrator_streaming_finalize.py) of
monkeypatching app.db functions rather than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_cancel_marks_existing_run_failed(monkeypatch):
    run_row = {"id": "abc123", "title": "Test Meeting", "state": "received"}
    calls = []

    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row) if rid == "abc123" else None)

    def fake_mark_failed(rid, error):
        calls.append((rid, str(error)))
        run_row["state"] = "failed"
        run_row["error_message"] = str(error)
        return dict(run_row)

    monkeypatch.setattr(db, "mark_failed", fake_mark_failed)

    response = client.post("/meetings/abc123/cancel")

    assert response.status_code == 200
    assert response.json() == {"id": "abc123", "state": "failed"}
    assert calls == [("abc123", "Recording was cancelled before any audio was captured")]


def test_cancel_uses_the_provided_reason_when_given(monkeypatch):
    # Lets the dashboard distinguish "never started" from "started but
    # failed to upload" (see extension/background.js's two call sites)
    # instead of both landing on the same generic message.
    run_row = {"id": "abc123", "title": "Test Meeting", "state": "received"}
    calls = []

    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row) if rid == "abc123" else None)

    def fake_mark_failed(rid, error):
        calls.append((rid, str(error)))
        return dict(run_row)

    monkeypatch.setattr(db, "mark_failed", fake_mark_failed)

    response = client.post("/meetings/abc123/cancel", data={"reason": "Recording never started: some real error"})

    assert response.status_code == 200
    assert calls == [("abc123", "Recording never started: some real error")]


def test_cancel_unknown_run_returns_404(monkeypatch):
    monkeypatch.setattr(db, "get_run", lambda rid: None)
    called = []
    monkeypatch.setattr(db, "mark_failed", lambda rid, error: called.append(rid))

    response = client.post("/meetings/does-not-exist/cancel")

    assert response.status_code == 404
    assert called == []
