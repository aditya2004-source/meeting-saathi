"""Chunked/streaming sibling to orchestrator.py: instead of one whole-file
upload processed after the meeting ends, the Chrome extension uploads short
self-contained audio chunks *during* the call (see extension/DESIGN.md),
each transcribed+diarized as it arrives (diarize_chunk(), DOM-primary with
a pyannote fallback -- see app/pipeline/diarize.py). By the time the
meeting ends and /finalize's last short chunk is processed, the transcript
is already assembled and only document generation + rendering + saving is
left to do.

Shares merge.py, docgen/engine.py, docgen/render_pdf.py, and storage.py
unchanged with the legacy whole-file path in orchestrator.py -- only the
transcript-assembly step differs (accumulated chunk segments here vs. one
diarize() call there).
"""
import datetime
import json
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from app import db
from app.chunked_state import drop as drop_state, get_or_create
from app.config import settings
from app.docgen import engine as docgen_engine
from app.docgen.render_pdf import render_documents_to_pdf
from app.pipeline.diarize import diarize_chunk, probe_duration_seconds
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

# States a run may already be in by the time a chunk arrives without needing
# to (re-)enter "chunk_processing" -- e.g. a late-arriving retry after
# finalize has already moved the run further along the pipeline.
_PAST_CHUNK_PROCESSING = {"generating_docs", "rendering", "saving", "saved", "failed"}

# Bounded, process-wide, not one raw threading.Thread per chunk (the
# previous design) -- confirmed in production on a real 4-core machine:
# unbounded concurrent chunks each spawning transcribe (4 threads) +
# pyannote-fallback (4 threads) piled up and drove pyannote's embedding
# stage to 575s for a single ~50s chunk (vs. an expected ~10-30s
# uncontended). Capping how many chunks process at once keeps total
# in-flight CPU threads roughly matched to real core count instead of
# wildly oversubscribing it (see also settings.whisper_cpu_threads /
# settings.pyannote_torch_threads, reduced alongside this cap).
_CHUNK_EXECUTOR = ThreadPoolExecutor(max_workers=2)


def accept_chunk(
    run_id: str,
    sequence: int,
    content: bytes,
    speaker_events_json: Optional[str],
    attendee_roster_json: Optional[str],
    final: bool,
) -> None:
    """Called from the /meetings/{run_id}/chunk and /finalize routes. Writes
    the chunk to disk and returns immediately -- the actual transcribe/
    diarize work is submitted to _CHUNK_EXECUTOR (bounded, see above) so a
    slow chunk never blocks the next chunk's upload from being accepted,
    while still capping how many chunks' CPU-heavy work run at once.
    """
    work_dir = working_dir_for(run_id)
    chunks_dir = work_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunks_dir / f"{sequence}.webm"
    chunk_path.write_bytes(content)

    if speaker_events_json:
        # Same best-effort parse-and-ignore convention as the legacy
        # upload path (app/main.py) -- malformed input just means no DOM
        # names get resolved for segments processed before this arrives.
        try:
            json.loads(speaker_events_json)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            (work_dir / "speaker_events.json").write_text(speaker_events_json, encoding="utf-8")

    if attendee_roster_json:
        # Same best-effort parse-and-ignore convention -- malformed input
        # just means no roster is available at finalize time. Each chunk
        # upload carries the extension's latest roster snapshot, so this
        # file just gets overwritten with the freshest one as the meeting
        # progresses.
        try:
            json.loads(attendee_roster_json)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            (work_dir / "attendee_roster.json").write_text(attendee_roster_json, encoding="utf-8")

    run = db.get_run(run_id)
    if run is not None and run["state"] not in _PAST_CHUNK_PROCESSING:
        db.update_run(run_id, state="chunk_processing")

    state = get_or_create(run_id)
    with state.lock:
        state.pending_sequences.add(sequence)

    _CHUNK_EXECUTOR.submit(_process_chunk_then_maybe_finalize, run_id, sequence, chunk_path, final)


def _process_chunk_then_maybe_finalize(run_id: str, sequence: int, chunk_path: Path, final: bool) -> None:
    try:
        _process_chunk(run_id, sequence, chunk_path)
    except Exception:  # noqa: BLE001 - per-chunk guard, by design
        # A single chunk's transcribe/diarize step raising (e.g. one
        # corrupt ~50s audio segment) used to fail the *entire* run here via
        # db.mark_failed(), discarding every other chunk's already-processed
        # segments even if the rest of the meeting was fine. Now this chunk
        # just contributes no segments and the run continues -- only a
        # failure in the finalize tail below (docgen/render/save, which
        # can't partially succeed) still fails the whole run.
        traceback.print_exc()
        state = get_or_create(run_id)
        with state.lock:
            state.pending_sequences.discard(sequence)
    if final:
        try:
            _wait_for_pending_then_finalize(run_id)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            db.mark_failed(run_id, exc)


def _process_chunk(run_id: str, sequence: int, chunk_path: Path) -> None:
    work_dir = working_dir_for(run_id)
    recorder = TimingRecorder(work_dir)
    state = get_or_create(run_id)

    duration = probe_duration_seconds(chunk_path)
    with state.lock:
        # Offset = sum of durations of all lower sequences known so far.
        # Correct when chunks arrive in order (the normal case: one
        # MediaRecorder cycling sequentially, uploads fired in that same
        # order); approximates using only what's known if a chunk arrives
        # out of order -- an accepted v1 gap, same category as the
        # in-memory-only state noted in chunked_state.py.
        offset = sum(d for s, d in state.chunk_durations.items() if s < sequence)
        state.chunk_durations[sequence] = duration
        durations_snapshot = dict(state.chunk_durations)
    _persist_chunk_durations(work_dir, durations_snapshot)

    events = []
    speaker_events_path = work_dir / "speaker_events.json"
    if speaker_events_path.exists():
        events = parse_speaker_events(speaker_events_path.read_text(encoding="utf-8"))

    segments = diarize_chunk(
        chunk_path,
        events,
        chunk_start_offset=offset,
        chunk_end_offset=offset + duration,
        recorder=recorder,
    )

    with state.lock:
        state.processed_segments.extend(segments)
        state.pending_sequences.discard(sequence)


def _persist_chunk_durations(work_dir: Path, durations: dict[int, float]) -> None:
    tmp_path = work_dir / "chunk_durations.json.tmp"
    tmp_path.write_text(json.dumps(durations, indent=2), encoding="utf-8")
    tmp_path.replace(work_dir / "chunk_durations.json")


def _wait_for_pending_then_finalize(run_id: str, poll_interval: float = 0.2) -> None:
    state = get_or_create(run_id)
    while True:
        with state.lock:
            if not state.pending_sequences:
                break
        time.sleep(poll_interval)
    finalize_run(run_id)


def finalize_run(run_id: str) -> None:
    """Assembles the transcript from already-processed chunk segments, then
    runs the same tail as the legacy path: docgen -> render -> save. Called
    once every chunk (including the final short one) has finished
    processing.
    """
    run = db.get_run(run_id)
    if run is None:
        raise KeyError(f"No meeting_run with id {run_id!r}")

    work_dir = working_dir_for(run_id)
    recorder = TimingRecorder(work_dir)
    state = get_or_create(run_id)

    with state.lock:
        segments = sorted(state.processed_segments, key=lambda s: s.start)

    # Most segments already carry a real name from the DOM-primary fast
    # path (speaker_from_dom_events(), applied per-chunk as it arrived);
    # only chunks that fell into diarize_chunk()'s pyannote-fallback branch
    # still carry a chunk-tagged "Speaker N" placeholder at this point. This
    # is that placeholder's one chance to resolve against the FULL
    # accumulated speaker_events.json (more DOM events may have landed by
    # now than were available in that one chunk's own narrow time window)
    # -- without this call, a fallback chunk's placeholder never gets
    # resolved at all, which was the main cause of "works sometimes, not
    # others" for speaker names. fill_unresolved_with_excerpts() is the
    # terminal guarantee after that: whatever resolve_speaker_names()
    # couldn't confidently name (including "Unknown" segments, and the case
    # of no speaker_events.json at all) becomes a transcript-excerpt label
    # instead of a bare, meaningless "Speaker N".
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
    if segments:
        docs = docgen_engine.generate_documents(
            run["title"],
            transcript_text,
            attendees=attendees,
            meeting_date=transcript["meeting_date_display"],
            recorder=recorder,
        )
    else:
        # No transcribed speech at all (e.g. a short test call where nobody
        # spoke) -- confirmed in production that calling Gemini with an
        # essentially empty transcript makes it return non-JSON text,
        # crashing json.loads(). A "nothing was said" document is both
        # truthful and avoids that failure mode entirely.
        docs = docgen_engine.empty_meeting_documents(run["title"], transcript["meeting_date_display"])

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
    # Note: unlike orchestrator.py's legacy path, keep_raw_recording isn't
    # supported here yet -- the "raw recording" would need to be a
    # concatenation of working/<run_id>/chunks/*.webm, which (unlike a
    # single MediaRecorder session's output) may not remux into one valid
    # playable file across chunk boundaries. Not required for this
    # redesign's scope; the durable backup remains transcript.json/.txt.

    with timed(recorder, "save_meeting_folder"):
        folder = save_meeting_folder(settings.base_storage_dir, run["title"], now, files)
    db.update_run(run_id, state="saved", folder_path=str(folder))

    # Folds this run's bounded post-meeting-tail stage durations into the
    # small persisted history (see app/progress.py) so a *future* run's
    # status page can show a real ETA instead of a guess -- this run's own
    # timing.json is about to be deleted along with the rest of work_dir.
    tail_durations = {k: v for k, v in recorder.as_dict().items() if k in TAIL_STAGE_KEYS}
    record_stage_durations(settings.project_root / "data" / "stage_duration_history.json", tail_durations)

    drop_state(run_id)
    shutil.rmtree(work_dir, ignore_errors=True)
