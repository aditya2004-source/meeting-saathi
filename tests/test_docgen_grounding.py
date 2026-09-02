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


def test_all_placeholder_attendee_list_is_collapsed_to_a_count_for_gemini():
    mom_response = _fake_response({"title": "Minutes of Meeting", "markdown_body": "body"})
    anon = [f"Unidentified speaker {i}" for i in range(1, 5)] + ["Participant 2"]

    with patch("app.docgen.engine._client.models.generate_content", return_value=mom_response) as mock_call:
        engine.generate_mom("Sync", "date", anon, {"topics_discussed": []}, "[00:00:00] Unidentified speaker 1: hi")

    contents = mock_call.call_args.kwargs["contents"]
    assert "Unidentified speaker 3" not in contents
    assert "5 distinct speakers took part" in contents


def test_attendee_list_with_a_real_name_is_passed_through_untouched():
    mom_response = _fake_response({"title": "Minutes of Meeting", "markdown_body": "body"})

    with patch("app.docgen.engine._client.models.generate_content", return_value=mom_response) as mock_call:
        engine.generate_mom(
            "Sync", "date", ["Priya Shah", "Unidentified speaker 2"], {"topics_discussed": []}, "[00:00:00] Priya Shah: hi"
        )

    contents = mock_call.call_args.kwargs["contents"]
    assert "Priya Shah" in contents
    assert "Unidentified speaker 2" in contents


def test_leaked_chat_control_tokens_are_trimmed_from_markdown_body():
    leaked = _fake_response(
        {
            "title": "FRD",
            "markdown_body": (
                "## 1. Purpose & Scope\nReal content here.\n\n"
                '### Open Questions\n- A real question (Related to REQ-10)"}\n'
                "```\nThis is the complete and valid JSON format. Use it verbatim. "
                "Do not insert any other symbols. <|im_end|>_dst_id_="
            ),
        }
    )
    with patch("app.docgen.engine._client.models.generate_content", return_value=leaked):
        result = engine.generate_frd(
            "Sync",
            "date",
            ["Priya"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya: hi",
        )

    body = result["frd"]["markdown_body"]
    assert "im_end" not in body
    assert "_dst_id_" not in body
    assert "Use it verbatim" not in body
    assert "This is the complete and valid JSON" not in body
    assert body.rstrip().endswith("(Related to REQ-10)")
    assert "Real content here." in body


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

    with patch("app.docgen.engine.settings.docgen_quality_mode", False), patch(
        "app.docgen.engine._client.models.generate_content", return_value=stories_response
    ) as mock_call:
        result = engine.generate_user_stories_and_acceptance_criteria(
            "Weekly Sync",
            "28 July 2026, 2:30 PM IST",
            ["Priya Shah"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya Shah: hi",
        )

    mock_call.assert_called_once()  # one Gemini call produces all three documents
    assert set(result.keys()) == {"user_stories", "acceptance_criteria", "traceability_matrix"}
    assert "Sales Rep" in result["user_stories"]["markdown_body"]
    assert "Order saved to DB" in result["acceptance_criteria"]["markdown_body"]
    assert "REQ-1" in result["traceability_matrix"]["markdown_body"]


def test_quality_mode_adds_a_refine_pass_per_document():
    resp = _fake_response({"stories": [
        {"requirement_id": "REQ-1", "as_a": "sales rep", "i_want": "x", "so_that": "y",
         "acceptance_criteria": ["Given a, when b, then c."]}
    ]})
    with patch("app.docgen.engine.settings.docgen_quality_mode", True), patch(
        "app.docgen.engine._client.models.generate_content", return_value=resp
    ) as mock_call:
        engine.generate_user_stories_and_acceptance_criteria(
            "Sync", "date", ["Priya"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya: hi",
        )
    assert mock_call.call_count == 2  # generate + refine


def test_generate_user_stories_and_acceptance_criteria_skips_gemini_when_no_requirements():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_user_stories_and_acceptance_criteria(
            "Weekly Sync", "", [], {"requirements": []}, "[00:00:00] Priya Shah: hi"
        )

    mock_call.assert_not_called()
    assert "No requirements were extracted" in result["user_stories"]["markdown_body"]
    assert "No requirements were extracted" in result["acceptance_criteria"]["markdown_body"]


def test_generate_frd_threads_grounding_into_gemini():
    frd_response = _fake_response({"title": "FRD", "markdown_body": "## 1. Purpose & Scope\nbody"})

    with patch("app.docgen.engine.settings.docgen_quality_mode", False), patch(
        "app.docgen.engine._client.models.generate_content", return_value=frd_response
    ) as mock_call:
        result = engine.generate_frd(
            "Weekly Sync",
            "28 July 2026, 2:30 PM IST",
            ["Priya Shah"],
            {"requirements": [{"id": "REQ-1", "statement": "x", "category": "functional", "status": "clear"}]},
            "[00:00:00] Priya Shah: hi",
        )

    mock_call.assert_called_once()
    assert "REQ-1" in mock_call.call_args.kwargs["contents"]
    assert "## 1. Purpose & Scope" in result["frd"]["markdown_body"]


def test_generate_frd_skips_gemini_when_no_requirements():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_frd("Weekly Sync", "", [], {"requirements": []}, "[00:00:00] Priya Shah: hi")

    mock_call.assert_not_called()
    assert "No requirements were extracted" in result["frd"]["markdown_body"]


def test_generate_business_process_flow_never_calls_gemini_and_returns_none_when_no_process():
    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_business_process_flow("Weekly Sync", "", [], {"business_processes": []}, "text")

    mock_call.assert_not_called()
    assert result is None


def test_generate_business_process_flow_renders_multiple_processes_from_facts():
    facts = {
        "business_processes": [
            {
                "process_name": "Order Approval",
                "steps": [
                    {"id": "STEP-1", "type": "start", "description": "Order submitted", "status": "clear", "next_step_id": "STEP-2"},
                    {"id": "STEP-2", "type": "end", "description": "Order approved", "status": "clear"},
                ],
            },
            {
                "process_name": "Lead Intake",
                "steps": [
                    {"id": "STEP-1", "type": "start", "description": "Lead received", "status": "clear"},
                ],
            },
        ]
    }

    with patch("app.docgen.engine._client.models.generate_content") as mock_call:
        result = engine.generate_business_process_flow("Weekly Sync", "", [], facts, "text")

    mock_call.assert_not_called()
    source = result["business_process_flow"]["mermaid_source"]
    assert "flowchart TD" in source
    assert "Order submitted" in source
    assert 'subgraph p1["Order Approval"]' in source
    assert 'subgraph p2["Lead Intake"]' in source


def test_generate_business_process_flow_accepts_legacy_singular_facts_key():
    facts = {
        "business_process": {
            "process_name": "Order Approval",
            "steps": [
                {"id": "STEP-1", "type": "start", "description": "Order submitted", "status": "clear"},
            ],
        }
    }

    with patch("app.docgen.engine._client.models.generate_content"):
        result = engine.generate_business_process_flow("Weekly Sync", "", [], facts, "text")

    assert "Order submitted" in result["business_process_flow"]["mermaid_source"]
