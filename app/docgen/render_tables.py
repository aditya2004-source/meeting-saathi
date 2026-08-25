"""Renders structured document data (extracted facts / generated-schema output) into
markdown tables. Kept separate from generate_prompt.py/engine.py so the
escaping/formatting rules are pure functions, easy to unit test without a Gemini API
call.
"""

_NOT_DISCUSSED = "Not discussed in this meeting"
_NOT_SPECIFIED = "Not specified"


def _escape_cell(text: str) -> str:
    """Markdown table cells can't contain a literal newline or an unescaped `|`
    (either breaks the row into extra/malformed columns), so both are neutralized
    before a value goes into a cell.
    """
    return text.replace("|", "\\|").replace("\n", "<br>")


def _join_bullets(items: list[str], empty_fallback: str) -> str:
    if not items:
        return empty_fallback
    return "<br>".join(_escape_cell(item) for item in items)


def _render_table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    lines = [f"# {title}", ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


_FRD_HEADERS = ["ID", "Requirement", "Category", "Priority", "Stakeholder", "Status"]


def render_frd_markdown(title: str, requirements: list[dict]) -> str:
    """Functional Requirements Document -- a pure, zero-Gemini-call render of
    facts["requirements"] (see app/docgen/extract_prompt.py). Replaces the deleted
    Sarathi-specific Requirement Gathering Sheet; fully product-agnostic.
    """
    rows = []
    for req in requirements:
        status = req.get("status") or "clear"
        status_cell = "\u26a0 Needs Clarification" if status == "needs_clarification" else "Clear"
        rows.append(
            [
                _escape_cell(req.get("id") or ""),
                _escape_cell(req.get("statement") or _NOT_DISCUSSED),
                _escape_cell(req.get("category") or "other"),
                _escape_cell(req.get("priority") or _NOT_SPECIFIED),
                _escape_cell(req.get("stakeholder") or _NOT_SPECIFIED),
                status_cell,
            ]
        )
    return _render_table(title, _FRD_HEADERS, rows)


_USER_STORIES_HEADERS = ["Requirement ID", "As a", "I want", "So that", "Priority"]


def render_user_stories_markdown(title: str, stories: list[dict]) -> str:
    rows = []
    for story in stories:
        rows.append(
            [
                _escape_cell(story.get("requirement_id") or ""),
                _escape_cell(story.get("as_a") or _NOT_DISCUSSED),
                _escape_cell(story.get("i_want") or _NOT_DISCUSSED),
                _escape_cell(story.get("so_that") or _NOT_DISCUSSED),
                _escape_cell(story.get("priority") or _NOT_SPECIFIED),
            ]
        )
    return _render_table(title, _USER_STORIES_HEADERS, rows)


_ACCEPTANCE_CRITERIA_HEADERS = ["Requirement ID", "Story", "Acceptance Criteria"]


def render_acceptance_criteria_markdown(title: str, stories: list[dict]) -> str:
    rows = []
    for story in stories:
        story_summary = f"As a {story.get('as_a') or '?'}, I want {story.get('i_want') or '?'}"
        criteria = _join_bullets(story.get("acceptance_criteria") or [], "None")
        rows.append(
            [
                _escape_cell(story.get("requirement_id") or ""),
                _escape_cell(story_summary),
                criteria,
            ]
        )
    return _render_table(title, _ACCEPTANCE_CRITERIA_HEADERS, rows)
