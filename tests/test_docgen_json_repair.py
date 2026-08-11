"""Covers app.docgen.engine's JSON-repair fallback -- added after a real
production crash where Gemini's JSON-mode response embedded a markdown
`markdown_body` string containing an invalid `\\u` escape ("Invalid \\uXXXX
escape: line 1 column 3062"), which json.loads() can't parse as-is.
"""
import json

from app.docgen.engine import _repair_invalid_backslash_escapes


def test_valid_json_is_unchanged():
    text = json.dumps({"a": "line one\nline two", "b": "quote: \" and backslash: \\\\"})
    assert _repair_invalid_backslash_escapes(text) == text
    assert json.loads(_repair_invalid_backslash_escapes(text)) == json.loads(text)


def test_repairs_invalid_u_escape_not_followed_by_four_hex_digits():
    # The real production case: markdown content containing a literal
    # "\u" that isn't a valid unicode escape (e.g. "\user" or similar).
    broken = '{"markdown_body": "some text \\unit more text"}'
    repaired = _repair_invalid_backslash_escapes(broken)

    parsed = json.loads(repaired)
    assert parsed["markdown_body"] == "some text \\unit more text"


def test_repairs_markdown_escape_sequences():
    # Markdown's own escape syntax (\* \_ \[ etc.) isn't valid JSON escape
    # syntax -- must be doubled to parse as a literal backslash + character.
    broken = '{"markdown_body": "bold\\*text\\* and \\_italic\\_"}'
    repaired = _repair_invalid_backslash_escapes(broken)

    parsed = json.loads(repaired)
    assert parsed["markdown_body"] == "bold\\*text\\* and \\_italic\\_"


def test_valid_unicode_escape_still_decodes_to_the_right_character():
    text = '{"markdown_body": "caf\\u00e9"}'
    repaired = _repair_invalid_backslash_escapes(text)

    assert json.loads(repaired)["markdown_body"] == "café"


def test_valid_escapes_like_newline_and_tab_are_preserved():
    text = '{"markdown_body": "line one\\nline two\\ttabbed"}'
    repaired = _repair_invalid_backslash_escapes(text)

    parsed = json.loads(repaired)
    assert parsed["markdown_body"] == "line one\nline two\ttabbed"
