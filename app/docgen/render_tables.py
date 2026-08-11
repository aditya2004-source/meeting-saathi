"""Renders structured document data (e.g. the Requirement Gathering Sheet's
`rows`) into markdown tables. Kept separate from generate_prompt.py/engine.py
so the escaping/formatting rules are pure functions, easy to unit test
without a Gemini API call.
"""

_REQUIREMENT_GATHERING_HEADERS = [
    "Requirements",
    "Mapping of Requirements",
    "Customisation for Mapping / Some Requirements in Sarathi",
    "Queries regarding Requirements or Mapping",
]

_NOT_DISCUSSED = "Not discussed in this meeting"


def _escape_cell(text: str) -> str:
    """Markdown table cells can't contain a literal newline or an
    unescaped `|` (either breaks the row into extra/malformed columns), so
    both are neutralized before a value goes into a cell.
    """
    return text.replace("|", "\\|").replace("\n", "<br>")


def _join_bullets(items: list[str], empty_fallback: str) -> str:
    if not items:
        return empty_fallback
    return "<br>".join(_escape_cell(item) for item in items)


def render_requirement_gathering_markdown(title: str, rows: list[dict]) -> str:
    lines = [f"# {title}", ""]
    lines.append("| " + " | ".join(_REQUIREMENT_GATHERING_HEADERS) + " |")
    lines.append("| " + " | ".join("---" for _ in _REQUIREMENT_GATHERING_HEADERS) + " |")

    for row in rows:
        area = _escape_cell(row.get("area") or _NOT_DISCUSSED)
        discussion = _join_bullets(row.get("discussion_points") or [], _NOT_DISCUSSED)
        mapping = _escape_cell(row.get("sarathi_mapping") or _NOT_DISCUSSED)
        queries = _join_bullets(row.get("open_queries") or [], "None")
        lines.append(f"| {area} | {discussion} | {mapping} | {queries} |")

    return "\n".join(lines)
