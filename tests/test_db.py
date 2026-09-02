"""Covers the Phase 1 multi-user additions to app.db: the user_name column
(and its migration onto a pre-existing DB), count_runs_today() (the
/meetings/start rate-limit check), and usage_summary() (the dashboard's
"who's actually using it" table).
"""
import sqlite3

from app import db
from app.config import settings


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()
    return db_path


def test_create_run_stores_user_name(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    run = db.create_run(title="Weekly Sync", audio_path="", user_name="Priya Shah")

    assert run["user_name"] == "Priya Shah"


def test_create_run_defaults_user_name_to_empty_string(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    run = db.create_run(title="Weekly Sync", audio_path="")

    assert run["user_name"] == ""


def test_init_db_migrates_a_pre_existing_db_without_user_name_column(tmp_path, monkeypatch):
    # Simulates the user's real machine: a DB created before user_name
    # existed. init_db() must add the column instead of crashing on the
    # second run (CREATE TABLE IF NOT EXISTS alone is a no-op here).
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE meeting_runs (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
            audio_path TEXT, folder_path TEXT, diarization_source TEXT,
            error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()

    db.init_db()  # must not raise, and must add user_name

    run = db.create_run(title="Weekly Sync", audio_path="", user_name="Rahul Verma")
    assert run["user_name"] == "Rahul Verma"


def test_count_runs_today_counts_only_that_user_today(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", user_name="Priya Shah")
    db.create_run(title="M2", audio_path="", user_name="Priya Shah")
    db.create_run(title="M3", audio_path="", user_name="Rahul Verma")

    assert db.count_runs_today("Priya Shah") == 2
    assert db.count_runs_today("Rahul Verma") == 1
    assert db.count_runs_today("Nobody") == 0


def test_count_runs_today_excludes_cancelled_before_any_audio(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    counted = db.create_run(title="Real attempt", audio_path="", user_name="Priya Shah")
    cancelled = db.create_run(title="Rejected by tabCapture", audio_path="", user_name="Priya Shah")
    db.mark_failed(cancelled["id"], "Recording was cancelled before any audio was captured")

    assert db.count_runs_today("Priya Shah") == 1
    assert db.get_run(counted["id"])["state"] == "idle"


def test_count_runs_today_still_counts_other_failures(tmp_path, monkeypatch):
    # A real attempt that failed for some other reason (e.g. Gemini quota)
    # still happened and should still count against the daily limit --
    # only the "never captured any audio" case is excluded.
    _fresh_db(tmp_path, monkeypatch)

    run = db.create_run(title="Real attempt", audio_path="", user_name="Priya Shah")
    db.mark_failed(run["id"], "Gemini returned invalid JSON")

    assert db.count_runs_today("Priya Shah") == 1


def test_list_runs_without_user_name_returns_everyone(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", user_name="Priya Shah")
    db.create_run(title="M2", audio_path="", user_name="Rahul Verma")

    titles = {run["title"] for run in db.list_runs()}
    assert titles == {"M1", "M2"}


def test_list_runs_with_user_name_scopes_to_that_person_only(tmp_path, monkeypatch):
    # The dashboard's customer-facing view (see app.main.index()'s `name`
    # query param) must never leak another person's meetings.
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="Priya's meeting", audio_path="", user_name="Priya Shah")
    db.create_run(title="Rahul's meeting", audio_path="", user_name="Rahul Verma")

    runs = db.list_runs(user_name="Priya Shah")

    assert [run["title"] for run in runs] == ["Priya's meeting"]


def test_usage_summary_aggregates_per_user(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", user_name="Priya Shah")
    db.create_run(title="M2", audio_path="", user_name="Priya Shah")
    db.create_run(title="M3", audio_path="", user_name="Rahul Verma")
    db.create_run(title="M4", audio_path="")  # no user_name -- must be excluded

    summary = {entry["user_name"]: entry for entry in db.usage_summary()}

    assert summary["Priya Shah"]["total"] == 2
    assert summary["Priya Shah"]["today"] == 2
    assert summary["Rahul Verma"]["total"] == 1
    assert "" not in summary


def test_create_run_stores_client_name_and_normalizes_it(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    run = db.create_run(title="Kickoff", audio_path="", client_name="  Acme  Corp ")

    assert run["client_name"] == "  Acme  Corp "
    assert run["client_name_normalized"] == "acme corp"


def test_list_runs_filters_by_client_name_case_and_whitespace_insensitively(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", client_name="Acme Corp")
    db.create_run(title="M2", audio_path="", client_name="Other Client")
    db.create_run(title="M3", audio_path="")  # no client -- must never match a client filter

    runs = db.list_runs(client_name="  acme   corp  ")

    assert [run["title"] for run in runs] == ["M1"]


def test_list_runs_client_filter_never_matches_across_empty_client_names(tmp_path, monkeypatch):
    # An empty client_name filter must not be treated as "the shared client
    # every unlabeled meeting belongs to" -- list_runs() only applies the
    # client filter clause when client_name is truthy.
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="")
    db.create_run(title="M2", audio_path="")

    assert {run["title"] for run in db.list_runs(client_name=None)} == {"M1", "M2"}


def test_set_client_name_updates_both_columns(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    run = db.create_run(title="Kickoff", audio_path="")
    updated = db.set_client_name(run["id"], "Beta Inc")

    assert updated["client_name"] == "Beta Inc"
    assert updated["client_name_normalized"] == "beta inc"


def test_distinct_client_names_returns_display_strings_deduped(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", client_name="Acme Corp")
    db.create_run(title="M2", audio_path="", client_name="acme  corp")  # same client, different casing/spacing
    db.create_run(title="M3", audio_path="", client_name="Beta Inc")
    db.create_run(title="M4", audio_path="")  # no client -- must be excluded

    names = db.distinct_client_names()

    assert len(names) == 2
    assert {db.normalize_client_name(n) for n in names} == {"acme corp", "beta inc"}


def test_usage_summary_groups_by_device_id_surviving_a_display_name_change(tmp_path, monkeypatch):
    # The bug this fixes: retyping a different display name used to fragment
    # one person's usage history across multiple rows. device_id is now the
    # real grouping key; user_name is just the most-recently-seen label.
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", user_name="Priya", device_id="device-1")
    db.create_run(title="M2", audio_path="", user_name="Priya Shah", device_id="device-1")

    summary = db.usage_summary()

    assert len(summary) == 1
    assert summary[0]["total"] == 2
    assert summary[0]["user_name"] == "Priya Shah"  # the most recently-seen label


def test_usage_summary_falls_back_to_user_name_for_rows_without_device_id(tmp_path, monkeypatch):
    # Historical rows predating the device_id column (device_id == "") --
    # must still show up, grouped by user_name as before.
    _fresh_db(tmp_path, monkeypatch)

    db.create_run(title="M1", audio_path="", user_name="Rahul Verma")

    summary = db.usage_summary()

    assert len(summary) == 1
    assert summary[0]["user_name"] == "Rahul Verma"
    assert summary[0]["total"] == 1


def _age_run(run_id, minutes_ago):
    stamp = (
        db.datetime.datetime.now(db.datetime.timezone.utc)
        - db.datetime.timedelta(minutes=minutes_ago)
    ).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE meeting_runs SET updated_at = ? WHERE id = ?", (stamp, run_id)
        )


def test_fail_stale_runs_fails_old_in_progress_but_spares_recent_and_terminal(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    stale = db.create_run(title="orphan", audio_path="")          # state "idle"
    db.update_run(stale["id"], state="chunk_processing")
    _age_run(stale["id"], minutes_ago=200)

    recent = db.create_run(title="live", audio_path="")
    db.update_run(recent["id"], state="transcribing")            # updated just now

    done = db.create_run(title="done", audio_path="")
    db.update_run(done["id"], state="saved")
    _age_run(done["id"], minutes_ago=999)

    failed_before = db.create_run(title="already failed", audio_path="")
    db.mark_failed(failed_before["id"], "boom")
    _age_run(failed_before["id"], minutes_ago=999)

    n = db.fail_stale_runs(older_than_minutes=120)

    assert n == 1
    assert db.get_run(stale["id"])["state"] == "failed"
    assert "no activity" in db.get_run(stale["id"])["error_message"]
    assert db.get_run(recent["id"])["state"] == "transcribing"
    assert db.get_run(done["id"])["state"] == "saved"
    assert db.get_run(failed_before["id"])["error_message"] == "boom"


def test_fail_stale_runs_disabled_when_threshold_not_positive(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    stale = db.create_run(title="orphan", audio_path="")
    db.update_run(stale["id"], state="received")
    _age_run(stale["id"], minutes_ago=10_000)

    assert db.fail_stale_runs(older_than_minutes=0) == 0
    assert db.get_run(stale["id"])["state"] == "received"
