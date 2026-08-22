"""Covers POST /debug/log -- a fire-and-forget diagnostic breadcrumb channel
for the extension (background.js/offscreen.js), added to get real,
timestamped visibility into the recording start path without depending on
manually retrieving console output from the two Chrome contexts (the
offscreen document, the service worker) neither browser automation nor
manual copy-paste could reliably reach during a live debugging session.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_debug_log_accepts_a_breadcrumb():
    response = client.post(
        "/debug/log",
        data={"source": "offscreen", "event": "startRecording:begin", "detail": "runId=abc123"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_debug_log_defaults_detail_to_empty_string():
    response = client.post("/debug/log", data={"source": "background", "event": "startRecording:success"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
