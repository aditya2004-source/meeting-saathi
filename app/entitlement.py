"""Phase 2: free-trial / subscription entitlement -- server-side, unspoofable
by extension-frontend changes. app.auth decides *who* is calling; this
module decides whether that already-identified customer is allowed to
start or continue a meeting.

Deliberately only ever consulted for a caller with a real, resolved customer
identity -- an anonymous/unpaired caller (see auth.get_current_customer_optional)
never reaches this at all, matching Phase 1's transitional "don't break the
founder's own not-yet-re-paired extension" rule. Once Phase 4 ships the
pairing UI and every real caller has an identity, this becomes the only path.
"""
from fastapi import HTTPException

from app import db
from app.config import settings


def authorize_new_meeting(customer: dict) -> str:
    """Called from /meetings/start once a real customer identity is
    resolved. Returns "paid" or "free" (the run's billing_mode); raises an
    HTTPException if the customer isn't allowed to start a new meeting right
    now.
    """
    if db.count_start_attempts_today(customer["id"]) >= settings.max_start_attempts_per_day:
        raise HTTPException(status_code=429, detail="too_many_attempts_today")

    if db.get_active_subscription(customer["id"]) is not None:
        if db.count_runs_this_month(customer["id"]) >= settings.fair_use_hard_ceiling:
            raise HTTPException(status_code=429, detail="fair_use_ceiling_reached")
        return "paid"

    if customer["free_meetings_used"] < settings.free_meeting_allowance:
        return "free"

    raise HTTPException(status_code=402, detail="trial_exhausted")


def record_meeting_completed(run_id: str) -> None:
    """Called right after a run reaches state="saved" in either orchestrator
    (see app/orchestrator.py and app/orchestrator_streaming.py). Increments
    the customer's free-meeting counter only for a free-tier meeting that
    actually succeeded -- a paid (unlimited) meeting, or a run with no
    customer_id at all (an unpaired caller, or the admin-only legacy upload
    path), is a no-op. This is the ONLY place free_meetings_used increments:
    never at /meetings/start, so a meeting that fails or never captures
    audio never burns a free slot.
    """
    run = db.get_run(run_id)
    if (
        run is None
        or run.get("state") != "saved"
        or not run.get("customer_id")
        or run.get("billing_mode") != "free"
    ):
        return
    db.increment_free_meetings_used(run["customer_id"])


def is_fair_use_flagged(customer_id: str) -> bool:
    """Backs the admin panel's (Phase 7) "review this account" flag -- a
    paid customer crossing the alert threshold isn't blocked (see
    authorize_new_meeting's hard-ceiling-only check above), just surfaced.
    """
    return db.count_runs_this_month(customer_id) >= settings.fair_use_alert_threshold
