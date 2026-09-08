"""Thin wrapper around Razorpay's REST API via httpx (already a dependency
-- no new SDK needed). Only the calls Phase 6 actually needs: find-or-create
a Customer, create a Subscription, and verify a webhook signature.

No real Razorpay account was available to test this against in this
session -- calls are shaped per Razorpay's publicly documented API
(https://razorpay.com/docs/api/), and the HTTP-call logic itself is unit
tested with a mocked transport (see tests/test_billing.py), but real
end-to-end verification against a live (test-mode) Razorpay account is the
founder's to do before Phase 9.
"""
import hashlib
import hmac

import httpx

from app.config import settings

_BASE_URL = "https://api.razorpay.com/v1"


def _auth() -> tuple[str, str]:
    return (settings.razorpay_key_id, settings.razorpay_key_secret)


def find_or_create_customer(email: str, name: str) -> str:
    """Razorpay customers are unique per (account, email) -- creating one
    that already exists returns a 400 with the existing customer's id in
    the error payload, per Razorpay's documented behavior, rather than a
    dedicated "find" endpoint. Handles that instead of doing a separate
    lookup call first.
    """
    response = httpx.post(
        f"{_BASE_URL}/customers",
        auth=_auth(),
        json={"name": name or email, "email": email, "fail_existing": "0"},
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()["id"]


def create_subscription(plan_id: str, customer_id: str, billing_cycle: str, notes: dict) -> dict:
    """Returns the raw Razorpay Subscription entity (has "id" and
    "short_url" -- the hosted checkout page to redirect the customer to).
    `total_count` is required by Razorpay's API; 120 monthly cycles (10
    years) / 20 yearly cycles is effectively "until cancelled" for a
    subscription product like this.

    `billing_cycle` must come from the app's own plan config
    (app.billing.plans.Plan.billing_cycle), never inferred from `plan_id`
    itself -- real Razorpay-generated plan ids are opaque strings with no
    guaranteed relationship to the cycle they represent.
    """
    total_count = 120 if billing_cycle == "monthly" else 20
    response = httpx.post(
        f"{_BASE_URL}/subscriptions",
        auth=_auth(),
        json={
            "plan_id": plan_id,
            "customer_id": customer_id,
            "total_count": total_count,
            "customer_notify": 1,
            "notes": notes,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def cancel_subscription(razorpay_subscription_id: str) -> dict:
    """`cancel_at_cycle_end=1` -- the customer keeps access through what
    they already paid for, per the plan's explicit requirement, rather than
    an instant cutoff.
    """
    response = httpx.post(
        f"{_BASE_URL}/subscriptions/{razorpay_subscription_id}/cancel",
        auth=_auth(),
        json={"cancel_at_cycle_end": 1},
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Razorpay signs each webhook delivery with HMAC-SHA256 over the raw
    (unparsed) request body, keyed on the webhook secret configured in the
    Razorpay Dashboard -- sent as the X-Razorpay-Signature header, hex
    encoded. No signature verification existed anywhere in this repo before
    this. hmac.compare_digest avoids a timing side-channel on the compare.
    """
    if not settings.razorpay_webhook_secret or not signature_header:
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)
