import datetime
import json
import shutil
import traceback
from pathlib import Path

from app import db
from app.config import settings
from app.docgen import engine as docgen_engine
from app.pipeline.diarize import diarize
from app.pipeline.download import working_dir_for
from app.pipeline.merge import build_transcript, render_plain_text
from app.pipeline.roster import compute_attendees, parse_attendee_roster
from app.reconcile import maybe_reconcile_speakers
from app.pipeline.speaker_names import (
    fill_unresolved_with_excerpts,
    parse_speaker_events,
    resolve_speaker_names,
)
from app.pipeline.timing import TimingRecorder, record_stage_durations, timed
from app.pipeline.translate import maybe_translate_segments_to_english
from app.progress import TAIL_STAGE_KEYS
from app.storage import create_meeting_folder, write_meeting_file


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

    # Ensure the transcript we store is English even when a chunk fell to the
    # AssemblyAI fallback (which has no task="translate"). One Gemini call over
    # just the non-English segments; no-op otherwise. See app/pipeline/translate.py.
    segments = maybe_translate_segments_to_english(segments, recorder)

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

    # Recovery pass for a meeting where the DOM active-speaker scrape caught
    # nothing and one real person is now scattered across dozens of
    # "Unidentified speaker N" labels -- one Gemini pass collapses them back
    # to the true participant set. No-op (zero Gemini calls) for a normal,
    # well-diarized meeting. See app/reconcile.py.
    segments, unidentified_excerpts, attendees = maybe_reconcile_speakers(
        run["title"], segments, unidentified_excerpts, attendees, "\n".join(roster), recorder
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    transcript = build_transcript(
        meeting_title=run["title"],
        started_at=run["created_at"],
        ended_at=now.isoformat(),
        segments=segments,
        attendees=attendees,
        unidentified_speaker_excerpts=unidentified_excerpts,
        client_name=run.get("client_name") or "",
    )
    transcript_text = render_plain_text(transcript)

    db.update_run(run_id, state="extracting_facts")

    # Created and recorded on the run immediately -- not at the very end --
    # so the folder is visible to the dashboard/download route from this
    # point on, well before extraction finishes. See app/storage.py's
    # create_meeting_folder()/write_meeting_file() for why this no longer
    # writes everything atomically in one shot.
    folder = create_meeting_folder(settings.base_storage_dir, run["title"], now)
    db.update_run(run_id, folder_path=str(folder))
    write_meeting_file(folder, "transcript.json", json.dumps(transcript, indent=2))
    write_meeting_file(folder, "transcript.txt", transcript_text)
    if settings.keep_raw_recording and audio_path.exists():
        write_meeting_file(folder, f"recording{audio_path.suffix}", audio_path.read_bytes())

    # The one Gemini call that still runs automatically -- everything past this
    # point (MOM, BRD, FRD, User Stories, Acceptance Criteria, Business Process
    # Flow) is on-demand from the dashboard (see app/docgen/registry.py and
    # app/main.py's /meetings/{run_id}/documents/{key}/generate route), reading
    # facts.json + transcript.json back from this folder. No document is written
    # here; this run reaches "saved" the moment the transcript + facts exist.
    if segments:
        with timed(recorder, "extract_facts"):
            facts = docgen_engine.extract_meeting_facts(transcript_text)
    else:
        # Calling Gemini with an essentially empty transcript is a confirmed
        # production crash (see docgen_engine.empty_meeting_facts()) -- and
        # there's nothing to extract from silence anyway.
        facts = docgen_engine.empty_meeting_facts()
    write_meeting_file(folder, "facts.json", json.dumps(facts, indent=2))

    db.update_run(run_id, state="saved")

    # See app/progress.py -- folds this run's tail-stage durations into the
    # small persisted history used to estimate a future run's ETA, before
    # this run's own timing.json is deleted below.
    tail_durations = {k: v for k, v in recorder.as_dict().items() if k in TAIL_STAGE_KEYS}
    record_stage_durations(settings.project_root / "data" / "stage_duration_history.json", tail_durations)

    shutil.rmtree(work_dir, ignore_errors=True)
    audio_path.unlink(missing_ok=True)
