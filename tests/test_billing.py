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

import httpx
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


def test_inr_plans_are_purchasable_with_the_real_razorpay_plan_ids():
    monthly = plans.get_plan("monthly", "INR")
    yearly = plans.get_plan("yearly", "INR")

    assert monthly.is_purchasable is True
    assert monthly.razorpay_plan_id == "plan_TZXnf7CMdmdDfm"
    assert yearly.is_purchasable is True
    assert yearly.razorpay_plan_id == "plan_TZXpRhkLY9q1NK"


def test_international_plans_remain_defined_but_not_purchasable():
    """International pricing architecture stays available for later
    activation -- the Plan objects still exist with real display prices --
    but must not be purchasable while their Razorpay plan ids are
    placeholders (multi-currency isn't enabled on the Razorpay account yet).
    """
    for currency in ("USD", "EUR", "GBP"):
        for cycle in ("monthly", "yearly"):
            plan = plans.get_plan(cycle, currency)
            assert plan is not None
            assert plan.is_purchasable is False
            assert plan.razorpay_plan_id.startswith("plan_placeholder_")


def test_purchasable_currencies_is_inr_only_for_now():
    assert plans.purchasable_currencies() == ["INR"]


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
    body = response.json()
    # subscription_id and razorpay_key_id are what the pricing page needs to
    # open Razorpay Standard Checkout directly (no hosted-page redirect);
    # checkout_url is kept too for backward compatibility but unused by the
    # current frontend.
    assert body["checkout_url"] == "https://rzp.io/i/abc123"
    assert body["subscription_id"] == "sub_test123"
    assert body["razorpay_key_id"] == settings.razorpay_key_id
    mock_create.assert_called_once()
    # billing_cycle must be passed through explicitly from the resolved
    # Plan, not left for create_subscription to guess at from the plan id.
    assert mock_create.call_args.kwargs["billing_cycle"] == "monthly"
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


def test_subscribe_rejects_a_currently_non_purchasable_currency(tmp_path, monkeypatch):
    """Defense in depth: even though the pricing page's UI only offers INR
    right now, a direct API call for a currency that has a defined Plan but
    no real Razorpay plan id yet (USD/EUR/GBP) must still be rejected
    rather than attempting to create a subscription against a placeholder
    plan id.
    """
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    with patch("app.billing.routes.razorpay_client.find_or_create_customer") as mock_find, patch(
        "app.billing.routes.razorpay_client.create_subscription"
    ) as mock_create:
        response = session.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "USD"})

    assert response.status_code == 400
    assert response.json()["detail"] == "plan_not_available"
    mock_find.assert_not_called()
    mock_create.assert_not_called()


def test_subscribe_rejects_a_customer_who_already_has_an_active_subscription(tmp_path, monkeypatch):
    """A real-money guard: /pricing already hides the purchase buttons from
    an already-active subscriber, but repeated clicking, a stale open tab,
    or a direct API call must not be able to create a second real Razorpay
    subscription/charge for the same customer.
    """
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_already_active")
    db.update_subscription_status("sub_already_active", status="active")

    with patch("app.billing.routes.razorpay_client.find_or_create_customer") as mock_find, patch(
        "app.billing.routes.razorpay_client.create_subscription"
    ) as mock_create:
        response = session.post("/billing/subscribe", data={"billing_cycle": "yearly", "currency": "INR"})

    assert response.status_code == 409
    assert response.json()["detail"] == "already_subscribed"
    mock_find.assert_not_called()
    mock_create.assert_not_called()
    # No new/duplicate subscription row was created.
    with db._connect() as conn:  # noqa: SLF001
        count = conn.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE customer_id = ?", (customer["id"],)).fetchone()["n"]
    assert count == 1


def test_subscribe_rejects_a_concurrent_duplicate_for_the_same_customer(tmp_path, monkeypatch):
    """Server-side backstop for a rapid double-click getting past the
    pricing page's own client-side click-guard: a second /billing/subscribe
    request for the same customer, arriving while the first is still being
    processed, must not create a second Razorpay subscription.
    """
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    from app.billing import routes as billing_routes

    # Simulate "request 1 already in flight" directly, since actually
    # racing two real threads through TestClient is flaky to assert on --
    # the in-progress-set membership is exactly what the route checks.
    billing_routes._subscribe_in_progress.add(customer["id"])
    try:
        with patch("app.billing.routes.razorpay_client.find_or_create_customer") as mock_find, patch(
            "app.billing.routes.razorpay_client.create_subscription"
        ) as mock_create:
            response = session.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "INR"})
    finally:
        billing_routes._subscribe_in_progress.discard(customer["id"])

    assert response.status_code == 409
    mock_find.assert_not_called()
    mock_create.assert_not_called()


def test_subscribe_succeeds_after_a_prior_in_flight_request_completes(tmp_path, monkeypatch):
    """The in-progress guard must clear once a request finishes (success or
    failure) -- it should never permanently lock a customer out.
    """
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    from app.billing import routes as billing_routes

    with patch("app.billing.routes.razorpay_client.find_or_create_customer", return_value="cust_test123"), patch(
        "app.billing.routes.razorpay_client.create_subscription",
        return_value={"id": "sub_test123", "short_url": "https://rzp.io/i/abc123"},
    ):
        response = session.post("/billing/subscribe", data={"billing_cycle": "monthly", "currency": "INR"})

    assert response.status_code == 200
    assert customer["id"] not in billing_routes._subscribe_in_progress


# --- razorpay_client.create_subscription: total_count -----------------------


def test_create_subscription_uses_120_cycles_for_monthly_regardless_of_plan_id(monkeypatch):
    """The bug this replaces inferred cycle length by checking whether the
    literal word "monthly" appeared in the plan_id string -- true only for
    this project's own placeholder ids. Real Razorpay plan ids are opaque
    (e.g. "plan_QRstUvWxYz1234") and would silently fall through to the
    wrong total_count under that logic. billing_cycle must be the only
    thing that decides this, passed in explicitly by the caller.
    """
    from app.billing import razorpay_client

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "sub_x", "short_url": "https://rzp.io/i/x"}

    def _fake_post(url, auth, json, timeout):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(razorpay_client.httpx, "post", _fake_post)

    razorpay_client.create_subscription("plan_QRstUvWxYz1234", "cust_1", billing_cycle="monthly", notes={})

    assert captured["json"]["total_count"] == 120


def test_create_subscription_uses_20_cycles_for_yearly_regardless_of_plan_id(monkeypatch):
    from app.billing import razorpay_client

    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "sub_x", "short_url": "https://rzp.io/i/x"}

    def _fake_post(url, auth, json, timeout):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(razorpay_client.httpx, "post", _fake_post)

    # Opaque real-looking id that happens to contain neither "monthly" nor
    # "yearly" -- proves the decision no longer comes from the id at all.
    razorpay_client.create_subscription("plan_QRstUvWxYz1234", "cust_1", billing_cycle="yearly", notes={})

    assert captured["json"]["total_count"] == 20


# --- db.get_latest_subscription_for_customer ---------------------------------


def test_get_latest_subscription_for_customer_returns_the_most_recent_row(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_old")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_new")

    latest = db.get_latest_subscription_for_customer(customer["id"])

    assert latest["razorpay_subscription_id"] == "sub_new"


def test_get_latest_subscription_for_customer_includes_already_active_rows(tmp_path, monkeypatch):
    """Deliberately NOT filtered to status == 'created' -- a retried/refreshed
    Standard Checkout callback for a subscription this same reconciliation
    endpoint already activated must still find that row (for its own
    idempotency check), not 404 as if nothing existed.
    """
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_active")
    db.update_subscription_status("sub_active", status="active")

    latest = db.get_latest_subscription_for_customer(customer["id"])

    assert latest is not None
    assert latest["razorpay_subscription_id"] == "sub_active"


def test_get_latest_subscription_for_customer_returns_none_with_no_rows(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")

    assert db.get_latest_subscription_for_customer(customer["id"]) is None


# --- db.get_payment_by_razorpay_id -------------------------------------------


def test_get_payment_by_razorpay_id_finds_a_recorded_payment(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    customer = _verified_customer("priya@example.com")
    db.create_payment(
        customer_id=customer["id"],
        razorpay_subscription_id="sub_test123",
        razorpay_payment_id="pay_test123",
        original_currency="INR",
        original_amount_minor=29900,
        status="captured",
    )

    found = db.get_payment_by_razorpay_id("pay_test123")

    assert found is not None
    assert found["customer_id"] == customer["id"]


def test_get_payment_by_razorpay_id_returns_none_when_not_found(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    assert db.get_payment_by_razorpay_id("pay_nonexistent") is None


# --- razorpay_client.verify_payment_signature -------------------------------


def test_verify_payment_signature_accepts_a_correctly_signed_pair(monkeypatch):
    from app.billing import razorpay_client

    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    payment_id, subscription_id = "pay_test123", "sub_test123"
    signature = hmac.new(
        "test-key-secret".encode("utf-8"), f"{payment_id}|{subscription_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()

    assert razorpay_client.verify_payment_signature(payment_id, subscription_id, signature) is True


def test_verify_payment_signature_rejects_a_tampered_subscription_id(monkeypatch):
    """Proves the formula is sensitive to which subscription_id is checked
    against -- signing for one subscription must not verify against another,
    which is exactly why the verify endpoint below uses the server's own
    recorded subscription id rather than trusting whatever the browser sends.
    """
    from app.billing import razorpay_client

    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    payment_id = "pay_test123"
    signature = hmac.new(
        "test-key-secret".encode("utf-8"), f"{payment_id}|sub_real".encode("utf-8"), hashlib.sha256
    ).hexdigest()

    assert razorpay_client.verify_payment_signature(payment_id, "sub_attacker_supplied", signature) is False


def test_verify_payment_signature_rejects_when_no_key_secret_configured(monkeypatch):
    from app.billing import razorpay_client

    monkeypatch.setattr(settings, "razorpay_key_secret", "")

    assert razorpay_client.verify_payment_signature("pay_test123", "sub_test123", "anything") is False


# --- POST /billing/verify-subscription-auth ---------------------------------


def _valid_signature(payment_id: str, subscription_id: str, secret: str = "test-key-secret") -> str:
    return hmac.new(secret.encode("utf-8"), f"{payment_id}|{subscription_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def _mock_active_subscription(plan_id: str = "plan_TZXnf7CMdmdDfm", paid_count: int = 1) -> dict:
    return {
        "id": "sub_test123",
        "plan_id": plan_id,
        "status": "active",
        "paid_count": paid_count,
        "current_end": 1791397800,
    }


def _mock_captured_payment(amount: int = 29900, currency: str = "INR") -> dict:
    return {"id": "pay_test123", "status": "captured", "amount": amount, "currency": currency}


def test_verify_subscription_auth_reconciles_and_activates_on_success(tmp_path, monkeypatch):
    """The primary activation path while the webhook is debugged separately
    -- confirms the callback, then independently re-fetches the subscription
    and payment from Razorpay before touching anything, never trusting the
    browser's own claims about plan/status/amount.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    with patch("app.billing.routes.razorpay_client.get_subscription", return_value=_mock_active_subscription()) as mock_get_sub, patch(
        "app.billing.routes.razorpay_client.get_payment", return_value=_mock_captured_payment()
    ) as mock_get_payment:
        response = session.post(
            "/billing/verify-subscription-auth",
            data={"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_get_sub.assert_called_once_with("sub_test123")
    mock_get_payment.assert_called_once_with("pay_test123")

    subscription = db.get_subscription_by_razorpay_id("sub_test123")
    assert subscription["status"] == "active"
    assert subscription["current_period_end"] is not None

    payments = db.list_payments(customer_id=customer["id"])
    assert len(payments) == 1
    assert payments[0]["razorpay_payment_id"] == "pay_test123"
    assert payments[0]["original_amount_minor"] == 29900
    assert payments[0]["status"] == "captured"

    # Entitlement actually recognizes the customer as paid now.
    assert db.get_active_subscription(customer["id"]) is not None


def test_verify_subscription_auth_rejects_a_bad_signature(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    response = session.post(
        "/billing/verify-subscription-auth",
        data={"razorpay_payment_id": "pay_test123", "razorpay_signature": "0" * 64},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "signature_verification_failed"
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "created"


def test_verify_subscription_auth_uses_the_servers_own_subscription_id_not_the_clients(tmp_path, monkeypatch):
    """A signature correctly computed for a *different* subscription id must
    not verify -- proves the endpoint checks against app.db's own recorded
    razorpay_subscription_id, exactly as Razorpay's integration guide
    requires, rather than trusting anything the browser could supply.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_real123")

    payment_id = "pay_test123"
    # Signed for a subscription id that is NOT the one this customer's
    # pending row actually points to.
    forged_signature = hmac.new(
        "test-key-secret".encode("utf-8"), f"{payment_id}|sub_someone_elses".encode("utf-8"), hashlib.sha256
    ).hexdigest()

    response = session.post(
        "/billing/verify-subscription-auth",
        data={"razorpay_payment_id": payment_id, "razorpay_signature": forged_signature},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "signature_verification_failed"


def test_verify_subscription_auth_requires_a_pending_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)

    response = session.post(
        "/billing/verify-subscription-auth",
        data={"razorpay_payment_id": "pay_test123", "razorpay_signature": "anything"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "no_pending_subscription"


def test_verify_subscription_auth_requires_login(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    response = client.post(
        "/billing/verify-subscription-auth",
        data={"razorpay_payment_id": "pay_test123", "razorpay_signature": "anything"},
    )

    assert response.status_code == 401


def test_verify_subscription_auth_rejects_a_wrong_plan(tmp_path, monkeypatch):
    """The subscription Razorpay actually reports must match the plan our
    own pending row expects -- never trust that just because Checkout
    authorized *some* subscription, it was necessarily for the plan the
    customer chose.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    wrong_plan_subscription = _mock_active_subscription(plan_id="plan_TZXpRhkLY9q1NK")  # the yearly plan id
    with patch("app.billing.routes.razorpay_client.get_subscription", return_value=wrong_plan_subscription), patch(
        "app.billing.routes.razorpay_client.get_payment", return_value=_mock_captured_payment()
    ):
        response = session.post(
            "/billing/verify-subscription-auth",
            data={"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "plan_mismatch"
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "created"
    assert db.list_payments(customer_id=customer["id"]) == []


def test_verify_subscription_auth_rejects_an_unpaid_subscription(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    not_yet_paid = _mock_active_subscription(paid_count=0)
    not_yet_paid["status"] = "created"
    with patch("app.billing.routes.razorpay_client.get_subscription", return_value=not_yet_paid), patch(
        "app.billing.routes.razorpay_client.get_payment", return_value=_mock_captured_payment()
    ):
        response = session.post(
            "/billing/verify-subscription-auth",
            data={"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "subscription_not_active"
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "created"
    assert db.list_payments(customer_id=customer["id"]) == []


def test_verify_subscription_auth_rejects_an_uncaptured_payment(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    failed_payment = _mock_captured_payment()
    failed_payment["status"] = "failed"
    with patch("app.billing.routes.razorpay_client.get_subscription", return_value=_mock_active_subscription()), patch(
        "app.billing.routes.razorpay_client.get_payment", return_value=failed_payment
    ):
        response = session.post(
            "/billing/verify-subscription-auth",
            data={"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "payment_not_captured"
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "created"
    assert db.list_payments(customer_id=customer["id"]) == []


def test_verify_subscription_auth_is_idempotent_on_retry(tmp_path, monkeypatch):
    """A refreshed page or a retried callback for the same already-reconciled
    payment must be a harmless no-op -- never a second payment row, and
    never a second round-trip to Razorpay's API.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    data = {"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")}
    with patch("app.billing.routes.razorpay_client.get_subscription", return_value=_mock_active_subscription()) as mock_get_sub, patch(
        "app.billing.routes.razorpay_client.get_payment", return_value=_mock_captured_payment()
    ) as mock_get_payment:
        first = session.post("/billing/verify-subscription-auth", data=data)
        second = session.post("/billing/verify-subscription-auth", data=data)

    assert first.status_code == 200
    assert second.status_code == 200
    # The second call short-circuits on the idempotency check before ever
    # calling Razorpay again.
    mock_get_sub.assert_called_once()
    mock_get_payment.assert_called_once()
    assert len(db.list_payments(customer_id=customer["id"])) == 1


def test_verify_subscription_auth_returns_502_on_razorpay_api_failure(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer = _verified_customer("priya@example.com")
    session = _logged_in_client(customer)
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_test123")

    with patch(
        "app.billing.routes.razorpay_client.get_subscription", side_effect=httpx.ConnectError("boom")
    ):
        response = session.post(
            "/billing/verify-subscription-auth",
            data={"razorpay_payment_id": "pay_test123", "razorpay_signature": _valid_signature("pay_test123", "sub_test123")},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "razorpay_verification_failed"
    assert db.get_subscription_by_razorpay_id("sub_test123")["status"] == "created"
    assert db.list_payments(customer_id=customer["id"]) == []


def test_verify_subscription_auth_scopes_strictly_to_the_calling_customers_own_subscription(tmp_path, monkeypatch):
    """Customer B cannot reconcile using a signature that was only ever
    valid for customer A's subscription -- get_latest_subscription_for_customer
    scopes to the logged-in customer, so B's own (different) pending
    subscription id is what gets checked, not A's.
    """
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-key-secret")
    customer_a = _verified_customer("priya@example.com")
    customer_b = _verified_customer("rahul@example.com")
    session_b = _logged_in_client(customer_b)
    db.create_pending_subscription(customer_a["id"], "monthly", "INR", "sub_a_real")
    db.create_pending_subscription(customer_b["id"], "monthly", "INR", "sub_b_real")

    # Signature genuinely valid for A's subscription and payment.
    a_signature = _valid_signature("pay_a_real", "sub_a_real")

    response = session_b.post(
        "/billing/verify-subscription-auth",
        data={"razorpay_payment_id": "pay_a_real", "razorpay_signature": a_signature},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "signature_verification_failed"
    assert db.get_subscription_by_razorpay_id("sub_a_real")["status"] == "created"
    assert db.get_subscription_by_razorpay_id("sub_b_real")["status"] == "created"


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
