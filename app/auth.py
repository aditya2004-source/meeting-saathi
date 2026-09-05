"""Phase 1 (identity/auth/tenancy): customer identity, OTP verification,
device-token pairing, and the FastAPI dependencies that give every
meeting-scoped route a real, verified caller instead of a free-text name.

Two ways a request proves who it is:
  - Website: a signed session cookie (Starlette's SessionMiddleware, already
    used for the admin login) carrying request.session["customer_id"], set
    after OTP verification.
  - Extension: an `Authorization: Bearer <token>` header, minted once during
    the "Connect Account" pairing flow (see app.auth_routes) and stored by
    the extension in chrome.storage.local.

`/account/...` routes (app.auth_routes) depend on `get_current_customer`,
which requires one or the other. `/meetings/{run_id}/...` routes
(app.main) use `authorize_run_access` instead, which additionally
special-cases a run with no customer_id at all -- see its own docstring for
why that's a deliberate transitional rule, not a gap.
"""
import hashlib
import hmac
import secrets
import string

from fastapi import HTTPException, Request

from app import db
from app.config import settings

REQUIRED_CONSENT_POLICIES = [
    "terms_of_service",
    "privacy_policy",
    "refund_policy",
    "ai_disclaimer",
    "recording_consent",
]

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_MAX_REQUESTS_PER_WINDOW = 3
OTP_REQUEST_WINDOW_MINUTES = 15


def _pepper() -> bytes:
    # Reuses the existing session-signing secret as an HMAC key rather than
    # introducing a second secret to manage -- app.config already fails
    # startup if this is blank (see app/config.py's validate_settings()), so
    # this is never signing/verifying with an empty key.
    return settings.session_secret_key.encode("utf-8")


def _hash_otp(email: str, code: str) -> str:
    # HMAC, not a plain hash: a 6-digit code has only 10^6 possibilities, so
    # anyone with read access to otp_codes could otherwise brute-force it
    # offline in a fraction of a second. Keying on the server secret (which
    # never leaves the server) makes that infeasible even with DB access.
    return hmac.new(_pepper(), f"{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _hash_token(raw_token: str) -> str:
    # Device tokens are 256 bits of secrets.token_urlsafe() entropy -- unlike
    # the OTP, offline brute force is already infeasible without a pepper.
    # Still hashed at rest (never store the bearer secret in plaintext), same
    # principle as a password/API-key table.
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def generate_otp_code() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(OTP_LENGTH))


def issue_otp(email: str) -> str:
    """Creates and stores a new OTP for `email`, returning the raw code to
    send. Caller (app.auth_routes) is responsible for rate-limiting via
    otp_send_allowed() before calling this -- kept separate so the check and
    the side effect aren't silently coupled.
    """
    code = generate_otp_code()
    db.create_otp_code(email, _hash_otp(email, code), ttl_minutes=OTP_TTL_MINUTES)
    return code


def otp_send_allowed(email: str) -> bool:
    return db.count_recent_otp_requests(email, within_minutes=OTP_REQUEST_WINDOW_MINUTES) < OTP_MAX_REQUESTS_PER_WINDOW


def verify_otp(email: str, code: str) -> bool:
    """Checks `code` against the most recently issued OTP for `email`.
    Consumes it (so it can't be replayed) only on a correct match; a wrong
    code increments that same OTP's attempt counter instead of minting a new
    one, so OTP_MAX_ATTEMPTS actually bounds guesses against one code.
    """
    otp = db.get_latest_otp_code(email)
    if otp is None or otp["consumed_at"] is not None:
        return False
    if otp["attempt_count"] >= OTP_MAX_ATTEMPTS:
        return False
    if otp["expires_at"] < db._now():  # noqa: SLF001 - same ISO-8601 string comparison db.py uses elsewhere
        return False
    if not hmac.compare_digest(otp["code_hash"], _hash_otp(email, code)):
        db.increment_otp_attempt(otp["id"])
        return False
    db.consume_otp_code(otp["id"])
    return True


def has_required_consent(email: str) -> bool:
    return db.has_recorded_consent(email, REQUIRED_CONSENT_POLICIES)


def issue_device_token(customer_id: str, label: str = "") -> str:
    """Mints a new bearer token for the extension pairing flow. Returns the
    raw token exactly once -- only its hash is ever stored (create_device_token),
    same as the OTP/password pattern elsewhere in this module.
    """
    raw_token = secrets.token_urlsafe(32)
    db.create_device_token(customer_id, _hash_token(raw_token), label=label)
    return raw_token


def _customer_from_bearer_token(request: Request) -> dict | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    raw_token = header[len("bearer "):].strip()
    if not raw_token:
        return None
    token_row = db.get_active_device_token_by_hash(_hash_token(raw_token))
    if token_row is None:
        return None
    customer = db.get_customer(token_row["customer_id"])
    if customer is None or customer["status"] != "active":
        return None
    db.touch_device_token_last_used(token_row["id"])
    return customer


def _customer_from_session(request: Request) -> dict | None:
    customer_id = request.session.get("customer_id")
    if not customer_id:
        return None
    customer = db.get_customer(customer_id)
    if customer is None or customer["status"] != "active":
        return None
    return customer


def is_admin_session(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def require_owns_run(actor: dict, run: dict | None) -> dict:
    """Every /meetings/{run_id}/... route must call this (via
    authorize_run_access() below) before doing anything with `run`. Returns
    404 (not 403) for a run that exists but belongs to someone else -- same
    "don't confirm existence" philosophy app.main already uses for the admin
    slug (see _admin_not_found()) -- so a guessed run_id can't even be used
    to confirm another customer has a meeting with that id. An admin actor
    (`actor["is_admin"]`) bypasses the ownership check entirely: the admin's
    own unfiltered dashboard (/{slug}/dashboard) must keep working exactly
    as it does today against ANY run, including legacy rows with no
    customer_id at all -- that's existing behavior this phase must not
    break.
    """
    if run is None:
        raise HTTPException(status_code=404, detail="Not found")
    if actor["is_admin"]:
        return run
    if run.get("customer_id") != actor["customer"]["id"]:
        raise HTTPException(status_code=404, detail="Not found")
    return run


def _resolve_actor_optional(request: Request) -> dict | None:
    if is_admin_session(request):
        return {"is_admin": True}
    customer = _customer_from_session(request) or _customer_from_bearer_token(request)
    if customer is None:
        return None
    db.touch_customer_last_active(customer["id"])
    return {"is_admin": False, "customer": customer}


def authorize_run_access(request: Request, run: dict | None) -> dict:
    """Transitional access rule for every /meetings/{run_id}/... route,
    covering the gap between Phase 1 (this) and Phase 4/5 (the pairing UI
    and real customer dashboard actually existing): a run created before
    real identity existed -- or created by a caller who hasn't paired yet --
    has customer_id NULL, and stays exactly as open as it is today (no
    regression to the founder's own current, not-yet-re-paired usage). A run
    that DOES have a customer_id (created by an already-paired caller) is
    fully protected: admin always allowed, a customer must match, anyone
    else (including a wrong customer) gets 404.

    Once Phase 4 ships the pairing UI and every real caller sends a token,
    the practical effect converges to "always requires ownership" on its
    own, without needing a flag-day cutover that would otherwise strand
    whoever hasn't re-paired yet.
    """
    if run is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not run.get("customer_id"):
        return run
    actor = _resolve_actor_optional(request)
    if actor is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return require_owns_run(actor, run)


def get_current_customer_optional(request: Request) -> dict | None:
    """Like get_current_customer, but returns None instead of raising when
    no valid session/token is present -- used by /meetings/start so an
    unpaired caller (today's founder extension, before Phase 4's pairing UI
    exists) keeps working exactly as before (customer_id stays NULL on the
    created run), while an already-paired caller gets its run correctly
    attributed.
    """
    customer = _customer_from_session(request) or _customer_from_bearer_token(request)
    if customer is not None:
        db.touch_customer_last_active(customer["id"])
    return customer


def get_current_customer(request: Request) -> dict:
    """FastAPI dependency: resolves the caller's customer row from either a
    session cookie (website) or a bearer device token (extension), and
    touches last_active_at as a side effect (backs the admin "Active Users"
    metric -- see Phase 7). Raises 401 if neither is present/valid, so every
    route that depends on this can trust the returned customer is real,
    verified, and active without re-checking any of that itself.
    """
    customer = _customer_from_session(request) or _customer_from_bearer_token(request)
    if customer is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.touch_customer_last_active(customer["id"])
    return customer
