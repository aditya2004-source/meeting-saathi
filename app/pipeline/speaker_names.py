"""Resolves diarization's placeholder "Speaker N" labels to real names,
using a best-effort timeline of "who's currently speaking" captured by the
Chrome extension from Google Meet's UI while recording (see
extension/DESIGN.md). Pure functions, no I/O -- orchestrator.py is
responsible for reading/parsing the sidecar file and calling this.
"""
import json
import re
from dataclasses import dataclass, replace

from app.config import settings
from app.pipeline.diarize import SpeakerSegment
from app.pipeline.transcribe import TranscribedSegment

# The local user's own tile is commonly labeled "You" in Meet's UI -- never
# a real name, must never win a group.
_IGNORED_NAMES = {"you"}

# Matches diarize.py's own placeholder shapes: plain "Speaker 1" (legacy
# whole-file diarize(), and chunked diarize_chunk() before per-chunk
# tagging) and the chunk-tagged "Speaker 1 (chunk@123.4)" form
# diarize_chunk() produces so two different chunks' independently-numbered
# pyannote clusters never collide under the same literal string. Never
# matches a real name, so it's safe to use as "is this still unresolved".
_PLACEHOLDER_RE = re.compile(r"^Speaker \d+(?: \(chunk@[0-9.]+\))?$")


def is_placeholder_speaker(label: str) -> bool:
    """True for diarize.py's own generic labels ("Speaker N", "Unknown", or
    the chunk-tagged variant of "Speaker N") -- i.e. not yet a real name and
    not yet an excerpt fallback label either.
    """
    return label == "Unknown" or bool(_PLACEHOLDER_RE.match(label))


@dataclass
class SpeakerEvent:
    name: str
    t_seconds: float


def parse_speaker_events(raw_json: str) -> list[SpeakerEvent]:
    """Best-effort parse of the extension's speaker_events.json. Malformed
    or unexpected input (a buggy extension build, a truncated upload)
    yields an empty list rather than raising -- this is optional
    enrichment data, never something that should fail the pipeline.
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    events = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name.strip().lower() in _IGNORED_NAMES:
            continue
        try:
            t_seconds = float(item.get("t_seconds"))
        except (TypeError, ValueError):
            continue
        events.append(SpeakerEvent(name=name.strip(), t_seconds=t_seconds))
    return events


def resolve_speaker_names(
    segments: list[SpeakerSegment],
    events: list[SpeakerEvent],
    tolerance_seconds: float = 1.0,
    min_confidence: float = 0.5,
) -> list[SpeakerSegment]:
    """Groups segments by their current placeholder label ("Speaker 1",
    "Speaker 2", ...) and majority-votes a real name for each group from
    events whose timestamp falls within (tolerance_seconds of) that
    group's segments. A group keeps its placeholder if there's no
    confident match -- this is the permanent, intentional fallback, not
    an error case.

    "Unknown"-labeled segments (diarize.py's own no-overlap fallback) are
    never touched -- they aren't reliably tied to one diarization cluster,
    so name-resolving them would compound uncertainty rather than reduce it.

    Only groups segments whose label is still placeholder-shaped ("Speaker
    N", including the chunk-tagged form -- see is_placeholder_speaker()).
    The chunked/streaming pipeline feeds this *mixed* input: most segments
    already carry a real DOM-resolved name from the fast path, with only
    the rare pyannote-fallback chunk's segments still placeholder-shaped --
    a real name must pass through untouched, never get folded into a
    "group" and re-voted on.
    """
    if not events:
        return segments

    groups: dict[str, list[SpeakerSegment]] = {}
    for seg in segments:
        if not is_placeholder_speaker(seg.speaker) or seg.speaker == "Unknown":
            continue
        groups.setdefault(seg.speaker, []).append(seg)

    # placeholder -> (winning_name, votes_for_winner, total_matched_events)
    candidates: dict[str, tuple[str, int, int]] = {}
    for placeholder, group_segments in groups.items():
        counts: dict[str, int] = {}
        total = 0
        for seg in group_segments:
            window_start = seg.start - tolerance_seconds
            window_end = seg.end + tolerance_seconds
            for event in events:
                if window_start <= event.t_seconds <= window_end:
                    counts[event.name] = counts.get(event.name, 0) + 1
                    total += 1
        if total == 0:
            continue
        winning_name, votes = max(counts.items(), key=lambda kv: kv[1])
        if votes / total >= min_confidence:
            candidates[placeholder] = (winning_name, votes, total)

    # Collision rule: if multiple placeholder groups would resolve to the
    # same real name, only the more strongly-voted group actually gets
    # renamed -- the rest keep their placeholder. Letting two diarization
    # clusters both claim one name would misattribute someone's words in
    # the final documents, which is worse than an honest "Speaker N".
    name_to_placeholders: dict[str, list[str]] = {}
    for placeholder, (name, _votes, _total) in candidates.items():
        name_to_placeholders.setdefault(name, []).append(placeholder)

    resolved: dict[str, str] = {}
    for name, placeholders in name_to_placeholders.items():
        winner = max(placeholders, key=lambda p: (candidates[p][1], candidates[p][2]))
        resolved[winner] = name

    if not resolved:
        return segments

    return [
        replace(seg, speaker=resolved[seg.speaker]) if seg.speaker in resolved else seg
        for seg in segments
    ]


def dom_coverage_ratio(events: list[SpeakerEvent], window_start: float, window_end: float) -> float:
    """Fraction of [window_start, window_end) "covered" by a known speaker
    attribution -- i.e. some event at or before a given instant, so we know
    who was talking at that instant without needing pyannote. Used by
    diarize_chunk() to decide whether the DOM signal is trustworthy enough
    for this chunk, or whether to fall back to running pyannote on it.

    An event before window_start still counts (its attribution carries
    forward into the window until the next event) -- most chunks won't have
    an event landing exactly on their own start, so this matters for a
    realistic coverage estimate across chunk boundaries.
    """
    duration = window_end - window_start
    if duration <= 0 or not events:
        return 0.0

    times = sorted(e.t_seconds for e in events)
    preceding = [t for t in times if t <= window_start]
    if preceding:
        covered_start = window_start
    else:
        within = [t for t in times if window_start < t < window_end]
        if not within:
            return 0.0
        covered_start = within[0]

    covered = max(0.0, window_end - covered_start)
    return min(1.0, covered / duration)


def speaker_from_dom_events(
    transcribed: list[TranscribedSegment],
    events: list[SpeakerEvent],
    offset: float,
    max_staleness_seconds: float | None = None,
) -> list[SpeakerSegment]:
    """Assigns each transcribed segment the name of the nearest *preceding*
    DOM speaker-change event (events are treated as interval boundaries: an
    event at t "owns" attribution until the next event). No preceding event
    -> "Unknown", the same convention diarize.py's own _align() uses for its
    no-overlap case.

    `transcribed` segments use chunk-local timestamps (t=0 at chunk start);
    `offset` (the chunk's global start time) shifts them into the same
    global coordinate space `events`' t_seconds already use, so the
    returned SpeakerSegments carry global timestamps -- consistent with
    diarize_chunk()'s pyannote-fallback branch, which shifts the same way.

    A preceding event older than `max_staleness_seconds` (default
    settings.speaker_event_max_staleness_seconds) is treated as if there
    were no preceding event at all -- guards against a stalled DOM observer
    (see config.py) confidently mislabeling everyone as whoever spoke last
    before it stalled, for the rest of the meeting.
    """
    staleness_cap = (
        max_staleness_seconds
        if max_staleness_seconds is not None
        else settings.speaker_event_max_staleness_seconds
    )
    sorted_events = sorted(events, key=lambda e: e.t_seconds)
    segments = []
    for seg in transcribed:
        if not seg.text:
            continue
        global_start = seg.start + offset
        global_end = seg.end + offset
        speaker = "Unknown"
        last_event_time = None
        for event in sorted_events:
            if event.t_seconds <= global_start:
                speaker = event.name
                last_event_time = event.t_seconds
            else:
                break
        if last_event_time is not None and (global_start - last_event_time) > staleness_cap:
            speaker = "Unknown"
        segments.append(SpeakerSegment(start=global_start, end=global_end, speaker=speaker, text=seg.text))
    return segments


_EXCERPT_MAX_CHARS = 160


def _make_excerpt(texts: list[str], max_lines: int = 3) -> str:
    lines = [t.strip() for t in texts[:max_lines] if t.strip()]
    excerpt = " / ".join(lines) if lines else "(no transcribed speech)"
    if len(excerpt) > _EXCERPT_MAX_CHARS:
        excerpt = excerpt[: _EXCERPT_MAX_CHARS - 3].rstrip() + "..."
    return excerpt


def fill_unresolved_with_excerpts(
    segments: list[SpeakerSegment],
) -> tuple[list[SpeakerSegment], dict[str, str]]:
    """Terminal guarantee, called after resolve_speaker_names(): no bare
    "Speaker N" or "Unknown" placeholder is ever allowed to reach
    build_transcript()/Gemini. Every remaining placeholder-shaped segment is
    replaced with a clean, sequential "Unidentified speaker N" label --
    presentable in a client-facing document, and still distinguishes two
    different unidentified people from each other.

    Returns (segments, excerpts): excerpts maps each "Unidentified speaker
    N" label to a short quote of what that speaker actually said. This is
    NOT baked into the label itself (unlike the old inline-quote format) --
    callers should persist it only in transcript.json for a human to
    manually identify the speaker later, never in transcript_text, so it
    never reaches Gemini or a client-facing PDF.

    A "Speaker N" group is one real diarization cluster (one person, within
    the scope diarize.py minted that label in -- the whole meeting for the
    legacy path, one chunk for the chunked path's fallback branch -- see
    diarize_chunk()'s chunk-tagging), so every segment in that group shares
    ONE label (and one excerpt). "Unknown" segments are diarize.py's own
    no-overlap case -- not reliably the same person from one to the next --
    so each "Unknown" segment gets its OWN independent label/excerpt rather
    than being merged with other "Unknown" segments into one (possibly
    wrong) identity.
    """
    group_texts: dict[str, list[str]] = {}
    for seg in segments:
        if is_placeholder_speaker(seg.speaker) and seg.speaker != "Unknown":
            group_texts.setdefault(seg.speaker, []).append(seg.text)

    counter = 0
    group_labels: dict[str, str] = {}
    excerpts: dict[str, str] = {}
    for placeholder, texts in group_texts.items():
        counter += 1
        label = f"Unidentified speaker {counter}"
        group_labels[placeholder] = label
        excerpts[label] = _make_excerpt(texts)

    result = []
    for seg in segments:
        if seg.speaker == "Unknown":
            counter += 1
            label = f"Unidentified speaker {counter}"
            excerpts[label] = _make_excerpt([seg.text])
            result.append(replace(seg, speaker=label))
        elif seg.speaker in group_labels:
            result.append(replace(seg, speaker=group_labels[seg.speaker]))
        else:
            result.append(seg)
    return result, excerpts
