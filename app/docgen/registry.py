"""Single source of truth for "what documents exist, how are they generated, and
what files do they write" -- imported by app/main.py's on-demand generation routes,
scripts/regenerate_docs.py, and the dashboard. Replaces the old hardcoded
_DOC_FILENAMES dict that used to live separately in orchestrator.py/
orchestrator_streaming.py/scripts/regenerate_docs.py/app/main.py -- with the
generation model changed to on-demand (see app/docgen/DESIGN.md and the project plan),
one place to add a new document type instead of six.

Each entry in GROUPS is one Gemini call that produces one or more documents
(`produces`). (`local` groups -- a deterministic render with no Gemini call -- are
still supported by the dispatch, there just aren't any right now.) Each entry in
DOCUMENTS is one downloadable deliverable -- its own filenames, and which group
produces it.
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
    filename_base: str  # every document writes <base>.md (source) + <base>.pdf


# The active deliverable set is deliberately just these three: Minutes of
# Meeting, Meeting Analysis, and the AS-IS Business Process Flow. SOW (and the
# older BRD / FRD / User Stories / Acceptance Criteria) generators still exist
# in engine.py but are intentionally left out of the catalogue below, so the
# dashboard neither offers nor generates them. Re-add an entry here to bring a
# document back.
GROUPS: dict[str, Group] = {
    "mom": Group(engine.generate_mom, ("mom",), local=False),
    "meeting_analysis": Group(engine.generate_meeting_analysis, ("meeting_analysis",), local=False),
    "business_process_flow": Group(
        engine.generate_business_process_flow, ("business_process_flow",), local=False
    ),
}

DOCUMENTS: dict[str, Document] = {
    "mom": Document("Minutes of Meeting", "mom", "MOM"),
    "meeting_analysis": Document("Meeting Analysis", "meeting_analysis", "Meeting_Analysis"),
    "business_process_flow": Document(
        "Business Process Flow", "business_process_flow", "Business_Process_Flow"
    ),
}


def filenames_for(doc_key: str) -> tuple[str, str]:
    """(source_filename, pdf_filename) -- every document writes a markdown source
    (.md) and its rendered PDF. The Business Process Flow's Mermaid diagrams live
    inside fenced ```mermaid blocks in its .md.
    """
    base = DOCUMENTS[doc_key].filename_base
    return f"{base}.md", f"{base}.pdf"


def group_for_document(doc_key: str) -> Group:
    return GROUPS[DOCUMENTS[doc_key].group]


def documents_in_group(group_key: str) -> list[str]:
    return [key for key, doc in DOCUMENTS.items() if doc.group == group_key]
