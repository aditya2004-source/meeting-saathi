"""Covers the on-demand document catalogue on the dashboard: each document
(MOM, Meeting Analysis, BRD, FRD, ...) shows its own ready/not_generated/
generating/failed/unavailable status independently, computed from whether its
own file exists in the meeting folder -- see app.main._available_files(),
app.main._document_statuses(), and app/docgen/registry.py. No document is
generated automatically anymore (see app.orchestrator_streaming.finalize_run(),
which now stops at facts.json). Follows this repo's existing convention
(tests/test_cancel_endpoint.py) of monkeypatching app.db functions rather than
hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.main import _available_files, app

client = TestClient(app)


def _patch_list_runs(monkeypatch, run_row):
    monkeypatch.setattr(db, "list_runs", lambda user_name=None, client_name=None, limit=50: [dict(run_row)])
    monkeypatch.setattr(db, "distinct_client_names", lambda user_name=None: [])


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

    assert "MOM.md" in result
    assert "MOM.pdf" in result
    assert "chunk_durations.json" not in result


def test_dashboard_shows_generate_buttons_for_documents_not_yet_generated(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "transcript.json").write_text("{}")
    (folder / "facts.json").write_text("{}")
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    (folder / "MOM.md").write_text("# MOM")

    run_row = {
        "id": "r1",
        "title": "Weekly Sync",
        "state": "saved",
        "folder_path": str(folder),
        "user_name": "Priya Shah",
        "client_name": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "error_message": None,
    }
    _patch_list_runs(monkeypatch, run_row)

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    # MOM already has a .pdf on disk -- ready, with a download link.
    assert "/meetings/r1/files/MOM.pdf" in response.text
    # Meeting Analysis doesn't -- a Generate button, not a download link.
    assert "/meetings/r1/files/Meeting_Analysis.pdf" not in response.text
    assert 'data-doc-key="meeting_analysis"' in response.text
    assert "Generate" in response.text


def test_dashboard_shows_download_links_for_every_ready_document(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "transcript.json").write_text("{}")
    (folder / "facts.json").write_text("{}")
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake")
    (folder / "Meeting_Analysis.pdf").write_bytes(b"%PDF-fake")

    run_row = {
        "id": "r1",
        "title": "Weekly Sync",
        "state": "saved",
        "folder_path": str(folder),
        "user_name": "Priya Shah",
        "client_name": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "error_message": None,
    }
    _patch_list_runs(monkeypatch, run_row)

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert "/meetings/r1/files/MOM.pdf" in response.text
    assert "/meetings/r1/files/Meeting_Analysis.pdf" in response.text


def test_dashboard_shows_client_name_when_set(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()

    run_row = {
        "id": "r1",
        "title": "Weekly Sync",
        "state": "saved",
        "folder_path": str(folder),
        "user_name": "Priya Shah",
        "client_name": "Acme Corp",
        "created_at": "2026-01-01T00:00:00+00:00",
        "error_message": None,
    }
    _patch_list_runs(monkeypatch, run_row)

    response = client.get("/", params={"name": "Priya Shah"})

    assert response.status_code == 200
    assert "Acme Corp" in response.text
