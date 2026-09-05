"""Covers app.policies.seed_policies() and app.db's policy storage --
Phase 4's legal pages render from this, not hardcoded template HTML, so
this is what actually backs Phase 8's later versioning promise.
"""
from app import db
from app.config import settings
from app.policies import POLICY_VERSION, seed_policies


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def test_seed_policies_creates_all_four_required_policies(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    seed_policies()

    for policy_type in ["terms_of_service", "privacy_policy", "refund_policy", "ai_disclaimer"]:
        policy = db.get_policy(policy_type)
        assert policy is not None
        assert policy["version"] == POLICY_VERSION
        assert len(policy["content"]) > 100


def test_seeding_twice_does_not_duplicate_rows(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    seed_policies()
    seed_policies()

    with db._connect() as conn:  # noqa: SLF001 - test-only introspection
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM policies WHERE policy_type = 'terms_of_service'"
        ).fetchone()["n"]
    assert count == 1


def test_get_policy_without_version_returns_the_latest(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.upsert_policy("terms_of_service", "2026-01-01", "Terms", "old content", "2026-01-01")
    db.upsert_policy("terms_of_service", "2026-06-01", "Terms", "new content", "2026-06-01")

    latest = db.get_policy("terms_of_service")

    assert latest["content"] == "new content"


def test_get_policy_with_a_specific_version_returns_that_historical_text(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.upsert_policy("terms_of_service", "2026-01-01", "Terms", "old content", "2026-01-01")
    db.upsert_policy("terms_of_service", "2026-06-01", "Terms", "new content", "2026-06-01")

    historical = db.get_policy("terms_of_service", version="2026-01-01")

    assert historical["content"] == "old content"
