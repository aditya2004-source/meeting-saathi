"""Covers app.pipeline.merge -- format_meeting_date() (real meeting
timestamp -> display string, threaded into the Gemini grounding data so a
document's Date field is a real fact instead of Gemini guessing/extracting
it from transcript content) and build_transcript()'s new attendees/
meeting_date_display/unidentified_speaker_excerpts fields.
"""
from app.pipeline.diarize import SpeakerSegment
from app.pipeline.merge import build_transcript, format_meeting_date


def test_format_meeting_date_renders_ist_by_default():
    # 09:00 UTC on a July date -> 14:30 IST (UTC+5:30).
    result = format_meeting_date("2026-07-28T09:00:00+00:00")

    assert result == "28 July 2026, 2:30 PM IST"


def test_format_meeting_date_malformed_input_returned_unchanged():
    assert format_meeting_date("not a date") == "not a date"
    assert format_meeting_date("") == ""


def test_build_transcript_uses_provided_attendees_not_derived_from_segments():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi")]

    transcript = build_transcript(
        meeting_title="Sync",
        started_at="2026-07-28T09:00:00+00:00",
        ended_at="2026-07-28T09:30:00+00:00",
        segments=segments,
        attendees=["Priya Shah", "Silent Person"],
    )

    assert transcript["attendees"] == ["Priya Shah", "Silent Person"]
    assert transcript["meeting_date_display"] == "28 July 2026, 2:30 PM IST"


def test_build_transcript_falls_back_to_derived_attendees_when_not_given():
    segments = [
        SpeakerSegment(start=0.0, end=1.0, speaker="Priya Shah", text="hi"),
        SpeakerSegment(start=2.0, end=3.0, speaker="Priya Shah", text="again"),
    ]

    transcript = build_transcript(
        meeting_title="Sync",
        started_at="2026-07-28T09:00:00+00:00",
        ended_at="2026-07-28T09:30:00+00:00",
        segments=segments,
    )

    assert transcript["attendees"] == ["Priya Shah"]


def test_build_transcript_stores_unidentified_excerpts_separately():
    segments = [SpeakerSegment(start=0.0, end=1.0, speaker="Unidentified speaker 1", text="hi")]

    transcript = build_transcript(
        meeting_title="Sync",
        started_at="2026-07-28T09:00:00+00:00",
        ended_at="2026-07-28T09:30:00+00:00",
        segments=segments,
        attendees=[],
        unidentified_speaker_excerpts={"Unidentified speaker 1": "hi"},
    )

    assert transcript["unidentified_speaker_excerpts"] == {"Unidentified speaker 1": "hi"}
