"""Single source of truth for "what documents exist, how are they generated, and
what files do they write" -- imported by app/main.py's on-demand generation routes,
scripts/regenerate_docs.py, and the dashboard. Replaces the old hardcoded
_DOC_FILENAMES dict that used to live separately in orchestrator.py/
orchestrator_streaming.py/scripts/regenerate_docs.py/app/main.py -- with the
generation model changed to on-demand (see app/docgen/DESIGN.md and the project plan),
one place to add a new document type instead of six.

Each entry in GROUPS is one Gemini call (or, for "local" groups, one deterministic
render with no Gemini call at all) that produces one or more documents (`produces`).
Each entry in DOCUMENTS is one downloadable deliverable -- its own filenames, and
which group produces it.
"""
from typing import Callable, NamedTuple, Optional

from app.docgen import engine


class Group(NamedTuple):
    generator: Callable[..., Optional[dict]]
    produces: tuple[str, ...]
    local: bool  # True = no Gemini call, safe/instant, never fails on quota/network


class Document(NamedTuple):
    label: str
    group: str
    output: str  # "markdown" (writes .md + .pdf) or "diagram" (writes .mmd + .pdf)
    filename_base: str


GROUPS: dict[str, Group] = {
    "mom": Group(engine.generate_mom, ("mom",), local=False),
    "meeting_analysis": Group(engine.generate_meeting_analysis, ("meeting_analysis",), local=False),
    "brd": Group(engine.generate_brd, ("brd",), local=False),
    "frd": Group(engine.generate_frd, ("frd",), local=True),
    "stories_and_ac": Group(
        engine.generate_user_stories_and_acceptance_criteria,
        ("user_stories", "acceptance_criteria"),
        local=False,
    ),
    "business_process_flow": Group(
        engine.generate_business_process_flow, ("business_process_flow",), local=True
    ),
}

DOCUMENTS: dict[str, Document] = {
    "mom": Document("Minutes of Meeting", "mom", "markdown", "MOM"),
    "meeting_analysis": Document("Meeting Analysis", "meeting_analysis", "markdown", "Meeting_Analysis"),
    "brd": Document("Business Requirements Document (BRD)", "brd", "markdown", "BRD"),
    "frd": Document("Functional Requirements Document (FRD)", "frd", "markdown", "FRD"),
    "user_stories": Document("User Stories", "stories_and_ac", "markdown", "User_Stories"),
    "acceptance_criteria": Document("Acceptance Criteria", "stories_and_ac", "markdown", "Acceptance_Criteria"),
    "business_process_flow": Document(
        "Business Process Flow (AS-IS)", "business_process_flow", "diagram", "Business_Process_Flow"
    ),
}


def filenames_for(doc_key: str) -> tuple[str, str]:
    """(source_filename, pdf_filename) -- source is .md for a markdown document,
    .mmd (editable Mermaid source) for a diagram.
    """
    doc = DOCUMENTS[doc_key]
    source_ext = "md" if doc.output == "markdown" else "mmd"
    return f"{doc.filename_base}.{source_ext}", f"{doc.filename_base}.pdf"


def group_for_document(doc_key: str) -> Group:
    return GROUPS[DOCUMENTS[doc_key].group]


def documents_in_group(group_key: str) -> list[str]:
    return [key for key, doc in DOCUMENTS.items() if doc.group == group_key]
