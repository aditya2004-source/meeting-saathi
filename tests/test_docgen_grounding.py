"""Covers app.docgen.engine's grounding data -- added to fix two real client
meeting bugs: (1) the Attendees field was inferred by Gemini purely from who
spoke, silently dropping attendees who never spoke; (2) the Date field was
never given to Gemini at all, so it had to guess/extract one from transcript
content. Both are now passed in explicitly and must appear in the request
Gemini actually receives -- verified here without a real Gemini call by
patching _client.models.generate_content, same pattern as
test_docgen_max_tokens_retry.py.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import types

from app.docgen.engine import generate_documents


def _fake_response(payload: dict):
    return SimpleNamespace(text=json.dumps(payload), candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)])


def test_generate_documents_threads_attendees_and_date_into_grounding():
    extract_response = _fake_response(
        {"attendees": ["Priya Shah"], "topics_discussed": [], "decisions": [], "action_items": [], "key_quotes": []}
    )
    mom_response = _fake_response({"title": "Minutes of Meeting", "markdown_body": "body"})
    rg_response = _fake_response({"title": "Requirement Gathering Sheet", "rows": []})
    ap_response = _fake_response({"title": "Action Points", "markdown_body": "body"})

    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[extract_response, mom_response, rg_response, ap_response],
    ) as mock_call:
        generate_documents(
            "Weekly Sync",
            "[00:00:00] Priya Shah: hi",
            attendees=["Priya Shah", "Silent Person"],
            meeting_date="28 July 2026, 2:30 PM IST",
        )

    # First call is the extraction step (no attendees/meeting_date grounding
    # -- those are only added at the document-generation step); the
    # remaining three (MOM/RG/AP, order not guaranteed since they run
    # concurrently) must each carry the real roster and date verbatim.
    doc_calls = mock_call.call_args_list[1:]
    assert len(doc_calls) == 3
    for call in doc_calls:
        contents = call.kwargs["contents"]
        assert "Priya Shah" in contents
        assert "Silent Person" in contents
        assert "28 July 2026, 2:30 PM IST" in contents


def test_generate_documents_defaults_to_empty_attendees_when_not_provided():
    extract_response = _fake_response(
        {"attendees": [], "topics_discussed": [], "decisions": [], "action_items": [], "key_quotes": []}
    )
    doc_response = _fake_response({"title": "t", "markdown_body": "b"})
    rg_response = _fake_response({"title": "t", "rows": []})

    with patch(
        "app.docgen.engine._client.models.generate_content",
        side_effect=[extract_response, doc_response, rg_response, doc_response],
    ) as mock_call:
        generate_documents("Weekly Sync", "[00:00:00] Priya Shah: hi")

    doc_calls = mock_call.call_args_list[1:]
    for call in doc_calls:
        contents = call.kwargs["contents"]
        assert '"attendees": []' in contents
        assert '"meeting_date": ""' in contents
