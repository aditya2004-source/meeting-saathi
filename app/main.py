import hmac
import json
import logging
import threading
import time
import traceback
from pathlib import Path

import markdown as markdown_lib
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import admin_routes, auth, db, document_generation_state, entitlement
from app import orchestrator_streaming
from app.auth_routes import router as auth_router
from app.billing.routes import router as billing_router
from app.config import settings
from app.policies import seed_policies
from app.site_routes import router as site_router
from app.docgen import registry
from app.docgen.engine import business_processes_from_facts
from app.docgen.output import write_generated_document
from app.docgen.render_pdf import extract_mermaid_blocks
from app.orchestrator import process_recording
from app.pipeline.download import working_dir_for
from app.pipeline.merge import format_meeting_date, render_plain_text
from app.pipeline.timing import load_stage_history, load_timing
from app.progress import describe_progress

# A standalone handler on this specific logger (not logging.basicConfig())
# so it's unaffected by -- and doesn't affect -- uvicorn's own logging
# setup (uvicorn.config.LOGGING_CONFIG only configures its own "uvicorn.*"
# loggers, but relying on root-logger propagation here would be a subtler
# dependency on exactly how/when that runs relative to this import).
# stderr is what systemd/journalctl captures from this service either way.
logger = logging.getLogger("meeting_saathi")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)

app = FastAPI(
    title="Meeting Saathi",
    # See config.py's disable_api_docs -- off (docs/redoc/schema all public)
    # by default for local dev, set DISABLE_API_DOCS=true in production.
    docs_url=None if settings.disable_api_docs else "/docs",
    redoc_url=None if settings.disable_api_docs else "/redoc",
    openapi_url=None if settings.disable_api_docs else "/openapi.json",
)
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(auth_router)
app.include_router(site_router)
app.include_router(billing_router)
app.include_router(admin_routes.router)

# Seeds the four legal policies (ToS/Privacy/Refund/AI-Disclaimer) into the
# DB idempotently -- see app/policies.py. Same "module import == process
# startup" pattern as db.init_db()/the stale-run sweep below.
seed_policies()

# Every private/authenticated route gets `X-Robots-Tag: noindex, nofollow`
# rather than hand-adding the header to each one -- /dashboard (Phase
# 1/4/5), /account/* and /auth/* (Phase 1), and the admin path (whatever
# settings.admin_url_slug is set to) must never be crawlable/indexable.
# robots.txt/sitemap.xml (app.site_routes) independently never list these
# either -- this header is the backstop for a direct link or a crawler that
# ignores robots.txt.
_NOINDEX_PATH_PREFIXES = ("/dashboard", "/account", "/auth", "/billing", "/healthz")


@app.middleware("http")
async def _add_noindex_header_for_private_routes(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    is_admin_path = bool(settings.admin_url_slug) and path.startswith(f"/{settings.admin_url_slug}")
    if is_admin_path or path.startswith(_NOINDEX_PATH_PREFIXES):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response

# The Chrome extension runs as an extension origin (chrome-extension://...),
# not a normal web origin, so it needs CORS allowed to POST recordings here.
# A wildcard origin was fine when every route was unauthenticated anyway; now
# that real customer sessions/tokens exist (Phase 1), it's tightened to the
# specific origins that actually need it. `settings.cors_allowed_origins` is
# a comma-separated list (env-configurable, since the extension's origin --
# chrome-extension://<id> -- and the production website domain are both
# deployment-specific, not something to hardcode here).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Backs both the admin session (see /{slug}/login etc. below) and every
# customer session (see app.auth) -- a signed, httponly cookie is enough,
# no server-side session store needed. 30 days so a customer stays logged
# in across normal browser restarts/refreshes without re-verifying OTP each
# time, matching the product requirement that OTP is only for first login
# on a device, not every visit. session_cookie_https_only is True by
# default to match how this actually runs (see app/config.py's comment on
# that setting).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    max_age=30 * 24 * 60 * 60,
    https_only=settings.session_cookie_https_only,
)


# One-shot orphan sweep on startup: a meeting run left in a non-terminal state
# by a previous process (crash, `systemctl restart`, deploy) has no worker
# thread behind it anymore and would otherwise sit "in progress" on the
# dashboard forever. Runs here because uvicorn imports this module once at
# startup -- same "module import == process startup" pattern as app.db's
# own init_db() call. Threshold + disable via settings.stale_run_minutes.
_stale_runs_failed = db.fail_stale_runs(settings.stale_run_minutes)
if _stale_runs_failed:
    logger.info(
        "startup: marked %d stale in-progress meeting run(s) as failed",
        _stale_runs_failed,
    )


_STAGE_HISTORY_PATH = settings.project_root / "data" / "stage_duration_history.json"

# Fixed allowlist for /meetings/{run_id}/files/{filename} below -- these are
# exactly the filenames the pipeline (transcript/facts) or an on-demand
# document generation (app.docgen.registry) can ever write into a saved
# meeting's folder. Never treat a request's `filename` path segment as a
# trusted filesystem path on its own (that would allow path traversal, e.g.
# `../../etc/passwd`); only ever serve one of these exact names.
_DOWNLOADABLE_FILES = {"transcript.txt", "transcript.json", "facts.json"} | {
    filename for doc_key in registry.DOCUMENTS for filename in registry.filenames_for(doc_key)
}

# Phase 7: which doc_key a downloaded .pdf filename belongs to, for the
# "Document Usage" chart's download-event tracking.
_DOC_KEY_BY_PDF_FILENAME = {registry.filenames_for(doc_key)[1]: doc_key for doc_key in registry.DOCUMENTS}


def _format_started(created_at: str) -> str:
    """Raw ISO timestamps (e.g. "2026-08-10T10:18:17.897755+00:00") aren't
    something a non-technical user should have to read -- shown on the
    dashboard as a plain local date/time instead. Reuses the same
    settings.report_timezone-based formatter as the generated documents, so
    the dashboard and MOM/RG/AP show the identical date/time for a run."""
    return format_meeting_date(created_at)


def _load_chunk_durations(work_dir: Path) -> dict | None:
    path = work_dir / "chunk_durations.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _available_files(folder_path: str | None) -> list[str]:
    """Which of the allowlisted document files actually exist yet in a
    meeting's folder -- computed fresh from the filesystem (not a DB flag),
    since documents are now written incrementally as each one finishes
    generating (see app.orchestrator_streaming.finalize_run()) rather than
    all appearing at once when the run reaches state "saved". Used by both
    the dashboard template and /meetings/{id}/status so a document shows up
    for download the moment it exists, without waiting for its sibling.
    """
    if not folder_path:
        return []
    folder = Path(folder_path)
    if not folder.is_dir():
        return []
    return sorted(name for name in _DOWNLOADABLE_FILES if (folder / name).is_file())


def _read_json_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _document_statuses(run: dict) -> list[dict]:
    """Per-document status for the dashboard's document catalogue (see
    app.docgen.registry) -- MOM, BRD, FRD, User Stories, Acceptance Criteria,
    Business Process Flow, etc. are no longer generated automatically, so each
    one's state has to be surfaced individually: "ready" (its .pdf already
    exists in the folder -- computed fresh from the filesystem, the same
    philosophy _available_files() already uses), "generating" (a background
    thread is working on it right now -- see document_generation_state),
    "failed" (that thread's last attempt raised), "unavailable" (nothing to
    generate it from -- e.g. Business Process Flow when no process walkthrough
    was extracted), or "not_generated" (the normal starting state, waiting for
    a "Generate" click).
    """
    folder_path = run.get("folder_path")
    folder = Path(folder_path) if folder_path else None
    facts = _read_json_file(folder / "facts.json") if folder else None
    facts_ready = facts is not None

    statuses = []
    for doc_key, doc in registry.DOCUMENTS.items():
        _, pdf_name = registry.filenames_for(doc_key)
        ready = bool(folder and (folder / pdf_name).is_file())
        if ready:
            status = "ready"
        elif document_generation_state.is_generating(run["id"], doc.group):
            status = "generating"
        elif document_generation_state.get_failure(run["id"], doc.group):
            status = "failed"
        elif doc_key == "business_process_flow" and facts_ready and not business_processes_from_facts(facts or {}):
            status = "unavailable"
        elif not facts_ready:
            # Distinct from "not_generated" -- clicking Generate now would just
            # 409 (see /documents/{key}/generate's own facts.json check), which
            # used to fail silently in the dashboard with no explanation. Either
            # this run hasn't finished processing yet, or (for a meeting saved
            # before on-demand generation existed) it predates facts.json
            # entirely and never will have one without a manual backfill.
            status = "facts_not_ready"
        else:
            status = "not_generated"
        statuses.append(
            {
                "key": doc_key,
                "label": doc.label,
                "status": status,
                "error": document_generation_state.get_failure(run["id"], doc.group) if status == "failed" else None,
                "pdf_filename": pdf_name if ready else None,
            }
        )
    return statuses


def _progress_for_run(run: dict) -> dict:
    """Live status shown on the `/` page and returned by
    /meetings/{id}/status -- see app/progress.py for what each field means
    and why. work_dir (and its timing.json/chunk_durations.json) only
    exists while a run is still in flight; for a terminal run (saved/
    failed) describe_progress() still works fine with timing=None, since it
    only reads timing/chunk_durations for the in-progress states.
    """
    work_dir = settings.working_dir / run["id"]
    timing = load_timing(work_dir) if work_dir.exists() else None
    chunk_durations = _load_chunk_durations(work_dir) if work_dir.exists() else None
    history = load_stage_history(_STAGE_HISTORY_PATH)
    progress = describe_progress(
        state=run["state"],
        timing=timing,
        chunk_durations=chunk_durations,
        history=history,
        folder_path=run.get("folder_path"),
        error_message=run.get("error_message"),
    )
    progress["available_files"] = _available_files(run.get("folder_path"))
    progress["documents"] = _document_statuses(run)
    return progress


@app.get("/dashboard")
def index(request: Request, client: str = ""):
    """The customer-facing meetings dashboard, scoped to the logged-in
    customer's own real customer_id.

    Production-simplification: this used to fall back to an anonymous
    `?name=...`-scoped view (today's founder extension, before Phase 4's
    pairing UI existed) whenever no session was present -- confusing for a
    real customer whose session merely expired, since it silently showed a
    "pass your name" box instead of prompting login. Every real caller now
    pairs through the install flow and carries a real session/device token,
    so an unauthenticated visit here goes straight to /login (round-tripping
    back here via `next` on success) rather than that legacy fallback. A
    stray `?name=` from an old extension popup build is simply ignored, not
    an error (FastAPI drops undeclared query params silently).

    Moved here from `/` in Phase 4 -- `/` is now the public marketing
    landing page (see app/site_routes.py). Private route: noindex, matching
    every other authenticated/account-scoped page (see the noindex
    middleware above, which covers this by path prefix).
    """
    client = client.strip()
    customer = auth.get_current_customer_optional(request)
    if customer is None:
        return RedirectResponse(url="/login?next=/dashboard", status_code=303)
    runs = db.list_runs(customer_id=customer["id"], client_name=client or None)
    for run in runs:
        run["started_display"] = _format_started(run["created_at"])
        run["progress"] = _progress_for_run(run)
    subscription = db.get_active_subscription(customer["id"])
    trial_exhausted = subscription is None and customer["free_meetings_used"] >= settings.free_meeting_allowance
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "runs": runs,
            "usage": [],
            "viewing_name": customer["name"] or customer["email"],
            "logout_url": "/auth/logout",
            "client_filter": client,
            "client_names": db.distinct_client_names(customer_id=customer["id"]),
            "customer": customer,
            "subscription": subscription,
            "free_meeting_allowance": settings.free_meeting_allowance,
            "trial_exhausted": trial_exhausted,
        },
    )


@app.get("/account")
def account_page(request: Request):
    """Plan/status + links to versioned policies, per Phase 5's scope.
    "Manage subscription" (Phase 6) and the delete-my-data/delete-my-account
    actions (Phase 8) aren't wired to anything real yet -- shown as
    explicitly disabled rather than faked, since a fake "requested" toast
    with no real effect would be worse than not having the button at all.
    Requires a real session -- unlike /dashboard, there's no anonymous
    fallback that makes sense for an account settings page. Redirects to
    /login (a returning-user's session most likely just expired), not
    /signup, carrying `next` so the customer lands back here after OTP.
    """
    customer = auth.get_current_customer_optional(request)
    if customer is None:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    subscription = db.get_active_subscription(customer["id"])
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "customer": customer,
            "subscription": subscription,
            "free_meeting_allowance": settings.free_meeting_allowance,
        },
    )


def _admin_not_found() -> JSONResponse:
    """Any slug other than settings.admin_url_slug 404s exactly like a
    route that doesn't exist, rather than revealing "wrong password" (which
    would confirm admin functionality lives there at all). Reuses the same
    JSONResponse 404 pattern already used elsewhere in this file (e.g.
    download_meeting_file above).
    """
    return JSONResponse({"error": "not found"}, status_code=404)


def _is_admin_session(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


@app.get("/{slug}/login")
def admin_login_form(request: Request, slug: str):
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    error = bool(request.query_params.get("error"))
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "slug": slug, "error": error}
    )


# Basic brute-force guard on admin login -- in-memory (not DB-backed) is
# enough here: this is a single-owner login behind an already-unguessable
# slug, not a high-traffic endpoint worth a real rate-limit library or
# surviving a restart. Keyed by client IP; a handful of failures within the
# window blocks further attempts from that IP until it ages out.
_ADMIN_LOGIN_MAX_FAILURES = 5
_ADMIN_LOGIN_WINDOW_SECONDS = 300
_admin_login_failures: dict[str, list[float]] = {}


def _admin_login_rate_limited(client_ip: str) -> bool:
    import time

    now = time.monotonic()
    attempts = [t for t in _admin_login_failures.get(client_ip, []) if now - t < _ADMIN_LOGIN_WINDOW_SECONDS]
    _admin_login_failures[client_ip] = attempts
    return len(attempts) >= _ADMIN_LOGIN_MAX_FAILURES


def _record_admin_login_failure(client_ip: str) -> None:
    import time

    _admin_login_failures.setdefault(client_ip, []).append(time.monotonic())


def _credentials_match(username: str, password: str) -> bool:
    # hmac.compare_digest instead of == -- a plain string compare short-
    # circuits on the first mismatched character, which leaks (via response
    # timing) how many characters of the password were guessed correctly.
    # Real money/customer data sits behind this login now (Phase 1+), so
    # that's worth closing even though it's a narrow, hard-to-exploit gap.
    return bool(
        settings.admin_username
        and settings.admin_password
        and hmac.compare_digest(username, settings.admin_username)
        and hmac.compare_digest(password, settings.admin_password)
    )


@app.post("/{slug}/login")
def admin_login_submit(
    request: Request, slug: str, username: str = Form(...), password: str = Form(...)
):
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    client_ip = request.client.host if request.client else ""
    # Production-audit fix: _admin_login_rate_limited()/_record_admin_login_failure()
    # were fully implemented above but never actually called anywhere --
    # dead code, so there was no real lockout despite the timing-safe
    # credential check. Checked before comparing credentials, so a blocked
    # IP can't even use a correct-guess attempt to probe past the limit.
    if _admin_login_rate_limited(client_ip):
        return RedirectResponse(url=f"/{slug}/login?error=1", status_code=303)
    if _credentials_match(username, password):
        request.session["is_admin"] = True
        return RedirectResponse(url=f"/{slug}/dashboard", status_code=303)
    _record_admin_login_failure(client_ip)
    return RedirectResponse(url=f"/{slug}/login?error=1", status_code=303)


@app.get("/{slug}/logout")
def admin_logout(request: Request, slug: str):
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    request.session.clear()
    return RedirectResponse(url=f"/{slug}/login", status_code=303)


@app.get("/{slug}/dashboard")
def admin_dashboard(request: Request, slug: str, client: str = ""):
    """The owner's own unfiltered view of everyone's meetings plus the
    "Usage by person" table -- today's admin_token content, unchanged, just
    gated by an unguessable path + a real login session instead of a static
    query-string secret. `client` is the same optional client/project filter
    as the customer-facing `/` view.
    """
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    if not _is_admin_session(request):
        return RedirectResponse(url=f"/{slug}/login", status_code=303)
    client = client.strip()
    runs = db.list_runs(user_name=None, client_name=client or None)
    for run in runs:
        run["started_display"] = _format_started(run["created_at"])
        run["progress"] = _progress_for_run(run)
    usage = db.usage_summary()
    for entry in usage:
        entry["last_active_display"] = _format_started(entry["last_active"])
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "runs": runs,
            "client_filter": client,
            "client_names": db.distinct_client_names(),
            "usage": usage,
            "viewing_name": "",
            "logout_url": f"/{slug}/logout",
            "admin_analytics_url": f"/{slug}/overview",
        },
    )


def _start_processing(
    title: str,
    content: bytes,
    filename: str,
    speaker_events: str | None = None,
    attendee_roster: str | None = None,
    user_name: str = "",
    client_name: str = "",
    device_id: str = "",
) -> dict:
    run = db.create_run(
        title=title.strip() or "Untitled Meeting",
        audio_path="",
        user_name=user_name,
        client_name=client_name,
        device_id=device_id,
    )
    work_dir = working_dir_for(run["id"])
    suffix = Path(filename or "recording.webm").suffix or ".webm"
    audio_path = work_dir / f"original{suffix}"
    audio_path.write_bytes(content)

    if speaker_events:
        # Best-effort sidecar from the extension's speaking-indicator
        # observer (see extension/DESIGN.md) -- validity is checked by
        # app.pipeline.speaker_names when the orchestrator reads it back,
        # so a malformed value here just means no names get resolved.
        try:
            json.loads(speaker_events)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            (work_dir / "speaker_events.json").write_text(speaker_events, encoding="utf-8")

    if attendee_roster:
        # Best-effort sidecar from the extension's People-panel scrape (see
        # extension/DESIGN.md) -- validity is checked by
        # app.pipeline.roster when the orchestrator reads it back, so a
        # malformed value here just means no roster is available.
        try:
            json.loads(attendee_roster)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            (work_dir / "attendee_roster.json").write_text(attendee_roster, encoding="utf-8")

    run = db.update_run(run["id"], audio_path=str(audio_path), state="received")

    thread = threading.Thread(target=process_recording, args=(run["id"],), daemon=True)
    thread.start()
    return run


@app.post("/meetings/upload")
async def upload_meeting(
    request: Request,
    title: str = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
    user_name: str = Form(""),
    client_name: str = Form(""),
    device_id: str = Form(""),
):
    """The legacy whole-file upload path (app.orchestrator, not the chunked/
    streaming pipeline the real extension uses). Confirmed (Phase 1 audit)
    that the actual server-backed extension never calls this -- it's only
    reachable via the manual "testing / fallback" form on the admin
    dashboard (see app/web/templates/index.html), so it's gated admin-only
    rather than built out for multi-tenant customer use.
    """
    if not auth.is_admin_session(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    content = await audio.read()
    run = _start_processing(
        title,
        content,
        audio.filename or "recording.webm",
        speaker_events,
        attendee_roster,
        user_name.strip(),
        client_name.strip(),
        device_id.strip(),
    )
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.post("/meetings/upload-form")
async def upload_meeting_form(
    request: Request,
    title: str = Form(...),
    audio: UploadFile = File(...),
    user_name: str = Form(""),
    client_name: str = Form(""),
):
    # Same admin-only gate as /meetings/upload above -- this form only ever
    # renders on the admin's own unfiltered dashboard view (see
    # app/web/templates/index.html's `{% else %}` branch, shown only when
    # viewing_name is empty, i.e. never on a customer's `/?name=...` view).
    if not auth.is_admin_session(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    content = await audio.read()
    _start_processing(
        title, content, audio.filename or "recording.webm", user_name=user_name.strip(), client_name=client_name.strip()
    )
    return RedirectResponse(url="/", status_code=303)


@app.get("/meetings/{run_id}/status")
def meeting_status(request: Request, run_id: str):
    """Polled every few seconds by app/web/static/status.js so the status
    page updates live without a manual reload -- see app/progress.py for
    the "progress" field's shape. See auth.authorize_run_access() for who's
    allowed to see this: unrestricted for a legacy/unpaired run (customer_id
    NULL), owner-or-admin-only once a run has a real customer_id.
    """
    run = auth.authorize_run_access(request, db.get_run(run_id))
    work_dir = settings.working_dir / run_id
    timing = load_timing(work_dir) if work_dir.exists() else None
    if timing:
        run = {**run, "timing": timing}
    run["progress"] = _progress_for_run(run)
    return run


@app.post("/meetings/{run_id}/cancel")
def cancel_meeting(request: Request, run_id: str, reason: str = Form("")):
    """Called by extension/background.js in two distinct situations that
    both used to write the exact same generic message, making them
    impossible to tell apart later from the dashboard alone: (1)
    startRecording() fails AFTER /meetings/start already created the DB
    row (e.g. tabCapture rejected the request) -- no audio was ever
    captured; (2) stopRecording()'s final upload/finalize fails after a
    real recording happened -- audio WAS captured, just never made it to
    the server. `reason` (now sent by the extension) lets each call site
    say which one actually happened; falls back to the old generic
    message for any older extension install that doesn't send it.

    See auth.authorize_run_access() for who's allowed to cancel: same
    transitional owner-or-admin-or-unclaimed rule as /status above.
    """
    run = auth.authorize_run_access(request, db.get_run(run_id))
    message = reason.strip() or "Recording was cancelled before any audio was captured"
    run = db.mark_failed(run_id, message)
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.get("/meetings/{run_id}/files/{filename}")
def download_meeting_file(request: Request, run_id: str, filename: str):
    """Serves one file from a meeting's folder -- added for remote
    deployments (Railway etc.) where nobody has direct filesystem access to
    run["folder_path"] the way a local-machine user can just open their own
    Downloads folder. `filename` is checked against `_DOWNLOADABLE_FILES`
    rather than trusted as a path segment, so this can't be used to read
    arbitrary files off the server. See auth.authorize_run_access() for who's
    allowed to download: same transitional owner-or-admin-or-unclaimed rule
    used by every other /meetings/{run_id}/... route -- this was previously
    the most exploitable of the unauthenticated routes, since a bare run_id
    was enough to read another customer's transcript/documents outright.

    Deliberately NOT gated on run["state"] == "saved" -- documents are
    written into the folder incrementally as each one finishes generating
    (see app.orchestrator_streaming.finalize_run()), so a document can be
    genuinely ready and worth serving well before the whole run is
    "saved". Whether the specific requested *file* actually exists on disk
    is the real source of truth for whether it's ready.
    """
    if filename not in _DOWNLOADABLE_FILES:
        return JSONResponse({"error": "not found"}, status_code=404)
    run = auth.authorize_run_access(request, db.get_run(run_id))
    if not run.get("folder_path"):
        return JSONResponse({"error": "not found"}, status_code=404)
    file_path = Path(run["folder_path"]) / filename
    if not file_path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    if filename in _DOC_KEY_BY_PDF_FILENAME:
        db.record_document_event(run_id, _DOC_KEY_BY_PDF_FILENAME[filename], "downloaded")
    return FileResponse(file_path, filename=filename)


@app.get("/dashboard/meetings/{run_id}/documents/{doc_key}")
def view_document(request: Request, run_id: str, doc_key: str):
    """Phase 5's in-browser document viewer -- previously the dashboard only
    ever linked to the Playwright-rendered PDF (see download_meeting_file
    above, still available alongside this). Renders the same `.md` source
    client-side: python-markdown for the prose, and the same
    extract_mermaid_blocks() transform the PDF path uses (see
    app/docgen/render_pdf.py) so the Business Process Flow's fenced diagram
    becomes a <pre class="mermaid"> node the browser's own vendored
    mermaid.min.js renders live, instead of needing headless Chrome.
    """
    if doc_key not in registry.DOCUMENTS:
        return HTMLResponse("Not found", status_code=404)
    run = auth.authorize_run_access(request, db.get_run(run_id))
    if not run.get("folder_path"):
        return HTMLResponse("Not ready yet", status_code=404)
    md_filename, pdf_filename = registry.filenames_for(doc_key)
    md_path = Path(run["folder_path"]) / md_filename
    if not md_path.is_file():
        return HTMLResponse("Not ready yet", status_code=404)
    transformed, has_mermaid = extract_mermaid_blocks(md_path.read_text(encoding="utf-8"))
    body_html = markdown_lib.markdown(transformed, extensions=["tables", "fenced_code"])
    db.record_document_event(run_id, doc_key, "viewed")
    return templates.TemplateResponse(
        "document_view.html",
        {
            "request": request,
            "run": run,
            "doc_label": registry.DOCUMENTS[doc_key].label,
            "body_html": body_html,
            "has_mermaid": has_mermaid,
            "pdf_filename": pdf_filename,
        },
    )


@app.post("/meetings/{run_id}/client")
def set_meeting_client(request: Request, run_id: str, client_name: str = Form("")):
    """Post-hoc "set client/project" edit. The extension's popup has an
    optional client-name field too (see extension/popup.html), but most real
    meetings start automatically without the popup ever opening -- this route
    is the primary way a client/project actually gets attached in practice,
    from the dashboard, any time after the meeting. See
    auth.authorize_run_access() for who's allowed to edit this.
    """
    auth.authorize_run_access(request, db.get_run(run_id))
    run = db.set_client_name(run_id, client_name.strip())
    return JSONResponse({"id": run["id"], "client_name": run["client_name"]})


def _generate_document_group(run_id: str, group_key: str) -> None:
    """Background-thread target for the on-demand generate route below --
    runs the one Gemini call (or, for a "local" group like FRD/Business
    Process Flow, the zero-Gemini-call deterministic render) that produces
    every document in this group, then writes each one's .md/.mmd + .pdf into
    the meeting folder. A meeting can have several of these running
    concurrently (different groups, or even the same group from a retried
    click -- guarded by document_generation_state.mark_generating()'s
    already-in-progress check), so this must never touch shared server state
    other than through the already-thread-safe pieces it calls into
    (document_generation_state's lock, render_pdf.py's/render_diagram.py's
    shared Playwright _RENDER_LOCK, write_meeting_file()'s atomic per-file
    writes).
    """
    try:
        run = db.get_run(run_id)
        if run is None or not run.get("folder_path"):
            return
        folder = Path(run["folder_path"])
        facts = _read_json_file(folder / "facts.json")
        transcript = _read_json_file(folder / "transcript.json")
        if facts is None or transcript is None:
            document_generation_state.mark_failed(run_id, group_key, "Transcript/facts not ready yet")
            return

        group = registry.GROUPS[group_key]
        result = group.generator(
            run["title"],
            transcript.get("meeting_date_display", ""),
            transcript.get("attendees") or [],
            facts,
            render_plain_text(transcript),
        )
        if result is None:
            # Genuinely nothing to generate (e.g. Business Process Flow when no
            # process walkthrough was extracted) -- not a failure, see
            # _document_statuses()'s "unavailable" status.
            return

        for produced_key, content in result.items():
            write_generated_document(folder, produced_key, content, facts)
            db.record_document_event(run_id, produced_key, "generated")
    except Exception as exc:  # noqa: BLE001 - one document's failure must not crash the server
        traceback.print_exc()
        document_generation_state.mark_failed(run_id, group_key, str(exc))
    finally:
        document_generation_state.mark_done(run_id, group_key)


@app.post("/meetings/{run_id}/documents/{doc_key}/generate")
def generate_document(request: Request, run_id: str, doc_key: str):
    """The on-demand generation trigger -- every document (MOM, Meeting
    Analysis, Business Process Flow) is generated only when a user
    explicitly asks for it from the dashboard, never automatically (see
    app.orchestrator_streaming.finalize_run(), which now stops at
    facts.json). Returns immediately; the caller should poll
    /meetings/{run_id}/status (progress.documents) for status, same pattern
    as chunk/finalize uploads. See auth.authorize_run_access() for who's
    allowed to trigger this -- otherwise anyone with a run_id could burn
    another customer's Gemini quota/cost on demand.
    """
    if doc_key not in registry.DOCUMENTS:
        return JSONResponse({"error": "unknown document"}, status_code=404)
    run = auth.authorize_run_access(request, db.get_run(run_id))
    if not run.get("folder_path"):
        return JSONResponse({"error": "not found"}, status_code=404)
    folder = Path(run["folder_path"])
    if not (folder / "facts.json").is_file() or not (folder / "transcript.json").is_file():
        return JSONResponse({"error": "not_ready", "message": "Transcript/facts not ready yet"}, status_code=409)

    group_key = registry.DOCUMENTS[doc_key].group
    if document_generation_state.mark_generating(run_id, group_key):
        thread = threading.Thread(target=_generate_document_group, args=(run_id, group_key), daemon=True)
        thread.start()
    return JSONResponse({"id": run_id, "doc_key": doc_key, "status": "generating"})


# --- Chunked/streaming pipeline (additive -- /meetings/upload and
# /meetings/upload-form above are untouched and keep working as the
# manual/testing fallback). The Chrome extension uses this trio instead:
# one /start to get a run_id before recording begins, repeated /chunk calls
# as MediaRecorder restart-cycles during the call, and one final /finalize
# call when the meeting ends. See app/orchestrator_streaming.py.


@app.get("/healthz")
def healthz():
    """Phase 9: for the reverse proxy (Caddy) and any external uptime
    monitor to check -- no auth (a health check endpoint that itself
    requires a login isn't useful to infrastructure that doesn't have
    one), no expensive work, just proof the process is alive and the
    database is actually reachable (not just that the process started).
    """
    try:
        db.get_run("healthz-check-nonexistent-id")
        db_ok = True
    except Exception:  # noqa: BLE001 - report unhealthy, don't crash the health check itself
        db_ok = False
    status_code = 200 if db_ok else 503
    return JSONResponse({"ok": db_ok, "db": db_ok}, status_code=status_code)


@app.post("/debug/log")
async def debug_log(source: str = Form(...), event: str = Form(...), detail: str = Form("")):
    """Fire-and-forget breadcrumb channel for the extension (background.js/
    offscreen.js), added purely to diagnose a real stuck-recording bug live
    -- the two Chrome contexts that matter (the offscreen document, the
    service worker) are exactly the two surfaces neither browser automation
    nor manual copy-paste have reliably reached this session, but this
    machine's own journalctl has been the one source of truth for
    everything confirmed so far. No auth, no persistence beyond the log --
    intentionally cheap so it's safe to leave calling this indefinitely.
    """
    logger.info("EXT-DEBUG source=%s event=%s detail=%s", source, event, detail[:500])
    return JSONResponse({"ok": True})


@app.post("/meetings/start")
async def start_meeting(
    customer: dict = Depends(auth.get_current_customer),
    title: str = Form(...),
    user_name: str = Form(""),
    client_name: str = Form(""),
    device_id: str = Form(""),
):
    """`user_name` is a plain self-reported identifier (no login/password --
    see extension/popup.js's Setup section) used to populate the dashboard's
    usage-by-person table (see app.db.usage_summary()). `client_name` is an
    optional client/project label, purely for dashboard organization/
    filtering -- most real meetings start automatically without the popup
    ever opening, so it's commonly empty here and attached later via
    POST /meetings/{run_id}/client instead. `device_id` is a stable
    per-extension-install id (see extension/background.js) so retyping a
    different display name doesn't fragment one person's usage history in the
    admin panel.

    Phase 0 production-audit fix: a real, resolved customer identity (session
    cookie or a paired extension's bearer device token) is now REQUIRED to
    start a meeting -- `Depends(auth.get_current_customer)` raises 401
    before this body even runs otherwise. The previous "anonymous caller
    skips entitlement entirely" branch was a genuine trial/payment bypass:
    anyone could POST here directly with no login and no token and get
    unlimited meetings fully processed for free, since a NULL customer_id
    run was (and for pre-existing historical runs, still is -- see
    auth.authorize_run_access()) treated as unguarded on every other
    /meetings/{run_id}/... route too. Every NEW run from here on always has
    a real customer_id and always goes through app.entitlement's free-trial/
    subscription check (3 free meetings, then an active subscription, else
    402 trial_exhausted).
    """
    billing_mode = entitlement.authorize_new_meeting(customer)
    run = db.create_run(
        title=title.strip() or "Untitled Meeting",
        audio_path="",
        user_name=user_name.strip(),
        client_name=client_name.strip(),
        device_id=device_id.strip(),
        customer_id=customer["id"],
        billing_mode=billing_mode,
    )
    working_dir_for(run["id"])
    run = db.update_run(run["id"], state="received")
    return JSONResponse({"id": run["id"], "state": run["state"]})


# The extension's fixed MediaRecorder restart-cycle interval (see
# `Admin personal/extension(personal)/offscreen.js`'s CHUNK_INTERVAL_MS) --
# elapsed meeting time is knowable server-side from `sequence` alone,
# without any new state, since every chunk is ~this long.
_CHUNK_INTERVAL_SECONDS = 50


def _duration_cap_exceeded(sequence: int) -> bool:
    max_sequence = settings.max_meeting_duration_seconds // _CHUNK_INTERVAL_SECONDS
    return sequence > max_sequence


@app.post("/meetings/{run_id}/chunk")
async def upload_chunk(
    request: Request,
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
):
    auth.authorize_run_access(request, db.get_run(run_id))
    if _duration_cap_exceeded(sequence):
        # Audio past the cap is simply dropped -- never transcribed, never
        # billed. The extension keeps recording/uploading past this point
        # (a follow-up UX improvement to stop proactively is Phase 4/5
        # polish, not required for this safety cap to work).
        return JSONResponse({"id": run_id, "sequence": sequence, "accepted": False, "reason": "duration_cap_exceeded"})
    content = await audio.read()
    orchestrator_streaming.accept_chunk(
        run_id, sequence, content, speaker_events, attendee_roster, final=False
    )
    return JSONResponse({"id": run_id, "sequence": sequence, "accepted": True})


@app.post("/meetings/{run_id}/finalize")
async def finalize_meeting(
    request: Request,
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
):
    # Deliberately NOT duration-capped like upload_chunk above: this call
    # must always be allowed through so a meeting that ran past the cap
    # still properly wraps up and saves whatever was already gathered,
    # rather than being left stuck in "chunk_processing" forever (later
    # swept to "failed" by db.fail_stale_runs(), losing a real transcript
    # for no benefit). The cap already did its job by then -- every /chunk
    # call past the threshold was dropped (see upload_chunk), so at most
    # one final ~50s chunk's worth of audio is ever processed beyond it.
    auth.authorize_run_access(request, db.get_run(run_id))
    content = await audio.read()
    orchestrator_streaming.accept_chunk(
        run_id, sequence, content, speaker_events, attendee_roster, final=True
    )
    return JSONResponse({"id": run_id, "state": "chunk_processing"})
