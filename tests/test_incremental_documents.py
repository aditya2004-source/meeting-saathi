"""Covers incremental document delivery on the dashboard: a document (MOM
or Meeting Analysis) becomes downloadable/visible the moment its own file
exists in the meeting folder, without waiting for its sibling or for the
run to reach state "saved" -- see app.main._available_files() and
app.orchestrator_streaming.finalize_run()'s per-document write-as-ready
flow. Follows this repo's existing convention (tests/test_cancel_endpoint.py)
of monkeypatching app.db functions rather than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.main import _available_files, app

client = TestClient(app)


def test_available_files_empty_when_no_folder_path():
    assert _available_files(None) == []


def test_available_files_empty_when_folder_does_not_exist(tmp_path):
    assert _available_files(str(tmp_path / "does-not-exist")) == []


def test_available_files_lists_only_what_actually_exists(tmp_path):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    (folder / "MOM.md").write_text("# MOM")
    # Not on the allowlist -- must never show up, even if present on disk.
    (folder / "chunk_durations.json").write_text("{}")

    result = _available_files(str(folder))

    assert result == ["MOM.md", "MOM.pdf"]


def test_dashboard_shows_partial_progress_for_a_run_still_generating(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    (folder / "MOM.md").write_text("# MOM")

    run_row = {
        "id": "r1",
        "title": "Weekly Sync",
        "state": "generating_docs",
        "folder_path": str(folder),
        "user_name": "Priya Shah",
        "created_at": "2026-01-01T00:00:00+00:00",
        "error_message": None,
    }
    monkeypatch.setattr(db, "list_runs", lambda user_name=None: [dict(run_row)])

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert "1 of 2 documents ready so far" in response.text
    assert "/meetings/r1/files/MOM.pdf" in response.text
    assert "/meetings/r1/files/MOM.md" in response.text
    assert "/meetings/r1/files/Meeting_Analysis.pdf" not in response.text


def test_dashboard_shows_full_completion_once_saved(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    (folder / "Meeting_Analysis.pdf").write_bytes(b"%PDF-fake")

    run_row = {
        "id": "r1",
        "title": "Weekly Sync",
        "state": "saved",
        "folder_path": str(folder),
        "user_name": "Priya Shah",
        "created_at": "2026-01-01T00:00:00+00:00",
        "error_message": None,
    }
    monkeypatch.setattr(db, "list_runs", lambda user_name=None: [dict(run_row)])

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert "Documents ready" in response.text
    assert "of 2 documents ready so far" not in response.text
    assert "/meetings/r1/files/MOM.pdf" in response.text
    assert "/meetings/r1/files/Meeting_Analysis.pdf" in response.text
