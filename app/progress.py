"""Maps a meeting run's raw state (DB state column + timing.json +
chunk_durations.json + the persisted cross-run stage-duration history) into
a human status: a friendly stage label, a coarse percent, an ETA, and
whether audio was actually captured. Pure functions, no I/O -- app/main.py
is responsible for loading the run/timing/history and handing them here, so
this is trivially unit-testable and reusable by both the `/` status page
and the `/meetings/{id}/status` JSON endpoint.

Design choices worth calling out:
- A percent/ETA is only ever given for the *bounded* post-meeting tail
  (extract facts -> generate 2 docs -> render 2 PDFs -> save). While a
  meeting is still being recorded (chunk_processing, or legacy
  transcribing/diarizing), duration is fundamentally open-ended -- a
  fabricated countdown there would be actively misleading, so this
  deliberately shows no percent/ETA at all in that phase, only a live
  "how much has been captured so far" signal instead.
- An ETA is only shown when the persisted history has data for *every*
  remaining tail stage. Treating a not-yet-seen stage as "0s" would silently
  understate the true remaining time, which is worse than showing nothing.
"""
from typing import Optional

# Timing.json keys that make up the bounded post-meeting tail, in roughly
# the order they occur (extract_facts always finishes before the 2
# generate_* calls start; the 2 render_*_pdf calls run one after another
# once both generate_* calls finish; save_meeting_folder is last). Shared with
# orchestrator.py/orchestrator_streaming.py, which use this same list to
# decide which of TimingRecorder's keys are worth folding into the
# persisted cross-run history (app.pipeline.timing.record_stage_durations)
# -- chunk_transcribe/chunk_pyannote/etc. aren't part of the bounded tail
# and are intentionally excluded from that history.
TAIL_STAGE_KEYS = [
    "extract_facts",
    "generate_mom",
    "generate_meeting_analysis",
    "render_mom_pdf",
    "render_meeting_analysis_pdf",
    "save_meeting_folder",
]

_GENERATE_STAGE_LABELS = [
    ("generate_mom", "MOM"),
    ("generate_meeting_analysis", "Meeting Analysis"),
]

_STATE_LABELS = {
    "idle": "Waiting to start...",
    "received": "Recording received, starting up...",
    "transcribing": "Transcribing...",
    "diarizing": "Diarizing speakers...",
    "rendering": "Rendering PDFs...",
    "saving": "Saving files...",
    "saved": "Completed",
    "failed": "Failed",
}

_LIVE_STATES = {"received", "chunk_processing", "transcribing", "diarizing"}
_TAIL_STATES = {"generating_docs", "rendering", "saving"}

# A coarse, always-simple category for the status badge -- deliberately NOT
# the raw DB state string (e.g. "chunk_processing", "generating_docs"),
# which is internal pipeline vocabulary meaningless to someone just checking
# whether their meeting is done. The longer `label` (below) stays available
# as detail text for anyone who wants it; this is just for the badge.
_SIMPLE_STATUS = {
    "idle": "Starting",
    "received": "Starting",
    "chunk_processing": "Recording",
    "transcribing": "Recording",
    "diarizing": "Recording",
    "generating_docs": "Processing",
    "rendering": "Processing",
    "saving": "Processing",
    "saved": "Completed",
    "failed": "Failed",
}


def _generating_docs_label(timing: dict) -> str:
    pending = [name for key, name in _GENERATE_STAGE_LABELS if key not in timing]
    if not pending:
        return "Finishing document generation..."
    if len(pending) == len(_GENERATE_STAGE_LABELS):
        return "Generating MOM & Meeting Analysis..."
    return "Generating " + ", ".join(pending) + "..."


def _chunk_processing_label(chunk_durations: Optional[dict]) -> str:
    if not chunk_durations:
        return "Meeting in progress — waiting for the first audio chunk..."
    total_seconds = sum(v for v in chunk_durations.values() if isinstance(v, (int, float)))
    return f"Meeting in progress — {total_seconds / 60:.1f} min of audio captured so far"


def _stage_label(state: str, timing: dict, chunk_durations: Optional[dict]) -> str:
    if state == "generating_docs":
        return _generating_docs_label(timing)
    if state == "chunk_processing":
        return _chunk_processing_label(chunk_durations)
    return _STATE_LABELS.get(state, state)


def _tail_percent(timing: dict) -> int:
    done = sum(1 for key in TAIL_STAGE_KEYS if key in timing)
    return round(100 * done / len(TAIL_STAGE_KEYS))


def _tail_eta_seconds(timing: dict, history: dict) -> Optional[float]:
    if not history:
        return None
    remaining = [key for key in TAIL_STAGE_KEYS if key not in timing]
    if not remaining:
        return 0.0
    known = [history[key]["mean"] for key in remaining if key in history and "mean" in history[key]]
    if len(known) != len(remaining):
        return None
    return round(sum(known), 1)


def _recorded_signal(state: str, chunk_durations: Optional[dict]) -> Optional[bool]:
    """None = not applicable / not knowable yet (legacy whole-file uploads
    always start with a file, so this only means anything for the chunked
    pipeline; and even there, no chunks yet during a still-live meeting
    isn't evidence of failure). False is a real, actionable signal: the
    meeting has already ended (finalize ran) with zero captured audio.
    """
    if chunk_durations is None:
        return None
    total = sum(v for v in chunk_durations.values() if isinstance(v, (int, float)))
    if total > 0:
        return True
    if state in ("received", "chunk_processing"):
        return None
    return False


def describe_progress(
    state: str,
    timing: Optional[dict] = None,
    chunk_durations: Optional[dict] = None,
    history: Optional[dict] = None,
    folder_path: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict:
    timing = timing or {}
    history = history or {}

    label = _stage_label(state, timing, chunk_durations)
    if state == "failed":
        label = f"Failed: {error_message}" if error_message else "Failed"

    percent: Optional[int] = None
    eta_seconds: Optional[float] = None
    if state in _TAIL_STATES:
        percent = _tail_percent(timing)
        eta_seconds = _tail_eta_seconds(timing, history)
    elif state == "saved":
        percent = 100

    return {
        "status": _SIMPLE_STATUS.get(state, "Processing"),
        "label": label,
        "percent": percent,
        "eta_seconds": eta_seconds,
        "recorded": _recorded_signal(state, chunk_durations),
        "completed": state == "saved",
        "folder_path": folder_path if state == "saved" else None,
    }
