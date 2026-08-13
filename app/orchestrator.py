import datetime
import json
import shutil
import traceback
from pathlib import Path

from app import db
from app.config import settings
from app.docgen import engine as docgen_engine
from app.docgen.render_pdf import render_documents_to_pdf
from app.pipeline.diarize import diarize
from app.pipeline.download import working_dir_for
from app.pipeline.merge import build_transcript, render_plain_text
from app.pipeline.roster import compute_attendees, parse_attendee_roster
from app.pipeline.speaker_names import (
    fill_unresolved_with_excerpts,
    parse_speaker_events,
    resolve_speaker_names,
)
from app.pipeline.timing import TimingRecorder, record_stage_durations, timed
from app.progress import TAIL_STAGE_KEYS
from app.storage import save_meeting_folder


def process_recording(run_id: str) -> None:
    """Synchronous, blocking, end-to-end pipeline for one uploaded recording.
    Intended to be run on a background thread (a meeting can run for an
    hour+; this must not block the web server's event loop).
    """
    try:
        _run(run_id)
    except Exception as exc:  # noqa: BLE001 - top-level pipeline guard, by design
        traceback.print_exc()
        db.mark_failed(run_id, exc)


def _run(run_id: str) -> None:
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(f"No meeting_run with id {run_id!r}")

    audio_path = Path(run["audio_path"])
    work_dir = working_dir_for(run_id)
    recorder = TimingRecorder(work_dir)

    db.update_run(run_id, state="transcribing")
    segments = diarize(audio_path, recorder=recorder)
    db.update_run(run_id, state="diarizing", diarization_source="pyannote_local")

    speaker_events_path = work_dir / "speaker_events.json"
    if speaker_events_path.exists():
        events = parse_speaker_events(speaker_events_path.read_text(encoding="utf-8"))
        segments = resolve_speaker_names(segments, events)

    roster: list[str] = []
    roster_path = work_dir / "attendee_roster.json"
    if roster_path.exists():
        roster = parse_attendee_roster(roster_path.read_text(encoding="utf-8"))
    # Must run BEFORE fill_unresolved_with_excerpts() below -- that call
    # turns any remaining "Speaker N"/"Unknown" placeholder into an
    # "Unidentified speaker N" label, which is not a real name and must
    # never end up in the attendee list.
    attendees = compute_attendees(roster, segments)

    # Terminal guarantee (unconditional, unlike the resolve_speaker_names()
    # call above): pyannote always mints "Speaker N"/"Unknown" regardless of
    # whether speaker_events.json exists at all, so whatever's still
    # placeholder-shaped at this point becomes a clean "Unidentified speaker
    # N" label instead of a bare "Speaker N" reaching the final documents.
    segments, unidentified_excerpts = fill_unresolved_with_excerpts(segments)

    now = datetime.datetime.now(datetime.timezone.utc)
    transcript = build_transcript(
        meeting_title=run["title"],
        started_at=run["created_at"],
        ended_at=now.isoformat(),
        segments=segments,
        attendees=attendees,
        unidentified_speaker_excerpts=unidentified_excerpts,
    )
    transcript_text = render_plain_text(transcript)

    db.update_run(run_id, state="generating_docs")
    docs = docgen_engine.generate_documents(
        run["title"],
        transcript_text,
        attendees=attendees,
        meeting_date=transcript["meeting_date_display"],
        recorder=recorder,
    )

    db.update_run(run_id, state="rendering")
    mom_pdf_path = work_dir / "MOM.pdf"
    requirement_gathering_pdf_path = work_dir / "Requirement_Gathering_Sheet.pdf"
    action_pdf_path = work_dir / "Action_Points.pdf"
    render_documents_to_pdf(
        docs, mom_pdf_path, requirement_gathering_pdf_path, action_pdf_path, recorder=recorder
    )

    db.update_run(run_id, state="saving")
    files: dict[str, bytes | str] = {
        "MOM.md": docs["mom"]["markdown_body"],
        "MOM.pdf": mom_pdf_path.read_bytes(),
        "Requirement_Gathering_Sheet.md": docs["requirement_gathering"]["markdown_body"],
        "Requirement_Gathering_Sheet.pdf": requirement_gathering_pdf_path.read_bytes(),
        "Action_Points.md": docs["action_points"]["markdown_body"],
        "Action_Points.pdf": action_pdf_path.read_bytes(),
        "transcript.json": json.dumps(transcript, indent=2),
        "transcript.txt": transcript_text,
    }
    if settings.keep_raw_recording and audio_path.exists():
        files[f"recording{audio_path.suffix}"] = audio_path.read_bytes()

    with timed(recorder, "save_meeting_folder"):
        folder = save_meeting_folder(settings.base_storage_dir, run["title"], now, files)
    db.update_run(run_id, state="saved", folder_path=str(folder))

    # See app/progress.py -- folds this run's tail-stage durations into the
    # small persisted history used to estimate a future run's ETA, before
    # this run's own timing.json is deleted below.
    tail_durations = {k: v for k, v in recorder.as_dict().items() if k in TAIL_STAGE_KEYS}
    record_stage_durations(settings.project_root / "data" / "stage_duration_history.json", tail_durations)

    shutil.rmtree(work_dir, ignore_errors=True)
    audio_path.unlink(missing_ok=True)
