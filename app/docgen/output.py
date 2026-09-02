"""Shared write-out for a generated document: source file + PDF, plus a
deterministic quality review (see validate.py) dropped next to it as a
`<Base>.review.md` sidecar when -- and only when -- there is something for a
human to look at. Used by both the on-demand route (app/main.py) and
scripts/regenerate_docs.py so the two paths never drift.
"""
from pathlib import Path

from app.docgen import registry
from app.docgen.render_pdf import markdown_to_pdf
from app.docgen.validate import review_markdown_document
from app.storage import write_meeting_file


def write_generated_document(folder: Path, produced_key: str, content: dict, facts: dict) -> list[str]:
    """Writes one produced document into `folder` and returns any review
    findings (also persisted as a sidecar). `content` is a generator's
    per-key value: `{title, markdown_body}`. The Business Process Flow's
    Mermaid diagrams are rendered from the ```mermaid blocks in its body by
    markdown_to_pdf().
    """
    doc = registry.DOCUMENTS[produced_key]
    source_name, pdf_name = registry.filenames_for(produced_key)
    review_name = f"{doc.filename_base}.review.md"

    body = content["markdown_body"]
    write_meeting_file(folder, source_name, body)
    markdown_to_pdf(body, folder / pdf_name)
    findings = review_markdown_document(produced_key, body, facts or {})

    review_path = folder / review_name
    if findings:
        write_meeting_file(
            folder,
            review_name,
            f"# Automated review — {doc.label}\n\n"
            "These are deterministic checks, not judgements of substance. "
            "Review each before sending the document.\n\n"
            + "\n".join(f"- {f}" for f in findings)
            + "\n",
        )
    elif review_path.exists():
        review_path.unlink()

    return findings
