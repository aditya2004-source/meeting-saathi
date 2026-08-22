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
