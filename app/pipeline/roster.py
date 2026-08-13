"""Parses the extension's attendee_roster.json sidecar (a best-effort scrape
of Google Meet's People panel -- see extension/DESIGN.md) and combines it
with confidently-named speakers to produce one deterministic attendee list.
The roster, not the transcript, is the authoritative source of "who was
there" -- it's the only signal that can see a participant who joined but
never spoke. Pure functions, no I/O -- callers are responsible for reading
the sidecar file.
"""
import json

from app.pipeline.diarize import SpeakerSegment
from app.pipeline.speaker_names import is_placeholder_speaker

# Mirrors speaker_names.py's own _IGNORED_NAMES -- the local user's own tile
# is commonly labeled "You" in Meet's UI, and the same applies to People
# panel rows.
_IGNORED_NAMES = {"you"}


def parse_attendee_roster(raw_json: str) -> list[str]:
    """Best-effort parse of the extension's attendee_roster.json. Malformed
    or unexpected input (a buggy extension build, a truncated upload, or the
    People panel never being readable during this meeting) yields an empty
    list rather than raising -- this is optional enrichment data, never
    something that should fail the pipeline. Accepts either a flat list of
    name strings or a list of {"name": ...} objects (mirrors the shape
    extension/background.js's roster snapshot may reasonably take).
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name")
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        cleaned = name.strip()
        key = cleaned.lower()
        if key in _IGNORED_NAMES or key in seen:
            continue
        seen.add(key)
        names.append(cleaned)
    return names


def compute_attendees(roster: list[str], segments: list[SpeakerSegment]) -> list[str]:
    """The People-panel roster first (in the order Meet reported it), then
    any additionally-detected real speaker name not already on the roster
    (e.g. someone who dialed in outside Meet's own participant list). Never
    includes a still-unresolved placeholder ("Speaker N", "Unknown") --
    call this BEFORE fill_unresolved_with_excerpts() turns those into
    "Unidentified speaker N" labels, which also must never appear here.
    """
    seen = {name.lower() for name in roster}
    extra: list[str] = []
    for seg in segments:
        name = seg.speaker
        if is_placeholder_speaker(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        extra.append(name)
    return [*roster, *extra]
