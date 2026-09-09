"""Phase 6: Razorpay subscription lifecycle -- create, verify (webhook),
cancel. See app/billing/plans.py for pricing config and
app/billing/razorpay_client.py for the actual HTTP calls to Razorpay.
"""
import datetime
import hashlib
import json
import logging
import sqlite3
import threading

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import auth, db
from app.billing import plans, razorpay_client
from app.config import settings

logger = logging.getLogger("meeting_saathi")

router = APIRouter(prefix="/billing")

# Guards against a rapid double-click on the pricing page's subscribe
# button firing two concurrent /billing/subscribe requests, which would
# otherwise create two real Razorpay subscriptions for the same customer
# (the client-side click-guard in pricing.html covers the common case, this
# is the server-side backstop for the request that still gets through --
# e.g. two clicks close enough together that the first hasn't disabled the
# button yet). A single customer_id in flight at a time is all this needs
# to prevent; a plain in-memory set is sufficient because this app runs as
# a single uvicorn process (see docker-compose.yml) -- no cross-process
# coordination required.
_subscribe_in_progress: set[str] = set()
_subscribe_in_progress_lock = threading.Lock()


@router.post("/subscribe")
def subscribe(
    request: Request,
    billing_cycle: str = Form(...),
    currency: str = Form("INR"),
    customer: dict = Depends(auth.get_current_customer),
):
    plan = plans.get_plan(billing_cycle, currency)
    if plan is None:
        raise HTTPException(status_code=400, detail="unknown_plan")
    if not plan.is_purchasable:
        # Defense in depth -- the pricing page's currency selector and
        # subscribe buttons already only offer purchasable currencies (see
        # app.site_routes._pricing_context), but a direct API call must be
        # rejected too rather than attempting to create a Razorpay
        # subscription against a placeholder plan id.
        raise HTTPException(status_code=400, detail="plan_not_available")
    if db.get_active_subscription(customer["id"]) is not None:
        # Defense in depth -- /pricing already hides the purchase buttons
        # from an already-active subscriber (see
        # app.site_routes._active_subscription_for_request), but a direct
        # API call (or a stale page still showing the buttons) must not be
        # able to create a second real Razorpay subscription/charge.
        raise HTTPException(status_code=409, detail="already_subscribed")

    with _subscribe_in_progress_lock:
        if customer["id"] in _subscribe_in_progress:
            raise HTTPException(status_code=409, detail="subscription_creation_in_progress")
        _subscribe_in_progress.add(customer["id"])

    try:
        razorpay_customer_id = razorpay_client.find_or_create_customer(customer["email"], customer["name"])
        subscription_payload = razorpay_client.create_subscription(
            plan.razorpay_plan_id,
            razorpay_customer_id,
            billing_cycle=plan.billing_cycle,
            notes={"customer_id": customer["id"]},
        )
        db.create_pending_subscription(
            customer_id=customer["id"],
            plan=billing_cycle,
            currency=plan.currency,
            razorpay_subscription_id=subscription_payload["id"],
        )
        return JSONResponse(
            {
                "checkout_url": subscription_payload["short_url"],
                "subscription_id": subscription_payload["id"],
                # The Key ID (not the secret) is meant to be public --
                # Razorpay's own Standard Checkout requires it client-side to
                # open the payment modal. Never confuse this with
                # RAZORPAY_KEY_SECRET, which never leaves the server.
                "razorpay_key_id": settings.razorpay_key_id,
            }
        )
    finally:
        with _subscribe_in_progress_lock:
            _subscribe_in_progress.discard(customer["id"])


def _unix_to_iso(timestamp) -> str | None:
    if not timestamp:
        return None
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).isoformat()


@router.post("/verify-subscription-auth")
def verify_subscription_auth(
    razorpay_payment_id: str = Form(...),
    razorpay_signature: str = Form(...),
    customer: dict = Depends(auth.get_current_customer),
):
    """Server-side reconciliation for Standard Checkout: confirms the
    callback is genuine, then independently re-fetches the subscription and
    payment from Razorpay's API (never trusting anything the browser
    claims about plan/status/amount) before granting paid entitlement.

    This is the PRIMARY activation path while the Razorpay webhook (below)
    is debugged separately -- kept in place unchanged as a best-effort
    secondary path, so a real webhook delivery that does succeed is still a
    harmless no-op here via the same idempotency check.
    """
    pending = db.get_latest_subscription_for_customer(customer["id"])
    if pending is None:
        raise HTTPException(status_code=404, detail="no_pending_subscription")

    razorpay_subscription_id = pending["razorpay_subscription_id"]

    if not razorpay_client.verify_payment_signature(
        payment_id=razorpay_payment_id,
        subscription_id=razorpay_subscription_id,
        signature=razorpay_signature,
    ):
        raise HTTPException(status_code=400, detail="signature_verification_failed")

    # Idempotent: a refresh/retry of an already-reconciled payment is a
    # no-op success, never a second payment row or a repeated API round-trip.
    if db.get_payment_by_razorpay_id(razorpay_payment_id) is not None:
        return JSONResponse({"ok": True})

    try:
        subscription = razorpay_client.get_subscription(razorpay_subscription_id)
        payment = razorpay_client.get_payment(razorpay_payment_id)
    except httpx.HTTPError:
        logger.exception("Razorpay API call failed while reconciling subscription %s", razorpay_subscription_id)
        raise HTTPException(status_code=502, detail="razorpay_verification_failed")

    # Belongs to the exact subscription we created for this customer --
    # redundant with the signature check above (which already keys on our
    # own recorded id, never the browser's), kept as defense in depth.
    if subscription.get("id") != razorpay_subscription_id:
        raise HTTPException(status_code=400, detail="subscription_mismatch")

    expected_plan = plans.get_plan(pending["plan"], pending["currency"])
    if expected_plan is None or subscription.get("plan_id") != expected_plan.razorpay_plan_id:
        raise HTTPException(status_code=400, detail="plan_mismatch")

    if subscription.get("status") != "active" or not subscription.get("paid_count"):
        raise HTTPException(status_code=400, detail="subscription_not_active")

    if payment.get("status") != "captured":
        raise HTTPException(status_code=400, detail="payment_not_captured")

    db.update_subscription_status(
        razorpay_subscription_id, status="active", current_period_end=_unix_to_iso(subscription.get("current_end"))
    )
    try:
        db.create_payment(
            customer_id=customer["id"],
            razorpay_subscription_id=razorpay_subscription_id,
            razorpay_payment_id=razorpay_payment_id,
            original_currency=payment.get("currency", pending["currency"]),
            original_amount_minor=payment.get("amount", 0),
            status="captured",
            inr_settlement_amount_minor=payment.get("base_amount"),
        )
    except sqlite3.IntegrityError:
        pass  # a concurrent retry already inserted this exact payment first
    return JSONResponse({"ok": True})


@router.post("/cancel")
def cancel(customer: dict = Depends(auth.get_current_customer)):
    subscription = db.get_active_subscription(customer["id"])
    if subscription is None or not subscription.get("razorpay_subscription_id"):
        raise HTTPException(status_code=404, detail="no_active_subscription")
    razorpay_client.cancel_subscription(subscription["razorpay_subscription_id"])
    # Does NOT change `status` -- get_active_subscription() keys on
    # status == "active", and cancel-at-cycle-end means the customer keeps
    # access right up until the real period end. This flag is purely
    # informational (account.html shows "cancelling" instead of looking
    # unchanged) until Razorpay's own subscription.cancelled webhook
    # eventually flips `status` itself. An earlier version of this set
    # status="cancelling" directly here, which cut off access immediately
    # instead of at period end -- caught by Phase 10's end-to-end test.
    db.mark_subscription_cancel_at_period_end(subscription["razorpay_subscription_id"])
    return JSONResponse({"ok": True})


# Razorpay event types this handles. Any other event type is acknowledged
# (200) but not acted on -- an unhandled event must never make Razorpay
# retry it forever.
_HANDLED_EVENTS = {
    "subscription.activated",
    "subscription.charged",
    "subscription.completed",
    "subscription.cancelled",
    "subscription.paused",
    "subscription.halted",
    "payment.failed",
}


def _extract_subscription_id(payload: dict) -> str | None:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    return entity.get("id")


def _extract_current_period_end(payload: dict) -> str | None:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    # Razorpay's subscription entity carries `current_end` as a Unix
    # timestamp (seconds) -- confirmed against Razorpay's documented
    # Subscription entity shape; verify against a real webhook payload
    # before relying on this in production (flagged, same as the plan's own
    # note on the INR-settlement field name).
    return _unix_to_iso(entity.get("current_end"))


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not razorpay_client.verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=400, detail="invalid_signature")

    dedup_key = hashlib.sha256(raw_body).hexdigest()
    payload = json.loads(raw_body)
    event_type = payload.get("event", "")
    subscription_id = _extract_subscription_id(payload)

    is_new = db.record_subscription_event(dedup_key, event_type, subscription_id, raw_body.decode("utf-8"))
    if not is_new:
        return JSONResponse({"ok": True, "duplicate": True})

    if event_type not in _HANDLED_EVENTS:
        logger.info("Razorpay webhook: unhandled event type %s, acknowledged and ignored", event_type)
        return JSONResponse({"ok": True})

    if subscription_id is None:
        logger.warning("Razorpay webhook: %s had no subscription id in payload", event_type)
        return JSONResponse({"ok": True})

    subscription = db.get_subscription_by_razorpay_id(subscription_id)
    if subscription is None:
        logger.warning("Razorpay webhook: no local subscription for razorpay id %s", subscription_id)
        return JSONResponse({"ok": True})

    if event_type in ("subscription.activated", "subscription.charged"):
        db.update_subscription_status(
            subscription_id, status="active", current_period_end=_extract_current_period_end(payload)
        )
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        # Guards against a genuine cross-path collision: the server-side
        # reconciliation path (POST /billing/verify-subscription-auth) may
        # already have recorded this exact razorpay_payment_id if it beat
        # this webhook to it -- razorpay_payment_id is UNIQUE, so this would
        # otherwise raise and fail an event Razorpay would then retry
        # forever. Never a second payment row for the same real payment.
        if payment_entity.get("id") and db.get_payment_by_razorpay_id(payment_entity["id"]) is None:
            try:
                db.create_payment(
                    customer_id=subscription["customer_id"],
                    razorpay_subscription_id=subscription_id,
                    razorpay_payment_id=payment_entity["id"],
                    original_currency=payment_entity.get("currency", subscription["currency"]),
                    original_amount_minor=payment_entity.get("amount", 0),
                    status="captured",
                    # Razorpay includes a "base_amount" (in the account's
                    # settlement currency, INR) on international payments per
                    # their documented multi-currency support -- confirm this
                    # exact field name against a real payload before relying on
                    # it for financial reporting (Phase 7).
                    inr_settlement_amount_minor=payment_entity.get("base_amount"),
                )
            except sqlite3.IntegrityError:
                pass  # reconciliation path recorded it first, in the race window
    elif event_type in ("subscription.completed", "subscription.cancelled"):
        db.update_subscription_status(subscription_id, status="cancelled")
    elif event_type == "subscription.paused":
        db.update_subscription_status(subscription_id, status="paused")
    elif event_type == "subscription.halted":
        # Production-audit fix: Razorpay sends this once it has exhausted
        # its own payment retries on a failing subscription -- previously
        # unhandled entirely, so a customer whose card kept failing stayed
        # status="active" (and therefore db.get_active_subscription() kept
        # returning them, and app.entitlement kept granting unlimited
        # meetings) forever. Any non-"active" status downgrades access the
        # same way "cancelled"/"paused" already do -- get_active_subscription()
        # only ever matches status == "active".
        db.update_subscription_status(subscription_id, status="halted")
        logger.warning("Razorpay webhook: subscription %s halted (payment retries exhausted), access downgraded", subscription_id)
    elif event_type == "payment.failed":
        logger.warning("Razorpay webhook: payment.failed for subscription %s", subscription_id)

    return JSONResponse({"ok": True})
