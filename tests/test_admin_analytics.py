"""Covers app/admin_analytics.py's KPI queries against known fixture data --
this is the actual test of "no manufactured analytics": each number must be
provably derived from real rows, not a hardcoded/simulated figure.
"""
import uuid

from app import admin_analytics, auth, db
from app.config import settings


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _customer(email: str) -> dict:
    return db.create_customer(name="Test", email=email)


def _active_subscription(customer_id: str, plan: str = "monthly", currency: str = "INR") -> None:
    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            """INSERT INTO subscriptions (id, customer_id, plan, currency, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', datetime('now'), datetime('now'))""",
            (str(uuid.uuid4()), customer_id, plan, currency),
        )


def test_compute_mrr_sums_monthly_plans_and_divides_yearly_by_12(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    c1 = _customer("a@example.com")
    c2 = _customer("b@example.com")
    _active_subscription(c1["id"], plan="monthly")  # ₹299/mo
    _active_subscription(c2["id"], plan="yearly")  # ₹2999/yr -> ~₹249.92/mo

    mrr = admin_analytics.compute_mrr_inr()

    assert mrr == round(299 + 2999 / 12, 2)


def test_compute_mrr_ignores_non_inr_plans(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    c1 = _customer("a@example.com")
    _active_subscription(c1["id"], plan="monthly", currency="USD")

    assert admin_analytics.compute_mrr_inr() == 0.0


def test_revenue_in_period_uses_settlement_amount_when_present(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _customer("a@example.com")
    db.create_payment(
        customer_id=customer["id"], razorpay_subscription_id=None, razorpay_payment_id="pay_1",
        original_currency="USD", original_amount_minor=499, status="captured",
        inr_settlement_amount_minor=42000,
    )
    db.create_payment(
        customer_id=customer["id"], razorpay_subscription_id=None, razorpay_payment_id="pay_2",
        original_currency="INR", original_amount_minor=29900, status="captured",
    )

    revenue = admin_analytics.revenue_in_period_inr("0001-01-01", "9999-01-01")

    assert revenue == round(420 + 299, 2)


def test_revenue_excludes_non_captured_payments(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _customer("a@example.com")
    db.create_payment(
        customer_id=customer["id"], razorpay_subscription_id=None, razorpay_payment_id="pay_1",
        original_currency="INR", original_amount_minor=29900, status="failed",
    )

    assert admin_analytics.revenue_in_period_inr("0001-01-01", "9999-01-01") == 0.0


def test_plan_mix_counts_free_as_customers_with_no_active_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    paid = _customer("paid@example.com")
    _customer("free@example.com")
    _active_subscription(paid["id"], plan="monthly")

    mix = admin_analytics.plan_mix()

    assert mix == {"monthly": 1, "yearly": 0, "free": 1}


def test_meeting_volume_and_success_rate(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    r1 = db.create_run(title="A", audio_path="")
    db.update_run(r1["id"], state="saved")
    r2 = db.create_run(title="B", audio_path="")
    db.update_run(r2["id"], state="failed")
    db.create_run(title="C", audio_path="")  # still in progress -- excluded from success rate denominator

    start, end = "0001-01-01", "9999-01-01"
    assert admin_analytics.meeting_volume(start, end) == 3
    assert admin_analytics.processing_success_rate(start, end) == 50.0


def test_average_duration_only_counts_saved_runs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    r1 = db.create_run(title="A", audio_path="")
    db.update_run(r1["id"], state="saved", duration_seconds=600.0)
    r2 = db.create_run(title="B", audio_path="")
    db.update_run(r2["id"], state="saved", duration_seconds=1200.0)
    r3 = db.create_run(title="C", audio_path="")
    db.update_run(r3["id"], state="failed", duration_seconds=99999.0)  # excluded

    avg = admin_analytics.average_duration_seconds("0001-01-01", "9999-01-01")

    assert avg == 900.0


def test_free_to_paid_conversion(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    paying = _customer("paying@example.com")
    _customer("free1@example.com")
    _customer("free2@example.com")
    db.create_payment(
        customer_id=paying["id"], razorpay_subscription_id=None, razorpay_payment_id="pay_1",
        original_currency="INR", original_amount_minor=29900, status="captured",
    )

    conversion = admin_analytics.free_to_paid_conversion()

    assert conversion == round(100 * 1 / 3, 1)


def test_estimated_cost_combines_gemini_assemblyai_and_fixed_costs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "estimated_gemini_cost_per_call_inr", 1.0)
    monkeypatch.setattr(settings, "transcription_provider", "assemblyai")
    monkeypatch.setattr(settings, "assemblyai_cost_per_hour_inr", 14.0)
    r1 = db.create_run(title="A", audio_path="")
    db.update_run(r1["id"], state="saved", duration_seconds=3600.0)  # 1 hour
    db.add_fixed_cost(effective_month="2026-09", amount_inr=500.0, note="VPS")

    cost = admin_analytics.estimated_cost_in_period_inr("2026-09-01T00:00:00", "2026-09-30T23:59:59")

    assert cost["gemini"] == admin_analytics.ESTIMATED_GEMINI_CALLS_PER_MEETING * 1.0
    assert cost["assemblyai"] == 14.0
    assert cost["fixed"] == 500.0
    assert cost["total"] == cost["gemini"] + cost["assemblyai"] + cost["fixed"]


def test_estimated_cost_is_zero_for_local_whisper_provider(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "transcription_provider", "auto")
    monkeypatch.setattr(settings, "estimated_gemini_cost_per_call_inr", 0.0)
    r1 = db.create_run(title="A", audio_path="")
    db.update_run(r1["id"], state="saved", duration_seconds=3600.0)

    cost = admin_analytics.estimated_cost_in_period_inr("0001-01-01", "9999-01-01")

    assert cost["assemblyai"] == 0.0
    assert cost["gemini"] == 0.0


def test_document_usage_counts_by_event_type(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    run = db.create_run(title="A", audio_path="")
    db.record_document_event(run["id"], "mom", "generated")
    db.record_document_event(run["id"], "mom", "viewed")
    db.record_document_event(run["id"], "mom", "viewed")
    db.record_document_event(run["id"], "meeting_analysis", "generated")

    usage = admin_analytics.document_usage()

    assert usage["mom"] == {"generated": 1, "viewed": 2, "downloaded": 0}
    assert usage["meeting_analysis"]["generated"] == 1
    assert usage["business_process_flow"] == {"generated": 0, "viewed": 0, "downloaded": 0}


def test_resolve_period_custom_range_overrides_preset():
    start, end = admin_analytics.resolve_period("30d", from_date="2026-01-01", to_date="2026-01-31")
    assert start.startswith("2026-01-01")
    assert end.startswith("2026-01-31")


def test_resolve_period_all_time_starts_far_in_the_past():
    start, _ = admin_analytics.resolve_period("all")
    assert start == "0001-01-01"
