from app.pipeline.diarize import SpeakerSegment


def build_transcript(
    meeting_title: str,
    started_at: str,
    ended_at: str,
    segments: list[SpeakerSegment],
) -> dict:
    attendees = [{"name": speaker, "source": "diarization"} for speaker in dict.fromkeys(s.speaker for s in segments)]

    return {
        "meeting_title": meeting_title,
        "started_at": started_at,
        "ended_at": ended_at,
        "attendees": attendees,
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
