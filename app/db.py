import contextlib
import datetime
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from app.config import settings

# Ordered so the status page can show a progress bar; "failed" is terminal
# and can be reached from any non-terminal state.
STATES = [
    "idle",
    "received",
    "transcribing",
    "diarizing",
    # Chunked/streaming pipeline only: covers the whole span between
    # /meetings/start and /meetings/{id}/finalize's tail kicking off, while
    # per-chunk transcribe+diarize work happens during the call. A new
    # allowed value in this list only -- no column/schema change, since
    # update_run()'s validation is the only place STATES is checked against.
    "chunk_processing",
    "generating_docs",
    "rendering",
    "saving",
    "saved",
    "failed",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_runs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    audio_path TEXT,
    folder_path TEXT,
    diarization_source TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    path = db_path or settings.db_path
    conn = sqlite3.connect(path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def create_run(title: str, audio_path: str) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO meeting_runs
               (id, title, state, audio_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, title, "idle", audio_path, now, now),
        )
    return get_run(run_id)


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM meeting_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM meeting_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def update_run(run_id: str, **fields: Any) -> dict[str, Any]:
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"Unknown state: {fields['state']!r}")
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [run_id]
    with _connect() as conn:
        conn.execute(f"UPDATE meeting_runs SET {set_clause} WHERE id = ?", values)
    run = get_run(run_id)
    if run is None:
        raise KeyError(f"No meeting_run with id {run_id!r}")
    return run


def mark_failed(run_id: str, error: Exception | str) -> dict[str, Any]:
    return update_run(run_id, state="failed", error_message=str(error))


init_db()
