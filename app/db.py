import contextlib
import datetime
import re
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
    # Extracting structured facts (requirements/decisions/risks/etc.) from the
    # assembled transcript -- the one Gemini call that still runs automatically.
    # No individual *document* (MOM, BRD, ...) is generated automatically anymore;
    # those are on-demand from the dashboard (see app/docgen/registry.py) and don't
    # move this run's state at all -- their status is computed from the meeting
    # folder's filesystem contents, not stored here. "generating_docs"/"rendering"/
    # "saving" are kept in this list only so a historical row already in that state
    # (from before this change) still passes update_run()'s validation if touched;
    # no current code writes them.
    "extracting_facts",
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
    user_name TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    client_name_normalized TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Text stamped into error_message by /meetings/{id}/cancel when a recording
# never actually captured any audio (e.g. Chrome's tabCapture rejected the
# request) -- excluded from count_runs_today() so a failed-before-it-started
# attempt doesn't burn part of someone's daily quota.
_CANCELLED_BEFORE_AUDIO_MARKER = "cancelled before any audio was captured"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalize_client_name(client_name: str) -> str:
    """Comparison key for client_name -- trims, collapses whitespace, casefolds.
    Two people typing "Acme Corp" and "acme  corp" should be treated as the same
    client for dashboard filtering, rather than silently becoming two clients.
    """
    if not client_name:
        return ""
    return re.sub(r"\s+", " ", client_name).strip().casefold()


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
        # Idempotent migration for a DB created before user_name existed --
        # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table,
        # so the column has to be added separately here. OperationalError
        # means it's already there (a fresh DB, or a DB already migrated).
        for column_sql in (
            "ALTER TABLE meeting_runs ADD COLUMN user_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN client_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN client_name_normalized TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN device_id TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass  # column already exists -- this DB was already migrated (or is fresh)
        conn.commit()
    finally:
        conn.close()


def create_run(
    title: str,
    audio_path: str,
    user_name: str = "",
    client_name: str = "",
    device_id: str = "",
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO meeting_runs
               (id, title, state, audio_path, user_name, client_name, client_name_normalized, device_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                title,
                "idle",
                audio_path,
                user_name,
                client_name,
                normalize_client_name(client_name),
                device_id,
                now,
                now,
            ),
        )
    return get_run(run_id)


def count_runs_today(user_name: str) -> int:
    """How many meeting-starts this person already has today (UTC day,
    matching how created_at is stored). No longer used to enforce a cap --
    settings.daily_meeting_limit enforcement was removed (this is currently a
    self-use/testing deployment) -- kept as informational data for the admin
    panel. Excludes attempts that never actually captured any audio (e.g.
    Chrome's tabCapture rejecting the request).
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM meeting_runs
               WHERE user_name = ?
                 AND date(created_at) = date('now')
                 AND NOT (state = 'failed' AND error_message LIKE ?)""",
            (user_name, f"%{_CANCELLED_BEFORE_AUDIO_MARKER}%"),
        ).fetchone()
    return row["n"]


def usage_summary() -> list[dict[str, Any]]:
    """One row per *device* that has ever recorded a meeting -- total count,
    today's count, and when they were last active. Lets the dashboard
    answer "is the person I shared this with actually using it".

    Grouped by device_id (a stable per-extension-install id, see
    extension/background.js), falling back to user_name for older rows that
    predate that column -- retyping a different display name in the popup used
    to fragment one person's usage history into multiple rows; now the group key
    survives a display-name change, and the label shown is simply the most
    recently-seen user_name for that device.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT
                   COALESCE(NULLIF(device_id, ''), user_name) AS identity,
                   (
                       SELECT r2.user_name FROM meeting_runs r2
                       WHERE COALESCE(NULLIF(r2.device_id, ''), r2.user_name)
                             = COALESCE(NULLIF(meeting_runs.device_id, ''), meeting_runs.user_name)
                       ORDER BY r2.created_at DESC LIMIT 1
                   ) AS user_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN date(created_at) = date('now') THEN 1 ELSE 0 END) AS today,
                   MAX(created_at) AS last_active
               FROM meeting_runs
               WHERE user_name != '' OR device_id != ''
               GROUP BY identity
               ORDER BY last_active DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM meeting_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(
    limit: int = 50, user_name: Optional[str] = None, client_name: Optional[str] = None
) -> list[dict[str, Any]]:
    """`user_name` scopes the dashboard to one person's own meetings (see
    /?name=... in app.main.index()) -- without it, this returns every
    meeting from every person, which is only appropriate for the owner's
    own unfiltered admin view, not something to hand to a customer.
    `client_name` additionally scopes to one client/project (matched via
    normalize_client_name(), same casefold/whitespace-collapse rule used when the
    row was created) -- purely a dashboard organization/filtering aid.
    """
    clauses = []
    params: list[Any] = []
    if user_name:
        clauses.append("user_name = ?")
        params.append(user_name)
    if client_name:
        clauses.append("client_name_normalized = ?")
        params.append(normalize_client_name(client_name))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM meeting_runs {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def distinct_client_names(user_name: Optional[str] = None) -> list[str]:
    """Distinct, non-empty client_name values (display strings, not the
    normalized key) -- backs the dashboard's client filter dropdown. Scoped to
    one person's own meetings when `user_name` is given, same as list_runs().
    """
    clauses = ["client_name != ''"]
    params: list[Any] = []
    if user_name:
        clauses.append("user_name = ?")
        params.append(user_name)
    where = f"WHERE {' AND '.join(clauses)}"
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT client_name, MAX(created_at) AS last_used
                FROM meeting_runs {where}
                GROUP BY client_name_normalized
                ORDER BY last_used DESC""",
            params,
        ).fetchall()
    return [row["client_name"] for row in rows]


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


def set_client_name(run_id: str, client_name: str) -> dict[str, Any]:
    """Post-hoc "set client" edit -- most real meetings start automatically
    (the extension's popup, where the optional client-name field lives, often
    never opens), so attaching a client/project name after the fact from the
    dashboard is the primary path, not just a convenience. Keeps
    client_name_normalized in sync, same as create_run().
    """
    return update_run(run_id, client_name=client_name, client_name_normalized=normalize_client_name(client_name))


init_db()
