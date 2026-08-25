"""In-memory bookkeeping for which document *groups* (see app/docgen/registry.py)
are currently generating or last failed, per run -- same spirit as
app/chunked_state.py's in-memory RunChunkState registry: document status is mostly
computed fresh from the meeting folder's filesystem contents (a .pdf either exists or
it doesn't), but "generating" and "failed" aren't observable from the filesystem
alone, so this small process-wide, lock-guarded store fills that one gap.

Accepted v1 scope, same category as chunked_state.py's own accepted gap: a server
restart mid-generation loses track of an in-flight generate -- the affected document
just reverts to "not_generated" (nothing was written yet, since generation writes its
output atomically only once it's fully computed) and the user can simply click
"Generate" again.
"""
import threading

_lock = threading.Lock()
_in_progress: set[tuple[str, str]] = set()  # (run_id, group_key)
_failures: dict[tuple[str, str], str] = {}


def mark_generating(run_id: str, group_key: str) -> bool:
    """Returns False (and marks nothing) if this group is already generating for
    this run -- the caller should treat that as "already in progress, don't spawn a
    second background thread for it" rather than an error.
    """
    with _lock:
        key = (run_id, group_key)
        if key in _in_progress:
            return False
        _in_progress.add(key)
        _failures.pop(key, None)
        return True


def mark_done(run_id: str, group_key: str) -> None:
    with _lock:
        _in_progress.discard((run_id, group_key))


def mark_failed(run_id: str, group_key: str, message: str) -> None:
    with _lock:
        _failures[(run_id, group_key)] = message


def is_generating(run_id: str, group_key: str) -> bool:
    with _lock:
        return (run_id, group_key) in _in_progress


def get_failure(run_id: str, group_key: str) -> str | None:
    with _lock:
        return _failures.get((run_id, group_key))
