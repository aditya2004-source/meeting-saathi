"""Covers Phase 2's entitlement engine (app/entitlement.py): 3 free
meetings enforced server-side, incrementing only on a successful
completion (never at start, never on a failed/empty meeting), an
attempt-rate guard independent of that free-meeting count, the 3-hour
duration cap on chunk uploads, and the two-tier fair-use rule for a paid
"unlimited" subscription. Follows tests/test_run_ownership.py's convention
of a real TestClient + real app.db functions (not a monkeypatched DB) since
this phase's whole point is end-to-end correctness of the enforcement, not
just the wiring.
"""
import uuid

from fastapi.testclient import TestClient

from app import auth, db, entitlement
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")


def _verified_customer(email: str) -> dict:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _auth_header(customer_id: str) -> dict:
    token = auth.issue_device_token(customer_id, label="test")
    return {"Authorization": f"Bearer {token}"}


def _start(headers: dict, title: str = "Meeting"):
    return client.post("/meetings/start", data={"title": title}, headers=headers)


def _complete_successfully(run_id: str) -> None:
    """Simulates what both orchestrators do at the "saved" transition,
    without running the real transcription/diarization pipeline -- see
    app/orchestrator_streaming.py's finalize_run() / app/orchestrator.py's
    _run() for the real call sites this mirrors.
    """
    db.update_run(run_id, state="saved", duration_seconds=120.0)
    entitlement.record_meeting_completed(run_id)


def test_free_trial_allows_exactly_three_successful_meetings_then_blocks(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    for i in range(settings.free_meeting_allowance):
        response = _start(headers, title=f"Meeting {i}")
        assert response.status_code == 200
        run_id = response.json()["id"]
        run = db.get_run(run_id)
        assert run["billing_mode"] == "free"
        _complete_successfully(run_id)

    fourth = _start(headers, title="Meeting 4")
    assert fourth.status_code == 402
    assert fourth.json()["detail"] == "trial_exhausted"

    customer_after = db.get_customer(customer["id"])
    assert customer_after["free_meetings_used"] == settings.free_meeting_allowance


def test_a_failed_meeting_does_not_consume_the_trial(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    response = _start(headers)
    run_id = response.json()["id"]
    db.mark_failed(run_id, "no audio was ever captured")
    entitlement.record_meeting_completed(run_id)  # same call the orchestrator would make

    assert db.get_customer(customer["id"])["free_meetings_used"] == 0
    # All 3 free meetings are still available.
    for _ in range(settings.free_meeting_allowance):
        assert _start(headers).status_code == 200


def test_an_unstarted_recording_does_not_consume_the_trial(tmp_path, monkeypatch):
    # "Recording never actually starts" case: /meetings/start succeeds (a
    # DB row exists) but the run is cancelled before finalize -- state never
    # reaches "saved", so record_meeting_completed() must still no-op even
    # though this exact call is never made for a cancelled run in practice
    # (defense in depth: the increment is gated on state == "saved", not
    # just billing_mode == "free").
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    response = _start(headers)
    run_id = response.json()["id"]
    entitlement.record_meeting_completed(run_id)  # state is still "received"

    assert db.get_customer(customer["id"])["free_meetings_used"] == 0


def test_attempt_rate_guard_blocks_before_the_free_allowance_is_reached(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "max_start_attempts_per_day", 2)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    assert _start(headers, "First").status_code == 200
    assert _start(headers, "Second").status_code == 200
    # Neither of the above was ever completed, so free_meetings_used is
    # still 0 -- the attempt-rate guard is what blocks the third, not the
    # free-meeting allowance (which would otherwise still allow it).
    third = _start(headers, "Third")
    assert third.status_code == 429
    assert third.json()["detail"] == "too_many_attempts_today"


def test_duration_cap_drops_chunks_past_the_threshold(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "max_meeting_duration_seconds", 100)  # 2 chunks at 50s each
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])
    run_id = _start(headers).json()["id"]

    within_cap = client.post(
        "/meetings/{}/chunk".format(run_id),
        data={"sequence": 1},
        files={"audio": ("chunk.webm", b"fake", "audio/webm")},
        headers=headers,
    )
    assert within_cap.json()["accepted"] is True

    past_cap = client.post(
        "/meetings/{}/chunk".format(run_id),
        data={"sequence": 3},
        files={"audio": ("chunk.webm", b"fake", "audio/webm")},
        headers=headers,
    )
    assert past_cap.status_code == 200
    assert past_cap.json() == {"id": run_id, "sequence": 3, "accepted": False, "reason": "duration_cap_exceeded"}


def test_paid_customer_gets_unlimited_meetings_past_the_free_allowance(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    with db._connect() as conn:  # noqa: SLF001 - simplest way to seed a subscription row directly
        conn.execute(
            """INSERT INTO subscriptions (id, customer_id, plan, currency, status, created_at, updated_at)
               VALUES (?, ?, 'monthly', 'INR', 'active', datetime('now'), datetime('now'))""",
            (str(uuid.uuid4()), customer["id"]),
        )

    for i in range(settings.free_meeting_allowance + 2):  # well past the free allowance
        response = _start(headers, title=f"Meeting {i}")
        assert response.status_code == 200
        run_id = response.json()["id"]
        assert db.get_run(run_id)["billing_mode"] == "paid"
        _complete_successfully(run_id)

    # free_meetings_used never moved -- paid meetings don't touch it.
    assert db.get_customer(customer["id"])["free_meetings_used"] == 0


def test_fair_use_alert_flags_without_blocking(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "fair_use_alert_threshold", 2)
    monkeypatch.setattr(settings, "fair_use_hard_ceiling", 100)
    monkeypatch.setattr(settings, "max_start_attempts_per_day", 1000)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            """INSERT INTO subscriptions (id, customer_id, plan, currency, status, created_at, updated_at)
               VALUES (?, ?, 'monthly', 'INR', 'active', datetime('now'), datetime('now'))""",
            (str(uuid.uuid4()), customer["id"]),
        )

    assert entitlement.is_fair_use_flagged(customer["id"]) is False
    for i in range(3):
        response = _start(headers, title=f"Meeting {i}")
        assert response.status_code == 200  # never blocked, just flagged

    assert entitlement.is_fair_use_flagged(customer["id"]) is True


def test_fair_use_hard_ceiling_blocks_further_meetings(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "fair_use_hard_ceiling", 2)
    monkeypatch.setattr(settings, "max_start_attempts_per_day", 1000)
    customer = _verified_customer("priya@example.com")
    headers = _auth_header(customer["id"])

    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            """INSERT INTO subscriptions (id, customer_id, plan, currency, status, created_at, updated_at)
               VALUES (?, ?, 'monthly', 'INR', 'active', datetime('now'), datetime('now'))""",
            (str(uuid.uuid4()), customer["id"]),
        )

    assert _start(headers, "First").status_code == 200
    assert _start(headers, "Second").status_code == 200
    blocked = _start(headers, "Third")
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "fair_use_ceiling_reached"


def test_meetings_start_without_identity_is_rejected_before_entitlement_even_runs(tmp_path, monkeypatch):
    """Phase 0 production-audit fix: Phase 1's transitional "anonymous
    caller skips entitlement entirely" rule was a genuine trial/payment
    bypass -- anyone could POST here with no login and no token and get
    unlimited meetings fully processed for free. A real, resolved customer
    identity is now required before a new run can be created at all, so an
    anonymous caller never reaches app.entitlement in the first place.
    """
    _fresh_db(tmp_path, monkeypatch)

    for _ in range(10):  # well past the free allowance, no auth at all
        response = client.post("/meetings/start", data={"title": "Meeting"})
        assert response.status_code == 401

    assert db.list_runs() == []
