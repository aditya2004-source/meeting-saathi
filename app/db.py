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

-- SaaS conversion, Phase 1 (identity/auth/tenancy) -----------------------

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL UNIQUE,
    email_verified INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    free_meetings_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS otp_codes (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_otp_codes_email ON otp_codes (email);

CREATE TABLE IF NOT EXISTS consent_records (
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    email TEXT NOT NULL,
    policy_type TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_consent_records_email ON consent_records (email);

CREATE TABLE IF NOT EXISTS device_tokens (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_customer ON device_tokens (customer_id);

-- SaaS conversion, Phase 2 (entitlement engine) -- status defaults to
-- nonexistent/inactive until Phase 6 wires real Razorpay events; this lets
-- the entitlement logic and its tests be written and verified independently
-- of payment integration being live yet.
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'inactive',
    current_period_end TEXT,
    razorpay_subscription_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_customer_id ON subscriptions (customer_id);

-- SaaS conversion, Phase 4 (website) -- created here (not Phase 8, which
-- originally owned it) because Phase 4's signup/consent flow and legal
-- routes both need it before Phase 8 is reached. Phase 8 later adds
-- founder-facing editing; for now rows are seeded once at startup (see
-- app/policies.py) with real, versioned content.
CREATE TABLE IF NOT EXISTS policies (
    policy_type TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    PRIMARY KEY (policy_type, version)
);

-- SaaS conversion, Phase 4 (website) -- the first public-facing feedback
-- surface; Phase 5 adds a logged-in link that pre-fills customer_id, Phase
-- 7 reads this same table for the admin inbox.
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    email TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback (status);
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
        conn.executescript(_SCHEMA)
        # Idempotent migration for a DB created before user_name existed --
        # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table,
        # so the column has to be added separately here. OperationalError
        # means it's already there (a fresh DB, or a DB already migrated).
        for column_sql in (
            "ALTER TABLE meeting_runs ADD COLUMN user_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN client_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN client_name_normalized TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meeting_runs ADD COLUMN device_id TEXT NOT NULL DEFAULT ''",
            # Phase 1 (identity/auth/tenancy): the real ownership key, replacing
            # the free-text user_name for scoping/authorization purposes.
            # user_name/device_id stay as-is -- still useful as a display label
            # and for usage_summary()'s existing grouping.
            "ALTER TABLE meeting_runs ADD COLUMN customer_id TEXT",
            # Phase 2 (entitlement engine) will read/write this; the column is
            # added here, with the rest of Phase 1's schema work, to avoid a
            # second migration pass over the same table.
            "ALTER TABLE meeting_runs ADD COLUMN billing_mode TEXT NOT NULL DEFAULT ''",
            # Phase 2: the meeting's real recorded audio duration, set once at
            # the "saved" transition (see both orchestrators) -- NULL for
            # every run that predates this column, and while a run is still
            # in progress.
            "ALTER TABLE meeting_runs ADD COLUMN duration_seconds REAL",
        ):
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass  # column already exists -- this DB was already migrated (or is fresh)
        # No index existed on this table at all before Phase 1 -- every query
        # was a full scan. customer_id-scoped lookups (every ownership check
        # from here on) and admin/analytics queries filtering by state or
        # created_at are frequent enough now to be worth it.
        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_meeting_runs_customer_id ON meeting_runs (customer_id)",
            "CREATE INDEX IF NOT EXISTS idx_meeting_runs_state ON meeting_runs (state)",
            "CREATE INDEX IF NOT EXISTS idx_meeting_runs_created_at ON meeting_runs (created_at)",
        ):
            conn.execute(index_sql)
        conn.commit()
    finally:
        conn.close()


def create_run(
    title: str,
    audio_path: str,
    user_name: str = "",
    client_name: str = "",
    device_id: str = "",
    customer_id: Optional[str] = None,
    billing_mode: str = "",
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO meeting_runs
               (id, title, state, audio_path, user_name, client_name, client_name_normalized,
                device_id, customer_id, billing_mode, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                title,
                "idle",
                audio_path,
                user_name,
                client_name,
                normalize_client_name(client_name),
                device_id,
                customer_id,
                billing_mode,
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
    limit: int = 50,
    user_name: Optional[str] = None,
    client_name: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """`user_name` scopes the dashboard to one person's own meetings (see
    /?name=... in app.main.index()) -- without it, this returns every
    meeting from every person, which is only appropriate for the owner's
    own unfiltered admin view, not something to hand to a customer.
    `client_name` additionally scopes to one client/project (matched via
    normalize_client_name(), same casefold/whitespace-collapse rule used when the
    row was created) -- purely a dashboard organization/filtering aid.
    `customer_id` is the real (Phase 1) ownership scope -- the customer-facing
    dashboard should filter by this, not by the free-text `user_name`.
    """
    clauses = []
    params: list[Any] = []
    if user_name:
        clauses.append("user_name = ?")
        params.append(user_name)
    if client_name:
        clauses.append("client_name_normalized = ?")
        params.append(normalize_client_name(client_name))
    if customer_id:
        clauses.append("customer_id = ?")
        params.append(customer_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM meeting_runs {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def distinct_client_names(user_name: Optional[str] = None, customer_id: Optional[str] = None) -> list[str]:
    """Distinct, non-empty client_name values (display strings, not the
    normalized key) -- backs the dashboard's client filter dropdown. Scoped to
    one person's own meetings when `user_name` or `customer_id` is given, same
    as list_runs() -- without one of these, a real customer's dropdown would
    leak every other customer's client/project names.
    """
    clauses = ["client_name != ''"]
    params: list[Any] = []
    if user_name:
        clauses.append("user_name = ?")
        params.append(user_name)
    if customer_id:
        clauses.append("customer_id = ?")
        params.append(customer_id)
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


# Everything in STATES except these is an "in progress" state -- a run sitting
# in one of those with no recent activity is orphaned (the worker thread that
# was driving it is gone).
_TERMINAL_STATES = ("saved", "failed")
# Public alias -- scripts/reap_working_dirs.py needs the same source of truth
# without reaching into a module-private name.
TERMINAL_STATES = _TERMINAL_STATES


def fail_stale_runs(older_than_minutes: int) -> int:
    """Mark orphaned in-progress runs as failed. Called once on server startup:
    any non-terminal run is stranded because the thread processing it died with
    the previous process, so it would otherwise show "in progress" on the
    dashboard forever. The age check spares a run that a fast restart could
    still resume via chunked_state recovery (a live recording keeps POSTing
    chunks, which refreshes updated_at). Returns how many runs were failed.
    `older_than_minutes <= 0` is a no-op.
    """
    if older_than_minutes <= 0:
        return 0
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=older_than_minutes)
    ).isoformat()
    message = (
        f"Marked failed on server startup: no activity for over {older_than_minutes} "
        "minutes and the process that was handling it is no longer running."
    )
    placeholders = ", ".join("?" for _ in _TERMINAL_STATES)
    with _connect() as conn:
        cursor = conn.execute(
            f"""UPDATE meeting_runs
                   SET state = 'failed', error_message = ?, updated_at = ?
                 WHERE state NOT IN ({placeholders})
                   AND updated_at < ?""",
            (message, _now(), *_TERMINAL_STATES, cutoff),
        )
        return cursor.rowcount


# --- Phase 1: customers, OTP, consent, device tokens ---------------------
# Crypto (hashing codes/tokens, generating random values) lives in app.auth,
# not here -- this module only ever stores/compares the hashes it's given,
# same separation the rest of the app already has between "persistence" and
# "business logic".


def create_customer(name: str, email: str) -> dict[str, Any]:
    customer_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO customers (id, name, email, created_at) VALUES (?, ?, ?, ?)",
            (customer_id, name, email, _now()),
        )
    return get_customer(customer_id)


def get_customer(customer_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def get_customer_by_email(email: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def update_customer(customer_id: str, **fields: Any) -> dict[str, Any]:
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [customer_id]
    with _connect() as conn:
        conn.execute(f"UPDATE customers SET {set_clause} WHERE id = ?", values)
    customer = get_customer(customer_id)
    if customer is None:
        raise KeyError(f"No customer with id {customer_id!r}")
    return customer


def touch_customer_last_active(customer_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE customers SET last_active_at = ? WHERE id = ?", (_now(), customer_id)
        )


def create_otp_code(email: str, code_hash: str, ttl_minutes: int) -> dict[str, Any]:
    otp_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = (now + datetime.timedelta(minutes=ttl_minutes)).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO otp_codes (id, email, code_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (otp_id, email, code_hash, expires_at, now.isoformat()),
        )
        row = conn.execute("SELECT * FROM otp_codes WHERE id = ?", (otp_id,)).fetchone()
    return dict(row)


def get_latest_otp_code(email: str) -> Optional[dict[str, Any]]:
    """The most recently created OTP for this email, consumed or not --
    verify_otp() in app.auth decides what "not usable" means (already
    consumed, expired, too many attempts); this just hands back the row.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM otp_codes WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def count_recent_otp_requests(email: str, within_minutes: int) -> int:
    """Backs app.auth's send-otp rate limit -- counts OTPs (successful or
    not) issued to this email in the last `within_minutes`, regardless of
    whether any was ever verified.
    """
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=within_minutes)
    ).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM otp_codes WHERE email = ? AND created_at >= ?",
            (email, cutoff),
        ).fetchone()
    return row["n"]


def increment_otp_attempt(otp_id: str) -> int:
    with _connect() as conn:
        conn.execute(
            "UPDATE otp_codes SET attempt_count = attempt_count + 1 WHERE id = ?", (otp_id,)
        )
        row = conn.execute("SELECT attempt_count FROM otp_codes WHERE id = ?", (otp_id,)).fetchone()
    return row["attempt_count"]


def consume_otp_code(otp_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE otp_codes SET consumed_at = ? WHERE id = ?", (_now(), otp_id))


def record_consent(
    email: str, policy_type: str, policy_version: str, ip_address: str = "", customer_id: Optional[str] = None
) -> dict[str, Any]:
    consent_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO consent_records
               (id, customer_id, email, policy_type, policy_version, accepted_at, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (consent_id, customer_id, email, policy_type, policy_version, _now(), ip_address),
        )
        row = conn.execute("SELECT * FROM consent_records WHERE id = ?", (consent_id,)).fetchone()
    return dict(row)


def has_recorded_consent(email: str, policy_types: list[str]) -> bool:
    """True only if every policy_type in `policy_types` has at least one
    consent_records row for this email -- signup requires ALL of Terms/
    Privacy/Refund/AI-Disclaimer/recording-responsibility, not just one.
    """
    if not policy_types:
        return True
    placeholders = ", ".join("?" for _ in policy_types)
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT COUNT(DISTINCT policy_type) AS n FROM consent_records
                WHERE email = ? AND policy_type IN ({placeholders})""",
            (email, *policy_types),
        ).fetchone()
    return row["n"] == len(set(policy_types))


def create_device_token(customer_id: str, token_hash: str, label: str = "") -> dict[str, Any]:
    token_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO device_tokens (id, customer_id, token_hash, label, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_id, customer_id, token_hash, label, _now()),
        )
        row = conn.execute("SELECT * FROM device_tokens WHERE id = ?", (token_id,)).fetchone()
    return dict(row)


def get_active_device_token_by_hash(token_hash: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM device_tokens WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


def touch_device_token_last_used(token_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE device_tokens SET last_used_at = ? WHERE id = ?", (_now(), token_id)
        )


def revoke_all_device_tokens_for_customer(customer_id: str) -> int:
    """Used by account deletion (Phase 8) and can be used defensively any
    time a customer is blocked -- a revoked token's hash stays in the table
    (for audit) but get_active_device_token_by_hash() will never return it
    again once revoked_at is set.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE device_tokens SET revoked_at = ? WHERE customer_id = ? AND revoked_at IS NULL",
            (_now(), customer_id),
        )
        return cursor.rowcount


# --- Phase 2: entitlement engine -----------------------------------------


def increment_free_meetings_used(customer_id: str) -> dict[str, Any]:
    with _connect() as conn:
        conn.execute(
            "UPDATE customers SET free_meetings_used = free_meetings_used + 1 WHERE id = ?",
            (customer_id,),
        )
    return get_customer(customer_id)


def count_start_attempts_today(customer_id: str) -> int:
    """Every /meetings/start call for this customer today, regardless of
    outcome -- the entitlement engine's separate abuse guard from the
    3-free-meeting allowance (see app.entitlement.authorize_new_meeting):
    a failed/never-captured attempt doesn't cost trial quota (only a
    successful "saved" run does, via increment_free_meetings_used above),
    so without this a scripted caller could spam free attempts at zero
    quota cost. Counts every state, unlike count_runs_today() which
    excludes cancelled-before-audio attempts.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM meeting_runs
               WHERE customer_id = ? AND date(created_at) = date('now')""",
            (customer_id,),
        ).fetchone()
    return row["n"]


def count_runs_this_month(customer_id: str) -> int:
    """Backs the fair-use alert/hard-ceiling checks for a paid customer's
    "unlimited" plan (see app.entitlement) -- calendar month, UTC, matching
    how created_at is stored.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM meeting_runs
               WHERE customer_id = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')""",
            (customer_id,),
        ).fetchone()
    return row["n"]


def get_active_subscription(customer_id: str) -> Optional[dict[str, Any]]:
    """The customer's current subscription if its status is "active" and
    (when set) current_period_end hasn't passed -- a cancelled-at-period-end
    subscription (Phase 6) keeps returning here, and thus keeps entitling
    unlimited meetings, right up until that date, same as real SaaS billing.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM subscriptions
               WHERE customer_id = ? AND status = 'active'
                 AND (current_period_end IS NULL OR current_period_end >= ?)
               ORDER BY created_at DESC LIMIT 1""",
            (customer_id, _now()),
        ).fetchone()
    return dict(row) if row else None


# --- Phase 4: policies + feedback -----------------------------------------


def upsert_policy(policy_type: str, version: str, title: str, content: str, effective_date: str) -> None:
    """Idempotent seed -- called at startup (see app/policies.py) so the same
    version's content is always in sync with what's in code, without ever
    duplicating a row. A version bump (new `version` value) adds a new row
    rather than overwriting the old one, so a past consent_records row's
    policy_version always still resolves to the exact text that was in
    effect when someone accepted it.
    """
    with _connect() as conn:
        conn.execute(
            """INSERT INTO policies (policy_type, version, title, content, effective_date)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(policy_type, version) DO UPDATE SET
                   title = excluded.title, content = excluded.content, effective_date = excluded.effective_date""",
            (policy_type, version, title, content, effective_date),
        )


def get_policy(policy_type: str, version: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Without `version`, returns the latest (by effective_date) row for
    this policy_type -- what every current-facing page (legal routes,
    signup consent) should link to. A specific `version` looks up exactly
    that historical text, e.g. to show a customer what they actually agreed
    to at signup time.
    """
    with _connect() as conn:
        if version:
            row = conn.execute(
                "SELECT * FROM policies WHERE policy_type = ? AND version = ?", (policy_type, version)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM policies WHERE policy_type = ? ORDER BY effective_date DESC LIMIT 1",
                (policy_type,),
            ).fetchone()
    return dict(row) if row else None


def create_feedback(message: str, category: str = "general", email: str = "", customer_id: Optional[str] = None) -> dict[str, Any]:
    feedback_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """INSERT INTO feedback (id, customer_id, email, category, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (feedback_id, customer_id, email, category, message, _now()),
        )
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    return dict(row)


def list_feedback(status: Optional[str] = None) -> list[dict[str, Any]]:
    where = "WHERE status = ?" if status else ""
    params = (status,) if status else ()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM feedback {where} ORDER BY created_at DESC", params
        ).fetchall()
    return [dict(row) for row in rows]


def set_client_name(run_id: str, client_name: str) -> dict[str, Any]:
    """Post-hoc "set client" edit -- most real meetings start automatically
    (the extension's popup, where the optional client-name field lives, often
    never opens), so attaching a client/project name after the fact from the
    dashboard is the primary path, not just a convenience. Keeps
    client_name_normalized in sync, same as create_run().
    """
    return update_run(run_id, client_name=client_name, client_name_normalized=normalize_client_name(client_name))


init_db()
