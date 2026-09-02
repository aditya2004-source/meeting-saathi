"""Collapses the many "Unidentified speaker N" labels a failed diarization
produces back into the true, small participant set, using a single Gemini
pass over the whole transcript (see
app/docgen/reconcile_prompt.py). Pure functions, no I/O and no Gemini call
of their own -- the orchestrator (and scripts/reconcile_speakers.py) read
the transcript, call engine.reconcile_speaker_identities(), and pass its
result here.

This is a safety net for the case where the extension captured no
active-speaker signal at all -- a normal meeting with good DOM coverage
never reaches placeholder_dominance() high enough to trigger it, so the
Gemini quota / on-demand-generation preference is respected.
"""
import re
from dataclasses import replace

from app.pipeline.diarize import SpeakerSegment

# "Unidentified speaker 1", "Unidentified speaker 42" -- the clean,
# client-safe label fill_unresolved_with_excerpts() produces for every
# diarization cluster it could not tie to a real name. Deliberately NOT
# matching speaker_names.is_placeholder_speaker()'s raw "Speaker N" shapes:
# reconciliation always runs after fill_unresolved_with_excerpts(), so by
# this point every unresolved speaker is in this final form.
_UNIDENTIFIED_RE = re.compile(r"^Unidentified speaker \d+$")


def is_unidentified_label(label: str) -> bool:
    return bool(_UNIDENTIFIED_RE.match(label or ""))


def distinct_unidentified_labels(segments: list[SpeakerSegment]) -> list[str]:
    """Every "Unidentified speaker N" label present, in order of first
    appearance -- the exact set the Gemini pass must return a mapping for.
    """
    seen: dict[str, None] = {}
    for seg in segments:
        if is_unidentified_label(seg.speaker):
            seen.setdefault(seg.speaker, None)
    return list(seen)


def placeholder_dominance(segments: list[SpeakerSegment]) -> float:
    """Fraction of transcribed text (by character count) attributed to an
    "Unidentified speaker N" label. ~0 for a normal meeting where the DOM
    scrape named everyone; near 1.0 for the failure case this module
    exists to recover from. Character count, not segment count, so a few
    long unidentified monologues still register as dominant.
    """
    total = 0
    unidentified = 0
    for seg in segments:
        n = len(seg.text or "")
        total += n
        if is_unidentified_label(seg.speaker):
            unidentified += n
    if total == 0:
        return 0.0
    return unidentified / total


def build_label_map(reconciliation: dict, known_labels: list[str]) -> dict[str, str]:
    """Inverts the Gemini pass's `participants[].speaker_numbers` into a
    {"Unidentified speaker N" -> canonical_label} dict, keeping only
    entries whose source label is actually in the transcript and whose
    target is a non-empty string that differs from it. A label the model
    omitted (or mapped to itself / to junk) simply keeps its original name
    -- this never raises. If two participants both claim the same number,
    the first one wins.
    """
    known = set(known_labels)
    mapping: dict[str, str] = {}
    for participant in reconciliation.get("participants") or []:
        if not isinstance(participant, dict):
            continue
        dst = participant.get("canonical_label")
        if not isinstance(dst, str) or not dst.strip():
            continue
        dst = dst.strip()
        for number in participant.get("speaker_numbers") or []:
            try:
                src = f"Unidentified speaker {int(number)}"
            except (TypeError, ValueError):
                continue
            if src not in known or src in mapping or src == dst:
                continue
            mapping[src] = dst
    return mapping


def apply_reconciliation(
    segments: list[SpeakerSegment],
    reconciliation: dict,
    excerpts: dict[str, str] | None = None,
) -> tuple[list[SpeakerSegment], dict[str, str], list[str]]:
    """Rewrites `segments` with the reconciled canonical labels and re-keys
    `excerpts` to match. Returns (new_segments, new_excerpts,
    real_names): `real_names` is the subset of canonical labels the model
    flagged `is_real_name` that segments actually ended up using -- the
    orchestrator folds these into the attendee list.

    A canonical label that still looks like "Participant N" keeps an
    excerpt (a human may still identify them later); one resolved to a real
    name drops its excerpt.
    """
    known = distinct_unidentified_labels(segments)
    mapping = build_label_map(reconciliation, known)

    real_name_labels = {
        p.get("canonical_label", "").strip()
        for p in (reconciliation.get("participants") or [])
        if isinstance(p, dict) and p.get("is_real_name") and isinstance(p.get("canonical_label"), str)
    }
    real_name_labels.discard("")

    new_segments = [
        replace(seg, speaker=mapping[seg.speaker]) if seg.speaker in mapping else seg
        for seg in segments
    ]

    # Re-key excerpts: an old "Unidentified speaker N" that mapped to a
    # still-anonymous "Participant M" carries its excerpt over (first one
    # wins if several fragments merged); one that mapped to a real name is
    # dropped from the excerpt sidecar entirely.
    old_excerpts = excerpts or {}
    new_excerpts: dict[str, str] = {}
    for old_label, quote in old_excerpts.items():
        new_label = mapping.get(old_label, old_label)
        if new_label in real_name_labels:
            continue
        new_excerpts.setdefault(new_label, quote)

    used_labels = {seg.speaker for seg in new_segments}
    real_names = [label for label in real_name_labels if label in used_labels]

    return new_segments, new_excerpts, real_names


def merge_real_names(attendees: list[str], real_names: list[str]) -> list[str]:
    """Adds reconciliation-discovered real names to the deterministic
    attendee list, skipping any already present (case-insensitively, on the
    bare name before any "(Side)" parenthetical).
    """
    def key(name: str) -> str:
        return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip().casefold()

    seen = {key(a) for a in attendees}
    out = list(attendees)
    for name in real_names:
        k = key(name)
        if k and k not in seen:
            seen.add(k)
            out.append(name)
    return out
