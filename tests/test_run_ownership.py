"""Covers app.auth.authorize_run_access()'s transitional rule, wired into
every /meetings/{run_id}/... route in app/main.py: this was the single
biggest finding of the Phase 1 audit -- every one of these routes previously
trusted a bare run_id with no ownership check at all, letting anyone who
knew/guessed a run_id read or manipulate another customer's meeting.

A run with no customer_id (created before real identity existed, or by a
caller that hasn't paired yet) stays exactly as open as it was before this
phase -- deliberately, so the founder's own not-yet-re-paired extension
usage doesn't break the moment this lands (see auth.authorize_run_access's
docstring). A run that DOES have a customer_id is fully protected.
"""
from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app, base_url="https://testserver")


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _verified_customer(email: str) -> dict:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    customer = db.get_customer_by_email(email)
    return db.update_customer(customer["id"], email_verified=1)


def _bearer_token_for(customer_id: str) -> str:
    return auth.issue_device_token(customer_id, label="test")


def test_legacy_run_with_no_customer_id_is_reachable_without_auth(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    run = db.create_run(title="Old meeting", audio_path="")  # customer_id defaults to None

    response = client.get(f"/meetings/{run['id']}/status")

    assert response.status_code == 200


def test_owned_run_is_not_reachable_without_any_auth(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = db.create_run(title="Client call", audio_path="", customer_id=owner["id"])

    unauthenticated_client = TestClient(app, base_url="https://testserver")
    response = unauthenticated_client.get(f"/meetings/{run['id']}/status")

    assert response.status_code == 401


def test_owned_run_is_not_reachable_by_a_different_customer(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    intruder = _verified_customer("intruder@example.com")
    run = db.create_run(title="Client call", audio_path="", customer_id=owner["id"])
    intruder_token = _bearer_token_for(intruder["id"])

    response = client.get(
        f"/meetings/{run['id']}/status", headers={"Authorization": f"Bearer {intruder_token}"}
    )

    # 404, not 403 -- a wrong customer must not even learn that a run with
    # this id exists at all (same "don't confirm existence" rule the admin
    # slug already uses elsewhere in this app).
    assert response.status_code == 404


def test_owned_run_is_reachable_by_its_own_customer(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = db.create_run(title="Client call", audio_path="", customer_id=owner["id"])
    token = _bearer_token_for(owner["id"])

    response = client.get(f"/meetings/{run['id']}/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_owned_run_is_reachable_by_admin_session(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    run = db.create_run(title="Client call", audio_path="", customer_id=owner["id"])

    import app.main as main_module

    monkeypatch.setattr(main_module.auth, "is_admin_session", lambda request: True)
    admin_client = TestClient(app, base_url="https://testserver")

    response = admin_client.get(f"/meetings/{run['id']}/status")

    assert response.status_code == 200


def test_downloading_a_file_from_a_run_owned_by_someone_else_is_blocked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    owner = _verified_customer("owner@example.com")
    intruder = _verified_customer("intruder@example.com")
    folder = tmp_path / "meeting-folder"
    folder.mkdir()
    (folder / "transcript.txt").write_text("secret transcript")
    run = db.create_run(title="Client call", audio_path="", customer_id=owner["id"])
    db.update_run(run["id"], folder_path=str(folder))
    intruder_token = _bearer_token_for(intruder["id"])

    response = client.get(
        f"/meetings/{run['id']}/files/transcript.txt",
        headers={"Authorization": f"Bearer {intruder_token}"},
    )

    assert response.status_code == 404


def test_meetings_start_stamps_customer_id_from_bearer_token(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")
    owner = _verified_customer("owner@example.com")
    token = _bearer_token_for(owner["id"])

    response = client.post(
        "/meetings/start",
        data={"title": "New meeting"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    run = db.get_run(response.json()["id"])
    assert run["customer_id"] == owner["id"]


def test_meetings_start_leaves_customer_id_null_without_a_token(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "working_dir", tmp_path / "working")

    response = client.post("/meetings/start", data={"title": "New meeting"})

    assert response.status_code == 200
    run = db.get_run(response.json()["id"])
    assert run["customer_id"] is None
