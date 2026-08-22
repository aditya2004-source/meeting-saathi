import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app import orchestrator_streaming
from app.config import settings
from app.orchestrator import process_recording
from app.pipeline.download import working_dir_for
from app.pipeline.merge import format_meeting_date
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

app = FastAPI(title="Meeting Saathi")
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

# The Chrome extension runs as an extension origin (chrome-extension://...),
# not a normal web origin, so it needs CORS allowed to POST recordings here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Backs the admin login session (see /{slug}/login etc. below) -- a signed,
# httponly cookie is enough for a single-owner admin panel, no server-side
# session store needed. 7 days so the owner doesn't have to re-login daily;
# session_cookie_https_only is True by default to match how this actually
# runs (see app/config.py's comment on that setting).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    max_age=7 * 24 * 60 * 60,
    https_only=settings.session_cookie_https_only,
)


_STAGE_HISTORY_PATH = settings.project_root / "data" / "stage_duration_history.json"

# Fixed allowlist for /meetings/{run_id}/files/{filename} below -- these are
# exactly the filenames app.orchestrator.py/app.orchestrator_streaming.py
# ever write into a saved meeting's folder. Never treat a request's
# `filename` path segment as a trusted filesystem path on its own (that
# would allow path traversal, e.g. `../../etc/passwd`); only ever serve one
# of these exact names.
_DOWNLOADABLE_FILES = {
    "MOM.pdf",
    "MOM.md",
    "Meeting_Analysis.pdf",
    "Meeting_Analysis.md",
    "transcript.txt",
    "transcript.json",
}


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
    available_files = _available_files(run.get("folder_path"))
    progress["available_files"] = available_files
    # Each of the two documents (MOM, Meeting Analysis) produces exactly
    # one .pdf -- counting those (not the .md siblings or transcript files)
    # gives "how many of the 2 documents are ready" for the dashboard's
    # partial-progress banner, without the template needing to know that.
    progress["documents_ready_count"] = sum(1 for f in available_files if f.endswith(".pdf"))
    return progress


@app.get("/")
def index(request: Request, name: str = ""):
    """`name` (from the extension's "View Dashboard" button, which passes
    its own stored user_name) scopes this to just that person's meetings.
    Without a `name`, there's nothing to scope by -- this is purely the
    customer-facing view now; the old unfiltered "everyone" view (gated by
    a static admin_token in this same query string) has moved to
    /{admin_url_slug}/dashboard behind a real login (see below), so `/` can
    never show every customer's meetings again under any query string.
    """
    name = name.strip()
    if not name:
        return HTMLResponse(
            "<p style='font-family: sans-serif; padding: 2rem;'>Pass your name "
            "(?name=...) to see your meetings.</p>",
            status_code=200,
        )
    runs = db.list_runs(user_name=name)
    for run in runs:
        run["started_display"] = _format_started(run["created_at"])
        run["progress"] = _progress_for_run(run)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "runs": runs, "usage": [], "viewing_name": name, "logout_url": ""},
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


@app.post("/{slug}/login")
def admin_login_submit(
    request: Request, slug: str, username: str = Form(...), password: str = Form(...)
):
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    if (
        settings.admin_username
        and settings.admin_password
        and username == settings.admin_username
        and password == settings.admin_password
    ):
        request.session["is_admin"] = True
        return RedirectResponse(url=f"/{slug}/dashboard", status_code=303)
    return RedirectResponse(url=f"/{slug}/login?error=1", status_code=303)


@app.get("/{slug}/logout")
def admin_logout(request: Request, slug: str):
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    request.session.clear()
    return RedirectResponse(url=f"/{slug}/login", status_code=303)


@app.get("/{slug}/dashboard")
def admin_dashboard(request: Request, slug: str):
    """The owner's own unfiltered view of everyone's meetings plus the
    "Usage by person" table -- today's admin_token content, unchanged, just
    gated by an unguessable path + a real login session instead of a static
    query-string secret.
    """
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return _admin_not_found()
    if not _is_admin_session(request):
        return RedirectResponse(url=f"/{slug}/login", status_code=303)
    runs = db.list_runs(user_name=None)
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
            "usage": usage,
            "viewing_name": "",
            "logout_url": f"/{slug}/logout",
        },
    )


def _daily_limit_response(user_name: str) -> JSONResponse | None:
    """Shared by every upload path (/meetings/start, /meetings/upload,
    /meetings/upload-form) so the daily cap can't be bypassed by using one
    of the fallback upload routes instead of the extension's normal
    /meetings/start -- returns a 429 JSONResponse if `user_name` is over
    settings.daily_meeting_limit, else None. An empty user_name is never
    rate-limited -- there's nothing to count it against.
    """
    if user_name and db.count_runs_today(user_name) >= settings.daily_meeting_limit:
        return JSONResponse(
            {
                "error": "daily_limit_reached",
                "message": (
                    f"Aaj ka limit ({settings.daily_meeting_limit} meetings) khatam ho gaya hai. "
                    "Kal phir try karo."
                ),
            },
            status_code=429,
        )
    return None


def _start_processing(
    title: str,
    content: bytes,
    filename: str,
    speaker_events: str | None = None,
    attendee_roster: str | None = None,
    user_name: str = "",
) -> dict:
    run = db.create_run(title=title.strip() or "Untitled Meeting", audio_path="", user_name=user_name)
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
    title: str = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
    user_name: str = Form(""),
):
    """Used by both the Chrome extension (automatic) and the manual form on
    the status page (fallback/testing): receives a finished recording and
    kicks off transcription -> diarization -> document generation -> save.
    `speaker_events` is optional JSON (see extension/DESIGN.md) used to
    resolve real speaker names instead of "Speaker N" placeholders.
    `attendee_roster` is optional JSON, the People-panel attendee list, used
    as the authoritative Attendees field instead of inferring it from who
    spoke. `user_name` is checked against the same daily limit as
    /meetings/start -- without this, the fallback upload routes would be an
    unlimited way around the cap.
    """
    user_name = user_name.strip()
    limit_response = _daily_limit_response(user_name)
    if limit_response is not None:
        return limit_response
    content = await audio.read()
    run = _start_processing(
        title, content, audio.filename or "recording.webm", speaker_events, attendee_roster, user_name
    )
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.post("/meetings/upload-form")
async def upload_meeting_form(title: str = Form(...), audio: UploadFile = File(...), user_name: str = Form("")):
    user_name = user_name.strip()
    if _daily_limit_response(user_name) is not None:
        return RedirectResponse(url="/?limit_reached=1", status_code=303)
    content = await audio.read()
    _start_processing(title, content, audio.filename or "recording.webm", user_name=user_name)
    return RedirectResponse(url="/", status_code=303)


@app.get("/meetings/{run_id}/status")
def meeting_status(run_id: str):
    """Polled every few seconds by app/web/static/status.js so the status
    page updates live without a manual reload -- see app/progress.py for
    the "progress" field's shape.
    """
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    work_dir = settings.working_dir / run_id
    timing = load_timing(work_dir) if work_dir.exists() else None
    if timing:
        run = {**run, "timing": timing}
    run["progress"] = _progress_for_run(run)
    return run


@app.post("/meetings/{run_id}/cancel")
def cancel_meeting(run_id: str, reason: str = Form("")):
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
    """
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    message = reason.strip() or "Recording was cancelled before any audio was captured"
    run = db.mark_failed(run_id, message)
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.get("/meetings/{run_id}/files/{filename}")
def download_meeting_file(run_id: str, filename: str):
    """Serves one file from a meeting's folder -- added for remote
    deployments (Railway etc.) where nobody has direct filesystem access to
    run["folder_path"] the way a local-machine user can just open their own
    Downloads folder. `filename` is checked against `_DOWNLOADABLE_FILES`
    rather than trusted as a path segment, so this can't be used to read
    arbitrary files off the server.

    Deliberately NOT gated on run["state"] == "saved" -- documents are
    written into the folder incrementally as each one finishes generating
    (see app.orchestrator_streaming.finalize_run()), so a document can be
    genuinely ready and worth serving well before the whole run is
    "saved". Whether the specific requested *file* actually exists on disk
    is the real source of truth for whether it's ready.
    """
    if filename not in _DOWNLOADABLE_FILES:
        return JSONResponse({"error": "not found"}, status_code=404)
    run = db.get_run(run_id)
    if run is None or not run.get("folder_path"):
        return JSONResponse({"error": "not found"}, status_code=404)
    file_path = Path(run["folder_path"]) / filename
    if not file_path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(file_path, filename=filename)


# --- Chunked/streaming pipeline (additive -- /meetings/upload and
# /meetings/upload-form above are untouched and keep working as the
# manual/testing fallback). The Chrome extension uses this trio instead:
# one /start to get a run_id before recording begins, repeated /chunk calls
# as MediaRecorder restart-cycles during the call, and one final /finalize
# call when the meeting ends. See app/orchestrator_streaming.py.


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
async def start_meeting(title: str = Form(...), user_name: str = Form("")):
    """`user_name` is a plain self-reported identifier (no login/password --
    see extension/popup.js's Setup section) used only to enforce
    settings.daily_meeting_limit and to populate the dashboard's usage-by-
    person table (see app.db.usage_summary()). An empty user_name (an
    extension that hasn't set one up yet) is never rate-limited -- there's
    nothing to count it against.
    """
    user_name = user_name.strip()
    limit_response = _daily_limit_response(user_name)
    if limit_response is not None:
        return limit_response
    run = db.create_run(title=title.strip() or "Untitled Meeting", audio_path="", user_name=user_name)
    working_dir_for(run["id"])
    run = db.update_run(run["id"], state="received")
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.post("/meetings/{run_id}/chunk")
async def upload_chunk(
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
):
    content = await audio.read()
    orchestrator_streaming.accept_chunk(
        run_id, sequence, content, speaker_events, attendee_roster, final=False
    )
    return JSONResponse({"id": run_id, "sequence": sequence, "accepted": True})


@app.post("/meetings/{run_id}/finalize")
async def finalize_meeting(
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
    attendee_roster: str | None = Form(None),
):
    content = await audio.read()
    orchestrator_streaming.accept_chunk(
        run_id, sequence, content, speaker_events, attendee_roster, final=True
    )
    return JSONResponse({"id": run_id, "state": "chunk_processing"})
