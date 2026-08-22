#!/usr/bin/env python3
"""Re-run only document generation + PDF rendering from an existing
transcript.json, without rejoining a meeting or re-transcribing audio.
Useful for iterating on the MOM/Meeting-Analysis prompts.

Usage:
    python scripts/regenerate_docs.py /path/to/meeting/folder/transcript.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docgen import engine as docgen_engine  # noqa: E402
from app.docgen.render_pdf import markdown_to_pdf  # noqa: E402
from app.pipeline.merge import render_plain_text  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_text = render_plain_text(transcript)

    docs = docgen_engine.generate_documents(
        transcript["meeting_title"],
        transcript_text,
        attendees=transcript.get("attendees"),
        meeting_date=transcript.get("meeting_date_display", ""),
    )

    out_dir = transcript_path.parent
    for key, filename in [
        ("mom", "MOM"),
        ("meeting_analysis", "Meeting_Analysis"),
    ]:
        markdown_body = docs[key]["markdown_body"]
        (out_dir / f"{filename}.md").write_text(markdown_body, encoding="utf-8")
        markdown_to_pdf(markdown_body, out_dir / f"{filename}.pdf")
        print(f"Wrote {filename}.md and {filename}.pdf")


if __name__ == "__main__":
    main()
