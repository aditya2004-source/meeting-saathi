from app.docgen.render_tables import (
    render_acceptance_criteria_markdown,
    render_frd_markdown,
    render_user_stories_markdown,
)


def test_frd_renders_header_and_title():
    markdown = render_frd_markdown("Acme Corp Kickoff", [])

    assert markdown.startswith("# Acme Corp Kickoff")
    assert "| ID | Requirement | Category | Priority | Stakeholder | Status |" in markdown


def test_frd_empty_fields_fall_back_to_placeholders():
    markdown = render_frd_markdown(
        "Kickoff",
        [{"id": "REQ-1", "statement": "", "category": "", "priority": None, "stakeholder": None, "status": "clear"}],
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| REQ-1"))
    assert "Not discussed in this meeting" in row
    assert "Not specified" in row
    assert row.endswith("| Clear |")


def test_frd_needs_clarification_status_is_flagged():
    markdown = render_frd_markdown(
        "Kickoff",
        [
            {
                "id": "REQ-2",
                "statement": "Approve discounts above 10%",
                "category": "functional",
                "priority": "high",
                "stakeholder": "Sales Manager",
                "status": "needs_clarification",
            }
        ],
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| REQ-2"))
    assert "Needs Clarification" in row


def test_frd_pipe_characters_are_escaped():
    markdown = render_frd_markdown(
        "Kickoff",
        [
            {
                "id": "REQ-3",
                "statement": "Support Cash | Cheque | UPI",
                "category": "functional",
                "priority": "medium",
                "stakeholder": "Finance",
                "status": "clear",
            }
        ],
    )

    assert "Support Cash \\| Cheque \\| UPI" in markdown
    row = next(line for line in markdown.splitlines() if line.startswith("| REQ-3"))
    # 6 data columns -> 7 pipes total (leading/trailing + 5 separators), none unescaped.
    assert row.count("|") - row.count("\\|") == 7


def test_frd_stray_newline_inside_a_field_becomes_br():
    markdown = render_frd_markdown(
        "Kickoff",
        [
            {
                "id": "REQ-4",
                "statement": "Line 1\nLine 2",
                "category": "functional",
                "priority": None,
                "stakeholder": None,
                "status": "clear",
            }
        ],
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| REQ-4"))
    assert "\n" not in row
    assert "Line 1<br>Line 2" in row


def test_user_stories_renders_rows():
    markdown = render_user_stories_markdown(
        "User Stories",
        [
            {
                "requirement_id": "REQ-1",
                "as_a": "Sales Rep",
                "i_want": "to submit an order",
                "so_that": "the customer gets their goods",
                "priority": "high",
            }
        ],
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| REQ-1"))
    assert "Sales Rep" in row
    assert "to submit an order" in row
    assert "high" in row


def test_acceptance_criteria_joins_bullets_and_falls_back():
    markdown = render_acceptance_criteria_markdown(
        "Acceptance Criteria",
        [
            {
                "requirement_id": "REQ-1",
                "as_a": "Sales Rep",
                "i_want": "to submit an order",
                "acceptance_criteria": ["Order saved to DB", "Confirmation email sent"],
            },
            {
                "requirement_id": "REQ-2",
                "as_a": "?",
                "i_want": "?",
                "acceptance_criteria": [],
            },
        ],
    )

    row1 = next(line for line in markdown.splitlines() if line.startswith("| REQ-1"))
    assert "Order saved to DB<br>Confirmation email sent" in row1
    row2 = next(line for line in markdown.splitlines() if line.startswith("| REQ-2"))
    assert row2.endswith("| None |")
