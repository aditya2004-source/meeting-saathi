from app.docgen.render_tables import render_requirement_gathering_markdown


def test_renders_header_and_title():
    markdown = render_requirement_gathering_markdown("Acme Corp Kickoff", [])

    assert markdown.startswith("# Acme Corp Kickoff")
    assert "| Requirements | Mapping of Requirements |" in markdown
    assert "Customisation for Mapping / Some Requirements in Sarathi" in markdown
    assert "Queries regarding Requirements or Mapping" in markdown


def test_empty_discussion_points_falls_back_to_not_discussed():
    markdown = render_requirement_gathering_markdown(
        "Kickoff",
        [{"area": "Attendance", "discussion_points": [], "sarathi_mapping": "", "open_queries": []}],
    )

    rows = [line for line in markdown.splitlines() if line.startswith("| Attendance")]
    assert len(rows) == 1
    assert "Not discussed in this meeting" in rows[0]
    assert "| None |" in rows[0] or rows[0].endswith("| None |")


def test_multi_bullet_cells_joined_with_br():
    markdown = render_requirement_gathering_markdown(
        "Kickoff",
        [
            {
                "area": "Order Management",
                "discussion_points": ["Bulk order entry", "Approval workflow"],
                "sarathi_mapping": "Orders module",
                "open_queries": ["Who approves discounts?"],
            }
        ],
    )

    assert "Bulk order entry<br>Approval workflow" in markdown
    assert "Who approves discounts?" in markdown


def test_pipe_characters_are_escaped():
    markdown = render_requirement_gathering_markdown(
        "Kickoff",
        [
            {
                "area": "Payment Collection",
                "discussion_points": ["Cash | Cheque | UPI accepted"],
                "sarathi_mapping": "Payments module",
                "open_queries": [],
            }
        ],
    )

    assert "Cash \\| Cheque \\| UPI accepted" in markdown
    # The row must still parse as exactly 4 data columns (5 pipes total
    # including the leading/trailing ones), i.e. no unescaped `|` leaked in.
    row = next(line for line in markdown.splitlines() if line.startswith("| Payment Collection"))
    assert row.count("|") - row.count("\\|") == 5


def test_stray_newline_inside_a_field_becomes_br():
    markdown = render_requirement_gathering_markdown(
        "Kickoff",
        [
            {
                "area": "Expense Management",
                "discussion_points": [],
                "sarathi_mapping": "Line 1\nLine 2",
                "open_queries": [],
            }
        ],
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| Expense Management"))
    assert "\n" not in row
    assert "Line 1<br>Line 2" in row
