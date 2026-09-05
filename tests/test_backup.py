"""Covers scripts/backup.py -- no automated DB/storage backup existed
anywhere in the repo before this. Verifies a snapshot is restorable (not just
"a file got written") and that pruning keeps only the newest N.
"""
import sqlite3
import tarfile

import scripts.backup as backup_module
from app.config import settings
from scripts.backup import run_backup


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
    (storage_dir / "Meeting - 2026-09-05 1200" / "transcript.txt").write_text("hi")

    backup_root = tmp_path / "backups"

    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "base_storage_dir", storage_dir)
    monkeypatch.setattr(backup_module, "BACKUP_ROOT", backup_root)
    return backup_root


def test_backup_snapshot_is_restorable(tmp_path, monkeypatch):
    _fresh_env(tmp_path, monkeypatch)

    dest = run_backup(keep=7)

    restored = sqlite3.connect(dest / "runs.sqlite3")
    rows = restored.execute("SELECT value FROM t").fetchall()
    restored.close()
    assert rows == [("hello",)]

    with tarfile.open(dest / "storage.tar.gz") as tar:
        names = tar.getnames()
    assert any("transcript.txt" in name for name in names)


def test_prunes_to_the_newest_n_backups(tmp_path, monkeypatch):
    backup_root = _fresh_env(tmp_path, monkeypatch)

    for _ in range(5):
        run_backup(keep=3)

    remaining = sorted(p.name for p in backup_root.iterdir())
    assert len(remaining) == 3
