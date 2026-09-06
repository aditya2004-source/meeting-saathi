"""Covers Phase 6: Razorpay subscription create/webhook/cancel. No real
Razorpay account was available this session -- httpx calls are mocked
(same "code + mocked tests, founder verifies for real" pattern already
used for Phase 3's AssemblyAI work). Webhook signature verification and
idempotency are tested against realistic (not live) payloads shaped per
Razorpay's documented webhook format.
"""
import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db
from app.billing import plans
from app.billing.razorpay_client import verify_webhook_signature
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


def _logged_in_client(customer: dict) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    code = auth.issue_otp(customer["email"])
    c.post("/auth/verify-otp", data={"email": customer["email"], "code": code})
    return c


def _signed_body(secret: str, payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, signature


def _activation_payload(subscription_id: str, payment_id: str = "pay_test123") -> dict:
    return {
        "event": "subscription.activated",
        "payload": {
            "subscription": {"entity": {"id": subscription_id, "current_end": 1780000000}},
            "payment": {"entity": {"id": payment_id, "currency": "INR", "amount": 29900}},
        },
    }


# --- plans -------------------------------------------------------------


def test_get_plan_returns_a_known_combo():
    plan = plans.get_plan("monthly", "INR")
    assert plan is not None
    assert plan.currency == "INR"


def test_get_plan_returns_none_for_unknown_combo():
    assert plans.get_plan("weekly", "INR") is None


# --- webhook signature ---------------------------------------------------


def test_verify_webhook_signature_accepts_a_correctly_signed_body(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    raw, signature = _signed_body("test-secret", {"event": "subscription.activated"})

    assert verify_webhook_signature(raw, signature) is True


def test_verify_webhook_signature_rejects_a_wrong_signature(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    raw, _ = _signed_body("test-secret", {"event": "subscription.activated"})

    assert verify_webhook_signature(raw, "0" * 64) is False


def test_verify_webhook_signature_rejects_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    raw, signature = _signed_body("anything", {"event": "subscription.activated"})

    assert verify_webhook_signature(raw, signature) is False


# --- POST /billing/subscribe ----------------------------------------------


def test_subscribe_requires_login(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    response = client.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "INR"})

    assert response.status_code == 401


def test_subscribe_creates_a_pending_subscription_and_returns_checkout_url(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    with patch("app.billing.routes.razorpay_client.find_or_create_customer", return_value="cust_test123"), patch(
        "app.billing.routes.razorpay_client.create_subscription",
        return_value={"id": "sub_test123", "short_url": "https://rzp.io/i/abc123"},
    ) as mock_create:
        response = session.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "INR"})

    assert response.status_code == 200
    assert response.json() == {"checkout_url": "https://rzp.io/i/abc123"}
    mock_create.assert_called_once()
    subscription = db.get_subscription_by_razorpay_id("sub_test123")
    assert subscription is not None
    assert subscription["customer_id"] == customer["id"]
    assert subscription["status"] == "created"


def test_subscribe_rejects_an_unknown_plan(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    response = session.post("/billing/subscribe", data={"billing_cycle": "weekly", "currency": "INR"})

    assert response.status_code == 400


# --- POST /billing/webhook/razorpay ----------------------------------------


def test_webhook_rejects_an_invalid_signature(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    raw, _ = _signed_body("test-secret", _activation_payload("sub_test123"))

    response = client.post(
        "/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": "wrong"}
    )

    assert response.status_code == 400


def test_webhook_activates_a_subscription_and_records_a_payment(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")
    raw, signature = _signed_body("test-secret", _activation_payload("sub_test123"))

    response = client.post(
        "/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature}
    )

    assert response.status_code == 200
    subscription = db.get_subscription_by_razorpay_id("sub_test123")
    assert subscription["status"] == "active"
    payments = db.list_payments(customer_id=customer["id"])
    assert len(payments) == 1
    assert payments[0]["razorpay_payment_id"] == "pay_test123"


def test_webhook_is_idempotent_on_a_duplicate_delivery(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")
    raw, signature = _signed_body("test-secret", _activation_payload("sub_test123"))

    first = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})
    second = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate") is True
    # Only one payment row was created, not two.
    assert len(db.list_payments(customer_id=customer["id"])) == 1


def test_webhook_acknowledges_an_unhandled_event_type_without_crashing(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    payload = {"event": "some.future.event", "payload": {}}
    raw, signature = _signed_body("test-secret", payload)

    response = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})

    assert response.status_code == 200


def test_webhook_cancellation_updates_status(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")
    db.update_subscription_status("sub_test123", status="active")
    payload = {"event": "subscription.cancelled", "payload": {"subscription": {"entity": {"id": "sub_test123"}}}}
    raw, signature = _signed_body("test-secret", payload)

    response = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})

    assert response.status_code == 200
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "cancelled"


def test_webhook_halted_downgrades_access(tmp_path, monkeypatch):
    """Production-audit fix: subscription.halted (Razorpay's own "payment
    retries exhausted" event) was previously not in _HANDLED_EVENTS at all
    -- a customer whose card kept failing stayed status="active" forever,
    so db.get_active_subscription() kept granting them unlimited meetings.
    The real test here is that get_active_subscription() actually stops
    returning this subscription, not just that the raw status string
    changed.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")
    db.update_subscription_status("sub_test123", status="active")
    assert db.get_active_subscription(customer["id"]) is not None

    payload = {"event": "subscription.halted", "payload": {"subscription": {"entity": {"id": "sub_test123"}}}}
    raw, signature = _signed_body("test-secret", payload)

    response = client.post("/billing/webhook/razorpay", content=raw, headers={"X-Razorpay-Signature": signature})

    assert response.status_code == 200
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "halted"
    assert db.get_active_subscription(customer["id"]) is None


# --- POST /billing/cancel --------------------------------------------------


def test_cancel_requires_an_active_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    response = session.post("/billing/cancel")

    assert response.status_code == 404


def test_cancel_calls_razorpay_and_marks_cancel_pending_without_revoking_access(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")
    db.update_subscription_status("sub_test123", status="active")
    session = _logged_in_client(customer)

    with patch("app.billing.routes.razorpay_client.cancel_subscription", return_value={}) as mock_cancel:
        response = session.post("/billing/cancel")

    assert response.status_code == 200
    mock_cancel.assert_called_once_with("sub_test123")
    subscription = db.get_subscription_by_razorpay_id("sub_test123")
    # status stays "active" -- cancel-at-cycle-end means the customer keeps
    # access until the real period end, not the moment they click cancel.
    assert subscription["status"] == "active"
    assert subscription["cancel_at_period_end"] == 1
    assert db.get_active_subscription(customer["id"]) is not None
