#!/usr/bin/env python3
"""Snapshot the SQLite DB and the meeting-output storage directory.

No automated backup of either existed anywhere in the repo before this script
-- a disk failure or a bad migration would have been unrecoverable. This
covers app.config.settings.db_path (and its sibling
stage_duration_history.json in the same data/ directory) and
settings.base_storage_dir (every generated transcript/document).

Uses sqlite3's own backup API (not a plain file copy) for the DB, so a
snapshot taken while the server is running is still consistent even if a
write is in flight. The storage directory is archived with shutil, which is
fine there since those files are written once and never modified in place
(see app/storage.py's atomic-write-then-never-touch-again pattern).

Usage:
    python scripts/backup.py [--keep N]

Writes to <project_root>/backups/<timestamp>/ (runs.sqlite3,
stage_duration_history.json if present, storage.tar.gz). Prunes to the most
recent N backups afterward (default 7). Intended to run on a schedule (cron /
systemd timer), not just by hand.
"""
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

BACKUP_ROOT = settings.project_root / "backups"


def _backup_sqlite(db_path: Path, dest: Path) -> None:
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _backup_storage_dir(storage_dir: Path, dest_tar: Path) -> None:
    with tarfile.open(dest_tar, "w:gz") as tar:
        if storage_dir.is_dir():
            tar.add(storage_dir, arcname=storage_dir.name)


def _prune_old_backups(keep: int) -> None:
    if not BACKUP_ROOT.is_dir():
        return
    snapshots = sorted(
        (p for p in BACKUP_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    for stale in snapshots[:-keep] if keep > 0 else []:
        shutil.rmtree(stale, ignore_errors=True)


def run_backup(keep: int = 7) -> Path:
    # Microsecond resolution: this is called in a tight loop by tests, and a
    # scheduled backup could in principle fire twice within the same second.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = BACKUP_ROOT / timestamp
    dest.mkdir(parents=True, exist_ok=True)

    if settings.db_path.is_file():
        _backup_sqlite(settings.db_path, dest / settings.db_path.name)

    history_path = settings.db_path.parent / "stage_duration_history.json"
    if history_path.is_file():
        shutil.copy2(history_path, dest / history_path.name)

    _backup_storage_dir(settings.base_storage_dir, dest / "storage.tar.gz")

    _prune_old_backups(keep)
    return dest


def main() -> None:
    keep = 7
    args = sys.argv[1:]
    if "--keep" in args:
        keep = int(args[args.index("--keep") + 1])
    dest = run_backup(keep=keep)
    print(f"Backup written to {dest}")


if __name__ == "__main__":
    main()
