from app.docgen.validate import review_markdown_document

_FACTS = {"requirements": [{"id": "REQ-1"}, {"id": "REQ-2"}, {"id": "REQ-3"}]}


def _brd(*sections: str) -> str:
    return "\n\n".join(sections)


def test_clean_frd_has_no_findings():
    body = "\n\n".join(
        f"## {h}\nContent for {h}."
        for h in [
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
        ]
    )
    body += "\nCovers REQ-1, REQ-2 and REQ-3."
    assert review_markdown_document("frd", body, _FACTS) == []


def test_missing_section_and_uncovered_requirements_are_flagged():
    body = "## 1. Purpose & Scope\nText mentioning REQ-1 only."
    findings = review_markdown_document("frd", body, _FACTS)
    assert any("Missing required section" in f and "Actors & Roles" in f for f in findings)
    assert any("Does not reference 2 requirement(s): REQ-2, REQ-3" in f for f in findings)


def test_leaked_artifacts_are_flagged():
    body = "## 1. Document Control\nText.\n```json\n{\"x\": 1}\n```\n<|im_end|>"
    findings = review_markdown_document("brd", body, _FACTS)
    assert any("code fence" in f for f in findings)
    assert any("chat control token" in f for f in findings)


def test_unknown_req_id_citation_is_flagged():
    body = "## 1. Purpose & Scope\nSee REQ-1 and REQ-9."
    findings = review_markdown_document("frd", body, _FACTS)
    assert any("REQ-9, which is not in facts.json" in f for f in findings)


def test_empty_section_is_flagged():
    body = "## Executive Snapshot\n\n## Key Discussion Points\nsome text"
    findings = review_markdown_document("meeting_analysis", body, {"requirements": []})
    assert any('Empty section: "Executive Snapshot"' in f for f in findings)


def test_untemplated_doc_key_only_gets_leak_and_citation_checks():
    body = "# User Stories\n\n## Lead Management\n| REQ-1 | rep | x | y | high |"
    assert review_markdown_document("user_stories", body, _FACTS) == []
