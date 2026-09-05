#!/usr/bin/env python3
"""Restores a snapshot written by scripts/backup.py. Phase 9 explicitly
calls for actually restoring a backup at least once, not just assuming the
script works -- this is that restore path, and its test
(tests/test_restore_backup.py) exercises it against a real snapshot.

Usage:
    python scripts/restore_backup.py <backup_dir> [--yes]

<backup_dir> is one of the timestamped directories scripts/backup.py
creates under backups/ (e.g. backups/20260905T072818.112423Z). Without
--yes, this only prints what it would do -- it overwrites the live DB and
storage directory, so it asks for that flag deliberately rather than a
prompt (safe to call non-interactively once the founder has confirmed).
"""
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def restore_backup(backup_dir: Path, confirm: bool = False) -> None:
    db_backup = backup_dir / settings.db_path.name
    storage_backup = backup_dir / "storage.tar.gz"

    if not backup_dir.is_dir():
        raise FileNotFoundError(f"No such backup directory: {backup_dir}")
    if not db_backup.is_file():
        raise FileNotFoundError(f"No {db_backup.name} in {backup_dir}")

    print(f"Would restore:\n  DB:      {db_backup} -> {settings.db_path}\n  Storage: {storage_backup} -> {settings.base_storage_dir}")
    if not confirm:
        print("\nDry run only -- pass --yes to actually restore (this overwrites the live DB and storage dir).")
        return

    # sqlite3's own backup API again (matches scripts/backup.py), for the
    # same "consistent even mid-write" reason, restoring into a fresh file
    # rather than a raw copy over a possibly-open handle.
    source = sqlite3.connect(db_backup)
    target = sqlite3.connect(settings.db_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    if storage_backup.is_file():
        if settings.base_storage_dir.exists():
            shutil.rmtree(settings.base_storage_dir)
        settings.base_storage_dir.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(storage_backup) as tar:
            tar.extractall(settings.base_storage_dir.parent)

    print("Restore complete.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    backup_dir = Path(args[0])
    confirm = "--yes" in args
    restore_backup(backup_dir, confirm=confirm)


if __name__ == "__main__":
    main()
