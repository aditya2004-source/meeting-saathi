#!/usr/bin/env python3
"""Delete orphaned working/<run_id>/ scratch directories.

Both orchestrators (app/orchestrator.py, app/orchestrator_streaming.py) clean up
their working directory only on the success path (state="saved") -- a run that
ends "failed", or that never reaches a terminal state before the process dies,
leaves its chunks/segments/timing sidecars behind forever. There is no periodic
sweep for this today, so these accumulate without bound (36 such directories
had built up in this repo before this script existed).

Safe to delete: a working/<run_id>/ directory whose run_id
  - has no matching row in meeting_runs at all, or
  - has a row in a terminal state (saved/failed).
A "saved" run's dir *should* already be gone (the success path removes it
itself), so one surviving is itself anomalous -- but still safe to remove,
since the real output already lives in base_storage_dir, not here.

Deliberately NOT touched: a working/<run_id>/ dir for a run still in a
non-terminal state -- that's either a live in-progress meeting, or a stranded
one that app.db.fail_stale_runs() (run on every server startup) will mark
"failed" soon, at which point a later run of this script picks it up. This
script never guesses at staleness by directory mtime; it only trusts the DB's
own state.

Usage:
    python scripts/reap_working_dirs.py [--dry-run]
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402


def reap_working_dirs(dry_run: bool = False) -> list[str]:
    """Returns the list of run_ids whose working dir was (or would be) removed."""
    removed = []
    if not settings.working_dir.is_dir():
        return removed
    for entry in sorted(settings.working_dir.iterdir()):
        if not entry.is_dir():
            continue
        run_id = entry.name
        run = db.get_run(run_id)
        if run is not None and run["state"] not in db.TERMINAL_STATES:
            continue  # live or not-yet-swept-stale -- leave it alone
        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)
        removed.append(run_id)
    return removed


def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]
    removed = reap_working_dirs(dry_run=dry_run)
    verb = "Would remove" if dry_run else "Removed"
    if not removed:
        print("No orphaned working directories found.")
        return
    print(f"{verb} {len(removed)} orphaned working director{'y' if len(removed) == 1 else 'ies'}:")
    for run_id in removed:
        print(f"  {run_id}")


if __name__ == "__main__":
    main()
