"""Covers app.docgen.engine's grounding data -- added to fix two real client
meeting bugs: (1) the Attendees field was inferred by Gemini purely from who
spoke, silently dropping attendees who never spoke; (2) the Date field was
never given to Gemini at all, so it had to guess/extract one from transcript
content. Both are now passed in explicitly and must appear in the request
Gemini actually receives -- verified here without a real Gemini call by
patching _client.models.generate_content, same pattern as
test_docgen_max_tokens_retry.py.

Document generation is on-demand now (see app/docgen/registry.py) -- each
document type has its own generator function (generate_mom(), generate_brd(),
...), all sharing the same (meeting_title, meeting_date, attendees, facts,
transcript_text) signature, called individually rather than through one bulk
generate_documents() call.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import types

from app.docgen import engine


def _fake_response(payload: dict):
    return SimpleNamespace(text=json.dumps(payload), candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)])


def test_generate_mom_threads_attendees_and_date_into_grounding():
    mom_response = _fake_response({"title": "Minutes of Meeting", "markdown_body": "body"})

    with patch("app.docgen.engine._client.models.generate_content", return_value=mom_response) as mock_call:
        engine.generate_mom(
            "Weekly Sync",
            "28 July 2026, 2:30 PM IST",
            ["Priya Shah", "Silent Person"],
            {"topics_discussed": []},
            "[00:00:00] Priya Shah: hi",
        )

    contents = mock_call.call_args.kwargs["contents"]
    assert "Priya Shah" in contents
    assert "Silent Person" in contents
    assert "28 July 2026, 2:30 PM IST" in contents


def test_generate_brd_threads_attendees_and_date_into_grounding():
    brd_response = _fake_response({"title": "BRD", "markdown_body": "body"})

    with patch("app.docgen.engine._client.models.generate_content", return_value=brd_response) as mock_call:
        engine.generate_brd(
            "Weekly Sync",
            "28 July 2026, 2:30 PM IST",
            ["Priya Shah"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya Shah: hi",
        )

    contents = mock_call.call_args.kwargs["contents"]
    assert "Priya Shah" in contents
    assert "28 July 2026, 2:30 PM IST" in contents


def test_generate_mom_skips_gemini_and_returns_placeholder_for_empty_transcript():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_mom("Weekly Sync", "", [], {}, "")

    mock_call.assert_not_called()
    assert "No speech was captured" in result["mom"]["markdown_body"]


def test_generate_user_stories_and_acceptance_criteria_shares_one_call():
    stories_response = _fake_response(
        {
            "stories": [
                {
                    "requirement_id": "REQ-1",
                    "as_a": "Sales Rep",
                    "i_want": "to submit an order",
                    "so_that": "the customer gets their goods",
                    "priority": "high",
                    "acceptance_criteria": ["Order saved to DB"],
                }
            ]
        }
    )

    with patch("app.docgen.engine._client.models.generate_content", return_value=stories_response) as mock_call:
        result = engine.generate_user_stories_and_acceptance_criteria(
            "Weekly Sync",
            "28 July 2026, 2:30 PM IST",
            ["Priya Shah"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya Shah: hi",
        )

    mock_call.assert_called_once()  # one Gemini call produces BOTH documents
    assert set(result.keys()) == {"user_stories", "acceptance_criteria"}
    assert "Sales Rep" in result["user_stories"]["markdown_body"]
    assert "Order saved to DB" in result["acceptance_criteria"]["markdown_body"]


def test_generate_user_stories_and_acceptance_criteria_skips_gemini_when_no_requirements():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_user_stories_and_acceptance_criteria(
            "Weekly Sync", "", [], {"requirements": []}, "[00:00:00] Priya Shah: hi"
        )

    mock_call.assert_not_called()
    assert "No requirements were extracted" in result["user_stories"]["markdown_body"]
    assert "No requirements were extracted" in result["acceptance_criteria"]["markdown_body"]


def test_generate_frd_never_calls_gemini():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_frd(
            "Weekly Sync",
            "",
            [],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya Shah: hi",
        )

    mock_call.assert_not_called()
    assert "REQ-1" in result["frd"]["markdown_body"]


def test_generate_business_process_flow_never_calls_gemini_and_returns_none_when_no_process():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_business_process_flow("Weekly Sync", "", [], {"business_process": None}, "text")

    mock_call.assert_not_called()
    assert result is None


def test_generate_business_process_flow_renders_from_facts():
    facts = {
        "business_process": {
            "process_name": "Order Approval",
            "steps": [
                {"id": "STEP-1", "type": "start", "description": "Order submitted", "status": "clear", "next_step_id": "STEP-2"},
                {"id": "STEP-2", "type": "end", "description": "Order approved", "status": "clear"},
            ],
        }
    }

    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_business_process_flow("Weekly Sync", "", [], facts, "text")

    mock_call.assert_not_called()
    assert "flowchart TD" in result["business_process_flow"]["mermaid_source"]
    assert "Order submitted" in result["business_process_flow"]["mermaid_source"]
