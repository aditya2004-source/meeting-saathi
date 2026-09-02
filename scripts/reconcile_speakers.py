#!/usr/bin/env python3
"""Re-run speaker reconciliation on an existing meeting whose diarization
failed -- i.e. transcript.json is full of "Unidentified speaker 1..N"
because the Chrome extension captured no active-speaker signal and every
chunk fell through to per-chunk diarization.

One Gemini pass (see app/docgen/reconcile_prompt.py) collapses those
fragments into the true, small participant set, assigning a real name only
where the transcript unambiguously reveals it. transcript.json /
transcript.txt are rewritten in place (the original transcript.json is
backed up to transcript.pre-reconcile.json first), and facts.json is
deleted so the next regenerate_docs.py run re-extracts against the clean
speaker labels.

Usage:
    python scripts/reconcile_speakers.py /path/to/meeting/folder/transcript.json \\
        [--roster "Mustafa - Imdadi BuildMart (client); Dhaval - Sangam; Aditya - Sarathi"] \\
        [--regenerate]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docgen import engine as docgen_engine  # noqa: E402
from app.pipeline.diarize import SpeakerSegment  # noqa: E402
from app.pipeline.merge import render_segments_text  # noqa: E402
from app.pipeline.speaker_reconcile import (  # noqa: E402
    apply_reconciliation,
    distinct_unidentified_labels,
    is_unidentified_label,
    merge_real_names,
    placeholder_dominance,
)


def _segments_from_transcript(transcript: dict) -> list[SpeakerSegment]:
    return [
        SpeakerSegment(start=s["start"], end=s["end"], speaker=s["speaker"], text=s["text"])
        for s in transcript["segments"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("transcript_path", type=Path)
    parser.add_argument("--roster", default="", help="Optional attendee hint for the model.")
    parser.add_argument("--regenerate", action="store_true", help="Run regenerate_docs.py afterwards.")
    args = parser.parse_args()

    transcript_path = args.transcript_path
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = _segments_from_transcript(transcript)

    labels = distinct_unidentified_labels(segments)
    dominance = placeholder_dominance(segments)
    print(f"{len(labels)} unidentified-speaker labels, {dominance:.0%} of transcript text.")
    if not labels:
        print("Nothing to reconcile -- no 'Unidentified speaker N' labels present.")
        return

    print("Calling Gemini for speaker reconciliation...")
    reconciliation = docgen_engine.reconcile_speaker_identities(
        render_segments_text(transcript["meeting_title"], segments), labels, args.roster
    )
    new_segments, new_excerpts, real_names = apply_reconciliation(
        segments, reconciliation, transcript.get("unidentified_speaker_excerpts") or {}
    )

    canonical = list(dict.fromkeys(s.speaker for s in new_segments))
    print(f"\nCollapsed {len(labels)} labels -> {len(canonical)} participants:")
    for name in canonical:
        tag = "  (name identified)" if name in real_names else ""
        print(f"  - {name}{tag}")

    kept_original = [a for a in (transcript.get("attendees") or []) if not is_unidentified_label(a)]
    attendees = merge_real_names(kept_original, real_names)
    # No real names found at all -> the anonymous "Participant N" set is the
    # only honest attendee list we can offer.
    if not attendees:
        attendees = [n for n in canonical if not is_unidentified_label(n)] or canonical

    transcript["attendees"] = attendees
    transcript["unidentified_speaker_excerpts"] = new_excerpts
    transcript["segments"] = [
        {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text} for s in new_segments
    ]

    backup = transcript_path.with_name("transcript.pre-reconcile.json")
    if not backup.exists():
        backup.write_text(json.dumps(json.loads(transcript_path.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
        print(f"\nBacked up original -> {backup.name}")

    transcript_path.write_text(json.dumps(transcript, indent=2), encoding="utf-8")
    from app.pipeline.merge import render_plain_text

    transcript_path.with_name("transcript.txt").write_text(render_plain_text(transcript), encoding="utf-8")
    print(f"Rewrote {transcript_path.name} and transcript.txt")

    facts_path = transcript_path.with_name("facts.json")
    if facts_path.exists():
        facts_path.unlink()
        print("Deleted facts.json (will be re-extracted on next regenerate).")

    if args.regenerate:
        import subprocess

        print("\nRegenerating documents...")
        subprocess.run(
            [sys.executable, str(Path(__file__).with_name("regenerate_docs.py")), str(transcript_path)],
            check=True,
        )


if __name__ == "__main__":
    main()
