import datetime
import json
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app import orchestrator_streaming
from app.config import settings
from app.orchestrator import process_recording
from app.pipeline.download import working_dir_for
from app.pipeline.timing import load_stage_history, load_timing
from app.progress import describe_progress

app = FastAPI(title="Sarathi Meeting Bot")
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


_STAGE_HISTORY_PATH = settings.project_root / "data" / "stage_duration_history.json"


def _format_started(created_at: str) -> str:
    """Raw ISO timestamps (e.g. "2026-08-10T10:18:17.897755+00:00") aren't
    something a non-technical user should have to read -- shown on the
    dashboard as a plain local date/time instead."""
    try:
        dt = datetime.datetime.fromisoformat(created_at).astimezone()
    except (ValueError, TypeError):
        return created_at
    return dt.strftime("%-d %b %Y, %-I:%M %p")  # e.g. "10 Aug 2026, 4:06 PM"


def _load_chunk_durations(work_dir: Path) -> dict | None:
    path = work_dir / "chunk_durations.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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
    return describe_progress(
        state=run["state"],
        timing=timing,
        chunk_durations=chunk_durations,
        history=history,
        folder_path=run.get("folder_path"),
        error_message=run.get("error_message"),
    )


@app.get("/")
def index(request: Request):
    runs = db.list_runs()
    for run in runs:
        run["started_display"] = _format_started(run["created_at"])
        run["progress"] = _progress_for_run(run)
    return templates.TemplateResponse("index.html", {"request": request, "runs": runs})


def _start_processing(title: str, content: bytes, filename: str, speaker_events: str | None = None) -> dict:
    run = db.create_run(title=title.strip() or "Untitled Meeting", audio_path="")
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

    run = db.update_run(run["id"], audio_path=str(audio_path), state="received")

    thread = threading.Thread(target=process_recording, args=(run["id"],), daemon=True)
    thread.start()
    return run


@app.post("/meetings/upload")
async def upload_meeting(
    title: str = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
):
    """Used by both the Chrome extension (automatic) and the manual form on
    the status page (fallback/testing): receives a finished recording and
    kicks off transcription -> diarization -> document generation -> save.
    `speaker_events` is optional JSON (see extension/DESIGN.md) used to
    resolve real speaker names instead of "Speaker N" placeholders.
    """
    content = await audio.read()
    run = _start_processing(title, content, audio.filename or "recording.webm", speaker_events)
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.post("/meetings/upload-form")
async def upload_meeting_form(title: str = Form(...), audio: UploadFile = File(...)):
    content = await audio.read()
    _start_processing(title, content, audio.filename or "recording.webm")
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
def cancel_meeting(run_id: str):
    """Called by extension/background.js when startRecording() fails AFTER
    /meetings/start already created the DB row (e.g. tabCapture rejecting
    the request) -- without this, that row sits at state=received forever,
    since nothing else ever touches it again."""
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    run = db.mark_failed(run_id, "Recording was cancelled before any audio was captured")
    return JSONResponse({"id": run["id"], "state": run["state"]})


# --- Chunked/streaming pipeline (additive -- /meetings/upload and
# /meetings/upload-form above are untouched and keep working as the
# manual/testing fallback). The Chrome extension uses this trio instead:
# one /start to get a run_id before recording begins, repeated /chunk calls
# as MediaRecorder restart-cycles during the call, and one final /finalize
# call when the meeting ends. See app/orchestrator_streaming.py.


@app.post("/meetings/start")
async def start_meeting(title: str = Form(...)):
    run = db.create_run(title=title.strip() or "Untitled Meeting", audio_path="")
    working_dir_for(run["id"])
    run = db.update_run(run["id"], state="received")
    return JSONResponse({"id": run["id"], "state": run["state"]})


@app.post("/meetings/{run_id}/chunk")
async def upload_chunk(
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
):
    content = await audio.read()
    orchestrator_streaming.accept_chunk(run_id, sequence, content, speaker_events, final=False)
    return JSONResponse({"id": run_id, "sequence": sequence, "accepted": True})


@app.post("/meetings/{run_id}/finalize")
async def finalize_meeting(
    run_id: str,
    sequence: int = Form(...),
    audio: UploadFile = File(...),
    speaker_events: str | None = Form(None),
):
    content = await audio.read()
    orchestrator_streaming.accept_chunk(run_id, sequence, content, speaker_events, final=True)
    return JSONResponse({"id": run_id, "state": "chunk_processing"})
