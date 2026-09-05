"""Phase 1 auth API: signup, consent, OTP verification, and device-token
pairing for the extension. Pure JSON endpoints for now -- Phase 4 builds the
actual website pages that call these; building the API first (and testing it
directly) keeps this phase's identity/security work independently verifiable
without waiting on the UI. Phase 8 adds the account/data-deletion endpoints
at the bottom of this file -- same /account/* namespace, same auth
dependency.
"""
import logging
import shutil

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth, db
from app.billing import razorpay_client
from app.email_sender import send_email
from app.policies import POLICY_VERSION as CURRENT_POLICY_VERSION

logger = logging.getLogger("meeting_saathi")

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.post("/auth/signup")
def signup(name: str = Form(...), email: str = Form(...)):
    email = auth.normalize_email(email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="invalid_email")
    customer = db.get_customer_by_email(email)
    if customer is None:
        customer = db.create_customer(name=name.strip(), email=email)
    return JSONResponse({"email": customer["email"], "email_verified": bool(customer["email_verified"])})


@router.post("/auth/consent")
def record_consent(request: Request, email: str = Form(...)):
    """Records acceptance of every required policy at once -- the signup
    form presents them as a single checkbox (per the approved product
    reference), but each is still stored as its own row so a future policy
    version bump can require re-consent for just that one policy without
    touching the others.
    """
    email = auth.normalize_email(email)
    customer = db.get_customer_by_email(email)
    ip_address = _client_ip(request)
    for policy_type in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(
            email=email,
            policy_type=policy_type,
            policy_version=CURRENT_POLICY_VERSION,
            ip_address=ip_address,
            customer_id=customer["id"] if customer else None,
        )
    return JSONResponse({"ok": True})


@router.post("/auth/send-otp")
def send_otp(email: str = Form(...)):
    email = auth.normalize_email(email)
    customer = db.get_customer_by_email(email)
    already_verified = bool(customer and customer["email_verified"])
    # A brand-new/never-verified email must have consent on file first --
    # server-enforced, not just a disabled button in the UI (the client-side
    # checkbox gate alone isn't sufficient: nothing stops a direct API call).
    # A returning, already-verified customer logging in again already
    # consented once and isn't asked to re-consent just to sign back in.
    if not already_verified and not auth.has_required_consent(email):
        raise HTTPException(status_code=400, detail="consent_required")
    if not auth.otp_send_allowed(email):
        raise HTTPException(status_code=429, detail="too_many_requests")
    code = auth.issue_otp(email)
    send_email(
        to=email,
        subject="Your Meeting Saathi verification code",
        text_body=(
            f"Your verification code is {code}. It expires in "
            f"{auth.OTP_TTL_MINUTES} minutes. If you didn't request this, "
            "you can ignore this email."
        ),
    )
    return JSONResponse({"ok": True})


@router.post("/auth/verify-otp")
def verify_otp(request: Request, email: str = Form(...), code: str = Form(...)):
    email = auth.normalize_email(email)
    if not auth.verify_otp(email, code):
        raise HTTPException(status_code=400, detail="invalid_or_expired_code")
    customer = db.get_customer_by_email(email)
    if customer is None:
        # Verifying a code implies consent was already required to send it
        # (see send_otp above), so this is a legitimate first-time
        # activation, not a bypass -- just self-heal a missing signup row.
        customer = db.create_customer(name="", email=email)
    if not customer["email_verified"]:
        customer = db.update_customer(customer["id"], email_verified=1)
    request.session["customer_id"] = customer["id"]
    db.touch_customer_last_active(customer["id"])
    return JSONResponse({"id": customer["id"], "name": customer["name"], "email": customer["email"]})


@router.post("/auth/logout")
def logout(request: Request):
    request.session.pop("customer_id", None)
    return JSONResponse({"ok": True})


@router.get("/account/me")
def whoami(customer: dict = Depends(auth.get_current_customer)):
    return JSONResponse(
        {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "free_meetings_used": customer["free_meetings_used"],
        }
    )


@router.post("/account/connect/device-token")
def create_device_token(request: Request, customer: dict = Depends(auth.get_current_customer)):
    """The "Connect Account" flow's server side: called by a fetch() from the
    logged-in website page, which then relays the returned raw token to the
    extension via chrome.runtime.sendMessage (externally_connectable) -- see
    Admin personal/extension(personal)/manifest.json's externally_connectable
    entry and background.js's onMessageExternal listener. The raw token is
    only ever returned here, once; only its hash is stored (app.auth.issue_device_token).
    """
    label = (request.query_params.get("label") or "Chrome extension").strip()
    token = auth.issue_device_token(customer["id"], label=label)
    return JSONResponse({"token": token})


@router.post("/account/delete-meeting-data")
def delete_meeting_data(customer: dict = Depends(auth.get_current_customer)):
    """Removes the actual on-disk content (transcript/documents) for every
    one of this customer's meetings -- a real deletion, not a toast that
    does nothing. The `meeting_runs` rows themselves are kept (folder_path
    cleared, everything else untouched) rather than deleted outright, so
    Phase 7's historical aggregate analytics (meeting volume, success rate)
    don't retroactively shrink for a period that already happened.
    """
    runs = db.list_runs(customer_id=customer["id"], limit=100000)
    cleared = 0
    for run in runs:
        folder_path = run.get("folder_path")
        if not folder_path:
            continue
        shutil.rmtree(folder_path, ignore_errors=True)
        db.update_run(run["id"], folder_path=None)
        cleared += 1
    return JSONResponse({"ok": True, "meetings_cleared": cleared})


@router.post("/account/delete-account")
def delete_account(request: Request, customer: dict = Depends(auth.get_current_customer)):
    """Real cascading effects: cancels any active Razorpay subscription,
    revokes every device token (a paired extension must stop working
    immediately, not keep recording against a "deleted" account), marks the
    customer inactive (get_current_customer/_customer_from_session/
    _customer_from_bearer_token in app/auth.py all already reject a
    non-"active" status, so this alone would be enough even if a token
    somehow survived revocation), and clears the current session.
    """
    subscription = db.get_active_subscription(customer["id"])
    if subscription and subscription.get("razorpay_subscription_id"):
        try:
            razorpay_client.cancel_subscription(subscription["razorpay_subscription_id"])
        except Exception:  # noqa: BLE001 - a Razorpay API failure must not block account deletion
            logger.exception("Failed to cancel Razorpay subscription during account deletion")
        db.update_subscription_status(subscription["razorpay_subscription_id"], status="cancelled")

    db.revoke_all_device_tokens_for_customer(customer["id"])
    db.set_customer_status(customer["id"], "deleted")
    request.session.pop("customer_id", None)
    return JSONResponse({"ok": True})
