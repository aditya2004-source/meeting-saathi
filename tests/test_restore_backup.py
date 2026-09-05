"""Covers scripts/restore_backup.py -- Phase 9 explicitly calls for actually
restoring a backup at least once, not just assuming scripts/backup.py's
snapshot works. Creates a real snapshot via run_backup(), "loses" the live
data, restores, and confirms it's back.
"""
import sqlite3

import scripts.backup as backup_module
from app.config import settings
from scripts.backup import run_backup
from scripts.restore_backup import restore_backup


def _fresh_env(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "runs.sqlite3"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO t (value) VALUES ('hello')")
    conn.commit()
    conn.close()

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    (storage_dir / "Meeting - 2026-09-05 1200").mkdir()
    (storage_dir / "Meeting - 2026-09-05 1200" / "transcript.txt").write_text("real transcript")

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "base_storage_dir", storage_dir)
    monkeypatch.setattr(backup_module, "BACKUP_ROOT", backup_root)
    return db_path, storage_dir


def test_dry_run_does_not_touch_anything(tmp_path, monkeypatch):
    db_path, storage_dir = _fresh_env(tmp_path, monkeypatch)
    snapshot_dir = run_backup(keep=7)

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()

    restore_backup(snapshot_dir, confirm=False)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT value FROM t").fetchall()
    conn.close()
    assert rows == []  # dry run -- the deletion above is still in effect


def test_restore_brings_back_the_db_and_storage_dir(tmp_path, monkeypatch):
    db_path, storage_dir = _fresh_env(tmp_path, monkeypatch)
    snapshot_dir = run_backup(keep=7)

    # Simulate data loss: DB row deleted, storage directory wiped entirely.
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    for child in storage_dir.iterdir():
        if child.is_dir():
            import shutil

            shutil.rmtree(child)
        else:
            child.unlink()

    restore_backup(snapshot_dir, confirm=True)

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT value FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]

    restored_transcript = storage_dir / "Meeting - 2026-09-05 1200" / "transcript.txt"
    assert restored_transcript.is_file()
    assert restored_transcript.read_text() == "real transcript"


def test_restore_raises_on_a_missing_backup_directory(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)
    import pytest

    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "does-not-exist", confirm=True)
