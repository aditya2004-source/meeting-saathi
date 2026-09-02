#!/usr/bin/env python3
"""Re-run document generation + PDF/diagram rendering from an existing
transcript.json (and, if present, facts.json), without rejoining a meeting or
re-transcribing audio. Useful for iterating on prompts, and the documented
recovery path when the pipeline died after transcription but before extraction.

Since document generation is on-demand (see app/docgen/registry.py), this script
is a thin CLI wrapper around the same registry the dashboard's "Generate" buttons
use -- one place to add a new document type, not two.

Usage:
    python scripts/regenerate_docs.py /path/to/meeting/folder/transcript.json [doc_key ...]

With no doc_key given, regenerates every document in the registry. Pass one or
more doc_keys (e.g. "mom brd frd") to regenerate only those.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docgen import engine as docgen_engine  # noqa: E402
from app.docgen import registry  # noqa: E402
from app.docgen.output import write_generated_document  # noqa: E402
from app.pipeline.merge import render_plain_text  # noqa: E402
from app.storage import write_meeting_file  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    requested_doc_keys = sys.argv[2:] or list(registry.DOCUMENTS)
    unknown = [key for key in requested_doc_keys if key not in registry.DOCUMENTS]
    if unknown:
        print(f"Unknown doc_key(s): {', '.join(unknown)}. Known: {', '.join(registry.DOCUMENTS)}")
        sys.exit(1)

    out_dir = transcript_path.parent
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_text = render_plain_text(transcript)

    facts_path = out_dir / "facts.json"
    if facts_path.is_file():
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    else:
        print("No facts.json found next to transcript.json -- extracting facts first.")
        facts = docgen_engine.extract_meeting_facts(transcript_text) if transcript.get("segments") else docgen_engine.empty_meeting_facts()
        write_meeting_file(out_dir, "facts.json", json.dumps(facts, indent=2))

    # Dedupe by group -- requesting both "user_stories" and "acceptance_criteria"
    # (or every doc_key, the default) must still only run their shared Gemini
    # call once.
    group_keys = list(dict.fromkeys(registry.DOCUMENTS[key].group for key in requested_doc_keys))

    for group_key in group_keys:
        group = registry.GROUPS[group_key]
        result = group.generator(
            transcript["meeting_title"],
            transcript.get("meeting_date_display", ""),
            transcript.get("attendees") or [],
            facts,
            transcript_text,
        )
        if result is None:
            print(f"{group_key}: nothing to generate (no supporting material)")
            continue
        for produced_key, content in result.items():
            source_name, pdf_name = registry.filenames_for(produced_key)
            findings = write_generated_document(out_dir, produced_key, content, facts)
            print(f"Wrote {source_name} and {pdf_name}")
            for finding in findings:
                print(f"    ⚠ {finding}")


if __name__ == "__main__":
    main()
