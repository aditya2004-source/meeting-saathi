"""Deterministic, zero-Gemini-call quality checks on a generated document.
Catches the failure modes a prompt alone can't guarantee against: leaked
prompt/JSON artifacts, a template section gone missing, a cited REQ-id that
doesn't exist, a requirement the document never mentions, an empty section.

Pure functions -- callers decide what to do with the findings (write a
sidecar, print, block). Findings are advisory: a document with findings is
still written, so a human can judge.
"""
import re

# The templated prose documents and the second-level headings each must contain.
# Keyed by registry doc_key. Diagram/table docs and the free-form ones aren't listed.
_REQUIRED_HEADINGS: dict[str, list[str]] = {
    "mom": [
        "Meeting Overview",
        "Discussion Highlights",
        "Decisions",
        "Action Items",
        "Open Questions",
        "Next Steps",
    ],
    "brd": [
        "1. Document Control",
        "2. Executive Summary",
        "3. Business Context & Background",
        "4. Current State & Pain Points",
        "5. Business Objectives & Goals",
        "6. Project Scope",
        "7. Stakeholders",
        "8. Business Requirements",
        "9. Assumptions",
        "10. Constraints",
        "11. Dependencies",
        "12. Risks",
        "13. Open Questions",
        "14. Success Criteria & KPIs",
        "15. Glossary",
    ],
    "frd": [
        "1. Purpose & Scope",
        "2. Actors & Roles",
        "3. Functional Modules Overview",
        "4. Functional Requirements by Module",
        "5. Business Rules",
        "6. Data Requirements",
        "7. Integration Requirements",
        "8. Non-Functional Requirements",
        "9. Assumptions & Constraints",
        "10. Open Questions",
        "11. Requirements Traceability",
    ],
    "meeting_analysis": [
        "Executive Snapshot",
        "Key Discussion Points",
        "Decisions & Direction",
        "What Needs To Happen Next",
        "Risks & Open Questions",
        "Analyst's Note",
    ],
}

# Documents expected to reference every requirement in facts.json.
_MUST_COVER_ALL_REQUIREMENTS = {"brd", "frd", "traceability_matrix"}

_LEAK_PATTERNS = [
    (re.compile(r"<\|[a-z_]+\|>"), "chat control token (<|...|>)"),
    (re.compile(r"_dst_id_="), "internal marker (_dst_id_=)"),
    (re.compile(r'"\s*markdown_body"\s*:'), 'raw JSON key ("markdown_body":)'),
    (re.compile(r"(?mi)^\s*```(json)?\s*$"), "stray code fence"),
    (re.compile(r"(?mi)^#+.*\b(use it verbatim|do not insert any other symbols|"
                r"EXACTLY these (numbered )?headings|extracted_facts\.)"), "prompt instruction used as a heading"),
]

_REQ_ID_RE = re.compile(r"\bREQ-\d+\b")
_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*#*$")


def _headings(markdown: str) -> list[tuple[int, str, int]]:
    """(level, text, char_offset) for every ATX heading."""
    return [(len(m.group(1)), m.group(2).strip(), m.start()) for m in _HEADING_RE.finditer(markdown)]


def review_markdown_document(doc_key: str, markdown: str, facts: dict) -> list[str]:
    findings: list[str] = []
    text = markdown or ""

    # The Business Process Flow doc legitimately contains fenced ```mermaid
    # blocks, so a bare closing ``` on its own line is expected there -- flag a
    # fence there only if the fences are unbalanced or one opens a non-mermaid
    # block (```json, ```python, ...).
    allows_mermaid = doc_key == "business_process_flow"
    for pattern, label in _LEAK_PATTERNS:
        if allows_mermaid and label == "stray code fence":
            fence_lines = re.findall(r"(?mi)^\s*```([a-z]*)\s*$", text)
            openers = [f for f in fence_lines if f]  # ```<lang> lines
            if len(fence_lines) % 2 != 0 or any(f != "mermaid" for f in openers):
                findings.append(f"Leaked artifact: {label}.")
            continue
        if pattern.search(text):
            findings.append(f"Leaked artifact: {label}.")

    headings = _headings(text)
    heading_texts = [h[1] for h in headings]

    for required in _REQUIRED_HEADINGS.get(doc_key, []):
        if not any(required.lower() in h.lower() for h in heading_texts):
            findings.append(f"Missing required section: \"{required}\".")

    # Empty section: a heading with only whitespace before the next heading / EOF.
    # A heading immediately followed by a DEEPER heading is a container (e.g. a
    # document title above its first section) and is fine.
    for i, (level, htext, offset) in enumerate(headings):
        start = offset + text[offset:].find("\n") + 1 if "\n" in text[offset:] else len(text)
        end = headings[i + 1][2] if i + 1 < len(headings) else len(text)
        next_level = headings[i + 1][0] if i + 1 < len(headings) else 0
        if not text[start:end].strip() and not next_level > level:
            findings.append(f"Empty section: \"{htext}\" has no content.")

    known_ids = {(r.get("id") or "").strip() for r in (facts.get("requirements") or []) if r.get("id")}
    cited = set(_REQ_ID_RE.findall(text))
    for missing in sorted(cited - known_ids):
        findings.append(f"Cites {missing}, which is not in facts.json.")

    if doc_key in _MUST_COVER_ALL_REQUIREMENTS and known_ids:
        uncovered = sorted(known_ids - cited)
        if uncovered:
            findings.append(
                f"Does not reference {len(uncovered)} requirement(s): {', '.join(uncovered)}."
            )

    if "Not discussed in this meeting" in text and text.count("Not discussed in this meeting") > max(
        3, len(_REQUIRED_HEADINGS.get(doc_key, [])) // 2
    ):
        findings.append(
            "Many sections are \"Not discussed in this meeting\" -- extraction may be thin "
            "or the transcript may be low quality."
        )

    return findings
