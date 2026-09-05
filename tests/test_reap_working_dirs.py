"""Covers scripts/reap_working_dirs.py -- the cleanup for working/<run_id>/
scratch directories that both orchestrators only ever remove on the success
path, leaving failed/orphaned runs' dirs behind forever otherwise.
"""
from app import db
from app.config import settings
from scripts.reap_working_dirs import reap_working_dirs


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _fresh_working_dir(tmp_path, monkeypatch):
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.setattr(settings, "working_dir", working_dir)
    return working_dir


def test_removes_dir_for_a_run_with_no_matching_db_row(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    working_dir = _fresh_working_dir(tmp_path, monkeypatch)
    orphan = working_dir / "no-such-run-id"
    orphan.mkdir()

    removed = reap_working_dirs()

    assert removed == ["no-such-run-id"]
    assert not orphan.exists()


def test_removes_dir_for_a_terminal_state_run(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    working_dir = _fresh_working_dir(tmp_path, monkeypatch)
    run = db.create_run(title="Failed meeting", audio_path="")
    db.mark_failed(run["id"], "boom")
    run_dir = working_dir / run["id"]
    run_dir.mkdir()

    removed = reap_working_dirs()

    assert removed == [run["id"]]
    assert not run_dir.exists()


def test_leaves_a_non_terminal_run_alone(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    working_dir = _fresh_working_dir(tmp_path, monkeypatch)
    run = db.create_run(title="Live meeting", audio_path="")  # state="idle"
    run_dir = working_dir / run["id"]
    run_dir.mkdir()

    removed = reap_working_dirs()

    assert removed == []
    assert run_dir.exists()


def test_dry_run_reports_without_deleting(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    working_dir = _fresh_working_dir(tmp_path, monkeypatch)
    orphan = working_dir / "no-such-run-id"
    orphan.mkdir()

    removed = reap_working_dirs(dry_run=True)

    assert removed == ["no-such-run-id"]
    assert orphan.exists()


def test_missing_working_dir_returns_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "working_dir", tmp_path / "does-not-exist")

    assert reap_working_dirs() == []
