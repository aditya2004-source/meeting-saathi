"""Glue between the two orchestrators and the speaker-reconciliation pass:
decides whether the recovery Gemini call is warranted for a finished set of
segments, runs it, and applies the result. Kept out of
app/pipeline/speaker_reconcile.py so that module stays pure (no Gemini
call, trivially unit-testable) and out of both orchestrators so the
trigger logic lives in exactly one place.
"""
from typing import Optional

from app.config import settings
from app.docgen import engine as docgen_engine
from app.pipeline.diarize import SpeakerSegment
from app.pipeline.merge import render_segments_text
from app.pipeline.speaker_reconcile import (
    apply_reconciliation,
    distinct_unidentified_labels,
    merge_real_names,
    placeholder_dominance,
)
from app.pipeline.timing import TimingRecorder, timed


def maybe_reconcile_speakers(
    meeting_title: str,
    segments: list[SpeakerSegment],
    unidentified_excerpts: dict[str, str],
    attendees: list[str],
    roster_hint: str = "",
    recorder: Optional[TimingRecorder] = None,
) -> tuple[list[SpeakerSegment], dict[str, str], list[str]]:
    """Returns (segments, unidentified_excerpts, attendees), unchanged when
    reconciliation isn't warranted or the Gemini call fails. Never raises --
    a failed recovery pass must not fail the pipeline; the meeting is still
    perfectly usable with the "Unidentified speaker N" labels.
    """
    if not settings.speaker_reconcile_enabled or not segments:
        return segments, unidentified_excerpts, attendees

    labels = distinct_unidentified_labels(segments)
    if len(labels) < settings.speaker_reconcile_min_labels:
        return segments, unidentified_excerpts, attendees
    if placeholder_dominance(segments) < settings.speaker_reconcile_min_dominance:
        return segments, unidentified_excerpts, attendees

    transcript_text = render_segments_text(meeting_title, segments)
    try:
        reconciliation = _timed_reconcile(recorder, transcript_text, labels, roster_hint)
        new_segments, new_excerpts, real_names = apply_reconciliation(
            segments, reconciliation, unidentified_excerpts
        )
    except Exception:  # noqa: BLE001 - recovery pass, must never fail the pipeline
        import traceback

        traceback.print_exc()
        return segments, unidentified_excerpts, attendees

    return new_segments, new_excerpts, merge_real_names(attendees, real_names)


def _timed_reconcile(
    recorder: Optional[TimingRecorder], transcript_text: str, labels: list[str], roster_hint: str
) -> dict:
    if recorder is None:
        return docgen_engine.reconcile_speaker_identities(transcript_text, labels, roster_hint)
    with timed(recorder, "reconcile_speakers"):
        return docgen_engine.reconcile_speaker_identities(transcript_text, labels, roster_hint)
