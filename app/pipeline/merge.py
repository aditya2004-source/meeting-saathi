import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.pipeline.diarize import SpeakerSegment


def format_meeting_date(started_at_iso: str) -> str:
    """Renders a stored UTC ISO8601 timestamp (e.g. run["created_at"]) as a
    clean, unambiguous local date/time string for display in generated
    documents and the dashboard -- e.g. "13 August 2026, 3:45 PM IST". Uses
    settings.report_timezone explicitly rather than the server's ambient
    system timezone, so it's correct regardless of where this is deployed.
    Malformed input is returned unchanged rather than raising -- this is a
    display convenience, never something that should fail the pipeline.
    """
    try:
        dt = datetime.datetime.fromisoformat(started_at_iso)
    except (ValueError, TypeError):
        return started_at_iso
    local_dt = dt.astimezone(ZoneInfo(settings.report_timezone))
    return local_dt.strftime("%-d %B %Y, %-I:%M %p %Z")


def build_transcript(
    meeting_title: str,
    started_at: str,
    ended_at: str,
    segments: list[SpeakerSegment],
    attendees: list[str] | None = None,
    unidentified_speaker_excerpts: dict[str, str] | None = None,
) -> dict:
    """`attendees` should be the deterministic roster+spoken-names union
    from app.pipeline.roster.compute_attendees() -- callers that don't have
    a roster available yet (e.g. existing tests) can omit it, in which case
    it falls back to the old behaviour of deriving it purely from distinct
    speaker labels in `segments`. `unidentified_speaker_excerpts` (from
    speaker_names.fill_unresolved_with_excerpts()) is stored only for a
    human's later reference -- never rendered into transcript_text/Gemini.
    """
    if attendees is None:
        attendees = list(dict.fromkeys(s.speaker for s in segments))

    return {
        "meeting_title": meeting_title,
        "started_at": started_at,
        "ended_at": ended_at,
        "meeting_date_display": format_meeting_date(started_at),
        "attendees": attendees,
        "unidentified_speaker_excerpts": unidentified_speaker_excerpts or {},
        "diarization_source": "pyannote_local",
        "segments": [
            {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
            for s in segments
        ],
    }


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_plain_text(transcript: dict) -> str:
    lines = [transcript["meeting_title"], ""]
    for seg in transcript["segments"]:
        lines.append(f"[{_format_timestamp(seg['start'])}] {seg['speaker']}: {seg['text']}")
    return "\n".join(lines)
