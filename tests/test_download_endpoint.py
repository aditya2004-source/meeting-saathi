"""Covers GET /meetings/{run_id}/files/{filename} -- added for remote
deployments (Railway) where nobody has direct filesystem access to a
saved meeting's folder_path the way a local-machine user does. Follows
this repo's existing convention (tests/test_cancel_endpoint.py) of
monkeypatching app.db functions rather than hitting the real sqlite file.
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_download_serves_an_allowlisted_file(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "MOM.pdf").write_bytes(b"%PDF-fake-content")

    run_row = {"id": "r1", "state": "saved", "folder_path": str(folder)}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row) if rid == "r1" else None)

    response = client.get("/meetings/r1/files/MOM.pdf")

    assert response.status_code == 200
    assert response.content == b"%PDF-fake-content"


def test_download_rejects_a_filename_outside_the_allowlist(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "Requirement_Gathering_Sheet.pdf").write_bytes(b"should never be served")

    run_row = {"id": "r1", "state": "saved", "folder_path": str(folder)}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    response = client.get("/meetings/r1/files/Requirement_Gathering_Sheet.pdf")

    assert response.status_code == 404


def test_download_rejects_path_traversal(monkeypatch):
    run_row = {"id": "r1", "state": "saved", "folder_path": "/some/real/folder"}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    response = client.get("/meetings/r1/files/..%2F..%2F..%2Fetc%2Fpasswd")

    assert response.status_code == 404


def test_download_404s_for_unknown_run(monkeypatch):
    monkeypatch.setattr(db, "get_run", lambda rid: None)

    response = client.get("/meetings/does-not-exist/files/MOM.pdf")

    assert response.status_code == 404


def test_download_404s_when_no_folder_exists_yet(monkeypatch):
    run_row = {"id": "r1", "state": "generating_docs", "folder_path": None}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    response = client.get("/meetings/r1/files/MOM.pdf")

    assert response.status_code == 404


def test_download_serves_a_file_even_before_the_run_is_saved(tmp_path, monkeypatch):
    # The whole point of incremental delivery: MOM can be ready and
    # downloadable while Meeting Analysis is still generating -- the run's
    # folder_path is set (see app.orchestrator_streaming.finalize_run())
    # long before state ever reaches "saved".
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()
    (folder / "MOM.pdf").write_bytes(b"%PDF-mom-only")

    run_row = {"id": "r1", "state": "generating_docs", "folder_path": str(folder)}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    mom_response = client.get("/meetings/r1/files/MOM.pdf")
    analysis_response = client.get("/meetings/r1/files/Meeting_Analysis.pdf")

    assert mom_response.status_code == 200
    assert mom_response.content == b"%PDF-mom-only"
    assert analysis_response.status_code == 404  # not written yet -- correctly not ready


def test_download_404s_when_the_file_is_missing_on_disk(tmp_path, monkeypatch):
    folder = tmp_path / "Weekly Sync - 2026-08-22 1200"
    folder.mkdir()  # MOM.pdf never actually written

    run_row = {"id": "r1", "state": "saved", "folder_path": str(folder)}
    monkeypatch.setattr(db, "get_run", lambda rid: dict(run_row))

    response = client.get("/meetings/r1/files/MOM.pdf")

    assert response.status_code == 404
