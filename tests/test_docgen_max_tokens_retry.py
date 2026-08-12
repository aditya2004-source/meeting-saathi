"""Covers app.docgen.engine's retry-on-truncation fallback -- added after a
real production crash where a transcript with a Whisper hallucination loop
(repeated Hindi word from a low-signal audio chunk) made Gemini's response
balloon past max_output_tokens=4096 and get cut off mid-string, which
json.loads() reported as "Unterminated string starting at...".
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.genai import types

from app.docgen.engine import _generate_json


def _response(text: str, finish_reason):
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish_reason)])


def test_retries_with_doubled_budget_on_max_tokens_truncation():
    truncated = _response('{"a": "this got cut off mid-str', types.FinishReason.MAX_TOKENS)
    complete = _response('{"a": "full value"}', types.FinishReason.STOP)

    with patch("app.docgen.engine._client.models.generate_content", side_effect=[truncated, complete]) as mock_call:
        result = _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert result == {"a": "full value"}
    assert mock_call.call_count == 2
    assert mock_call.call_args_list[1].kwargs["config"].max_output_tokens == 8192


def test_gives_up_after_one_retry_if_still_truncated():
    truncated = _response('{"a": "still cut off', types.FinishReason.MAX_TOKENS)

    with patch("app.docgen.engine._client.models.generate_content", side_effect=[truncated, truncated]) as mock_call:
        with pytest.raises(ValueError, match="Gemini returned invalid JSON"):
            _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert mock_call.call_count == 2


def test_does_not_retry_when_truncation_is_not_the_cause():
    # e.g. a genuinely malformed response that wasn't cut off (finish_reason
    # STOP) shouldn't burn a second Gemini call -- it can't help.
    broken = _response('{"a": "unterminated', types.FinishReason.STOP)

    with patch("app.docgen.engine._client.models.generate_content", return_value=broken) as mock_call:
        with pytest.raises(ValueError, match="Gemini returned invalid JSON"):
            _generate_json("system", {"type": "OBJECT"}, "content", max_output_tokens=4096)

    assert mock_call.call_count == 1
